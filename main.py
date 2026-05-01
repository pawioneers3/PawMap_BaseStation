from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

try:
    from supabase import Client, create_client  # type: ignore
except Exception:
    Client = Any  # type: ignore
    create_client = None  # type: ignore

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase_client: Optional[Client] = None  # type: ignore[valid-type]
if create_client and SUPABASE_URL:
    for candidate_key in (SUPABASE_KEY, SUPABASE_SERVICE_ROLE_KEY):
        if not candidate_key:
            continue
        try:
            supabase_client = create_client(SUPABASE_URL, candidate_key)
            break
        except Exception:
            supabase_client = None

supabase_admin_client: Optional[Client] = None  # type: ignore[valid-type]
if create_client and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase_admin_client = create_client(
            SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    except Exception:
        supabase_admin_client = None

DB_PATH = BASE_DIR / "basestation.db"
NOTIFY_COOLDOWN_S = 60
BATTERY_LOW_THRESHOLD = 10

# Global mock config pushed down to devices (dog tracker demo).
server_config: dict[str, Any] = {
    "gps_center": {"lat": 14.5995, "lng": 120.9842},  # fallback/default
    "gps_radius_m": 150.0,
    # Bounding box for the current demo area (test grid).
    "gps_bbox": {
        "min_lat": 10.657589126982737,
        "max_lat": 10.65811214392038,
        "min_lng": 122.95009491944131,
        "max_lng": 122.95057763399497,
    },
    "gps_mode": "bbox",  # "radius" | "bbox"
    "battery_drain": 1,
    "battery_loop": True,
    "post_interval_min": 1,  # 1 | 5 | 15 | 30
    "gps_check_every_n_posts": 1,  # 1 | 2 | 5
}


def _supabase_unavailable_message() -> str:
    if not create_client:
        return "Supabase support is unavailable because the Python 'supabase' package is not installed."
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in PawMap_BaseStation/.env."
    return "Supabase client could not be initialized. Verify the credentials and installed dependencies."


def _default_name(device_id: str) -> str:
    suffix = (device_id or "0000")[-4:]
    return f"Dog-{suffix}"


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=3000;")
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                shelter_user_id TEXT,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                force_oob INTEGER NOT NULL DEFAULT 0,
                force_low_battery INTEGER NOT NULL DEFAULT 0,
                freeze_lat REAL,
                freeze_lng REAL,
                last_oob INTEGER,
                last_notify_ts REAL NOT NULL DEFAULT 0,
                last_batt_low INTEGER,
                last_batt_notify_ts REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pairing_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                expected_name TEXT NOT NULL,
                claim_token TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                claimed_device_id TEXT,
                claimed_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pairing_claims_expected_active
            ON pairing_claims(expected_name, expires_at, claimed_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                ts REAL NOT NULL,
                status TEXT,
                battery INTEGER,
                lat REAL,
                lng REAL,
                raw_json TEXT NOT NULL,
                FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_readings_device_ts
            ON readings(device_id, ts DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fcm_tokens (
                token TEXT PRIMARY KEY,
                device_id TEXT,
                platform TEXT,
                created_at REAL NOT NULL,
                last_used_at REAL,
                FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE SET NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                ok INTEGER NOT NULL,
                response TEXT,
                FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE
            )
            """
        )

        # Best-effort migrations for existing DBs.
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN shelter_user_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE pairing_claims ADD COLUMN claim_token TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pairing_claims_token
                ON pairing_claims(claim_token)
                """
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN force_oob INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN force_low_battery INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN freeze_lat REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN freeze_lng REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN last_oob INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN last_notify_ts REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN last_batt_low INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN last_batt_notify_ts REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass


def _load_server_config() -> None:
    global server_config
    with _db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k = ?",
                           ("server_config",)).fetchone()
        if not row:
            return
        try:
            loaded = json.loads(row["v"])
            if isinstance(loaded, dict):
                server_config.update(loaded)
        except Exception:
            return


def _save_server_config() -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            ("server_config", json.dumps(server_config)),
        )


# Ensure DB is ready even when imported (e.g., tests / Flask reloader).
_init_db()
_load_server_config()


def _compute_geofence(data: dict[str, Any], cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    active_cfg = cfg or server_config
    gps = data.get("gps") if isinstance(data.get("gps"), dict) else None
    if not gps:
        return {"out_of_bounds": None, "in_bounds": None, "reason": "no_gps"}

    try:
        lat = float(gps.get("lat"))
        lng = float(gps.get("lng"))
    except Exception:
        return {"out_of_bounds": None, "in_bounds": None, "reason": "bad_gps"}

    mode = str(active_cfg.get("gps_mode") or "radius").lower()
    if mode == "bbox":
        bbox = active_cfg.get("gps_bbox") if isinstance(
            active_cfg.get("gps_bbox"), dict) else None
        if not bbox:
            return {"out_of_bounds": None, "in_bounds": None, "reason": "no_bbox"}
        try:
            min_lat = float(bbox.get("min_lat"))
            max_lat = float(bbox.get("max_lat"))
            min_lng = float(bbox.get("min_lng"))
            max_lng = float(bbox.get("max_lng"))
        except Exception:
            return {"out_of_bounds": None, "in_bounds": None, "reason": "bad_bbox"}
        in_bounds = (min_lat <= lat <= max_lat) and (min_lng <= lng <= max_lng)
        return {
            "out_of_bounds": (not in_bounds),
            "in_bounds": in_bounds,
            "reason": "bbox",
        }

    # radius mode (approx)
    center = active_cfg.get("gps_center") if isinstance(
        active_cfg.get("gps_center"), dict) else None
    radius_m = active_cfg.get("gps_radius_m")
    if not center:
        return {"out_of_bounds": None, "in_bounds": None, "reason": "no_center"}
    try:
        clat = float(center.get("lat"))
        clng = float(center.get("lng"))
        r_m = float(radius_m)
    except Exception:
        return {"out_of_bounds": None, "in_bounds": None, "reason": "bad_center"}
    if r_m <= 0:
        return {"out_of_bounds": None, "in_bounds": None, "reason": "bad_radius"}

    # Small-area approximation.
    import math

    meters_per_deg_lat = 111_320.0
    meters_per_deg_lng = 111_320.0 * math.cos(math.radians(clat))
    d_lat_m = (lat - clat) * meters_per_deg_lat
    d_lng_m = (lng - clng) * (meters_per_deg_lng or 1.0)
    dist_m = math.sqrt(d_lat_m * d_lat_m + d_lng_m * d_lng_m)
    in_bounds = dist_m <= r_m
    return {
        "out_of_bounds": (not in_bounds),
        "in_bounds": in_bounds,
        "reason": "radius",
    }


def _fcm_is_configured() -> bool:
    # Either provide a service account file path, or disable (then we just log).
    import os

    return bool(os.environ.get("FCM_SERVICE_ACCOUNT_FILE"))


_fcm_token_cache: dict[str, Any] = {"token": None, "exp": 0.0}


def _fcm_access_token() -> str | None:
    import os

    if not _fcm_is_configured():
        return None

    now = time.time()
    if _fcm_token_cache["token"] and now < float(_fcm_token_cache["exp"] or 0) - 60:
        return str(_fcm_token_cache["token"])

    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except Exception:
        return None

    sa_file = os.environ.get("FCM_SERVICE_ACCOUNT_FILE")
    if not sa_file:
        return None

    scopes = ["https://www.googleapis.com/auth/firebase.messaging"]
    creds = service_account.Credentials.from_service_account_file(
        sa_file, scopes=scopes)
    creds.refresh(GoogleAuthRequest())
    _fcm_token_cache["token"] = creds.token
    # creds.expiry is a datetime
    try:
        _fcm_token_cache["exp"] = float(
            creds.expiry.timestamp())  # type: ignore[union-attr]
    except Exception:
        _fcm_token_cache["exp"] = now + 3000
    return str(creds.token)


def _fcm_project_id() -> str | None:
    import os

    pid = os.environ.get("FCM_PROJECT_ID")
    if pid:
        return pid

    sa_file = os.environ.get("FCM_SERVICE_ACCOUNT_FILE")
    if not sa_file:
        return None
    try:
        with open(sa_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = data.get("project_id")
        return str(pid) if pid else None
    except Exception:
        return None


def _send_fcm(token: str, title: str, body: str, data: dict[str, Any]) -> tuple[bool, str]:
    access = _fcm_access_token()
    project_id = _fcm_project_id()
    if not access or not project_id:
        return False, "fcm_not_configured"

    try:
        import requests  # type: ignore
    except Exception:
        return False, "missing_requests"

    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    payload = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in (data or {}).items()},
        }
    }
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {access}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=6,
        )
        ok = 200 <= resp.status_code < 300
        return ok, resp.text[:2000]
    except Exception as e:
        return False, f"exception:{e!r}"


def _record_and_send_notification(
    *,
    device_id: str,
    kind: str,
    title: str,
    body: str,
    now: float,
    tokens: list[str],
    data: dict[str, Any],
) -> None:
    if not tokens:
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO notifications(device_id, ts, kind, title, body, ok, response)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (device_id, now, kind, title, body, 0, "no_tokens"),
            )
        return

    any_ok = False
    responses: list[str] = []
    for tok in tokens:
        ok, resp = _send_fcm(tok, title, body, data)
        any_ok = any_ok or ok
        responses.append(resp)

    with _db() as conn:
        for tok in tokens:
            conn.execute(
                "UPDATE fcm_tokens SET last_used_at = ? WHERE token = ?",
                (now, tok),
            )
        conn.execute(
            """
            INSERT INTO notifications(device_id, ts, kind, title, body, ok, response)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, now, kind, title, body,
             1 if any_ok else 0, "\n---\n".join(responses)[:2000]),
        )


def _resolve_pending_claim_user_id(incoming_name: str, now_ts: float) -> Optional[str]:
    if not incoming_name:
        return None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT id, user_id
            FROM pairing_claims
            WHERE expected_name = ?
              AND claimed_at IS NULL
              AND expires_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (incoming_name, now_ts),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE pairing_claims SET claimed_at = ? WHERE id = ?", (now_ts, row["id"]))
        return str(row["user_id"])


def _resolve_pending_claim_user_id_by_token(claim_token: str, now_ts: float) -> Optional[str]:
    if not claim_token:
        return None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT id, user_id
            FROM pairing_claims
            WHERE claim_token = ?
              AND claimed_at IS NULL
              AND expires_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (claim_token, now_ts),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE pairing_claims SET claimed_at = ? WHERE id = ?", (now_ts, row["id"]))
        return str(row["user_id"])


def _supabase_target_user_ids_for_alerts(device_id: Optional[str] = None) -> list[str]:
    # Optional explicit override to keep demo routing simple.
    env_ids = (os.environ.get("SUPABASE_NOTIFY_USER_IDS") or "").strip()
    if env_ids:
        ids = [p.strip() for p in env_ids.split(",") if p.strip()]
        if ids:
            return ids

    if device_id:
        with _db() as conn:
            drow = conn.execute(
                "SELECT shelter_user_id FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            owner_id = str(drow["shelter_user_id"]).strip() if (
                drow and drow["shelter_user_id"]) else ""
            if owner_id:
                return [owner_id]
        return []

    client = supabase_admin_client or supabase_client
    if not client:
        return []
    try:
        # Default audience: active shelter users.
        result = (
            client.table("profiles")
            .select("user_id")
            .eq("role", "shelter")
            .eq("status", "active")
            .execute()
        )
        rows = result.data or []
        return [str(row.get("user_id")) for row in rows if isinstance(row, dict) and row.get("user_id")]
    except Exception:
        return []


def _send_supabase_notification(
    *,
    device_id: Optional[str] = None,
    category: str,
    title: str,
    message: str,
    route_path: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    client = supabase_admin_client or supabase_client
    if not client:
        return {"enabled": False, "sent": 0, "failed": 0, "reason": "supabase_client_unavailable"}

    target_user_ids = _supabase_target_user_ids_for_alerts(device_id=device_id)
    if not target_user_ids:
        return {"enabled": True, "sent": 0, "failed": 0, "reason": "no_target_users"}

    sent = 0
    failed = 0
    for user_id in target_user_ids:
        try:
            (client.rpc)(
                "insert_notification",
                {
                    "target_user_id": user_id,
                    "target_category": category,
                    "target_title": title,
                    "target_message": message,
                    "target_route_path": route_path,
                    "target_payload": payload or {},
                },
            ).execute()
            sent += 1
        except Exception:
            failed += 1

    return {"enabled": True, "sent": sent, "failed": failed, "reason": "ok"}


def _fcm_runtime_status() -> dict[str, Any]:
    sa_file = os.environ.get("FCM_SERVICE_ACCOUNT_FILE")
    project_id = _fcm_project_id()
    supabase_bridge = bool(supabase_admin_client or supabase_client)
    with _db() as conn:
        token_row = conn.execute(
            "SELECT COUNT(*) AS c FROM fcm_tokens").fetchone()
        global_row = conn.execute(
            "SELECT COUNT(*) AS c FROM fcm_tokens WHERE device_id IS NULL"
        ).fetchone()
        failed_row = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE ok = 0"
        ).fetchone()
    return {
        "configured": bool(sa_file and project_id),
        "service_account_file_set": bool(sa_file),
        "service_account_file_exists": bool(sa_file and Path(sa_file).exists()),
        "project_id": project_id,
        "registered_tokens": int(token_row["c"] if token_row else 0),
        "global_tokens": int(global_row["c"] if global_row else 0),
        "failed_notifications": int(failed_row["c"] if failed_row else 0),
        "critical_events": ["oob", "low_battery"],
        "supabase_bridge_enabled": supabase_bridge,
    }


def _retry_failed_notifications(limit: int = 50) -> dict[str, int]:
    retried = 0
    sent_ok = 0
    skipped_no_tokens = 0
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT id, device_id, kind, title, body, response
            FROM notifications
            WHERE ok = 0
            ORDER BY ts DESC
            LIMIT ?
            """,
            (max(1, min(500, limit)),),
        ).fetchall()

        for row in rows:
            retried += 1
            device_id = str(row["device_id"])
            tokens_rows = conn.execute(
                """
                SELECT token FROM fcm_tokens
                WHERE device_id = ? OR device_id IS NULL
                """,
                (device_id,),
            ).fetchall()
            tokens = [str(r["token"]) for r in tokens_rows]
            if not tokens:
                skipped_no_tokens += 1
                continue

            data = {"device_id": device_id,
                    "event": str(row["kind"] or "critical")}
            responses: list[str] = []
            any_ok = False
            for token in tokens:
                ok, resp = _send_fcm(token, str(
                    row["title"]), str(row["body"]), data)
                any_ok = any_ok or ok
                responses.append(resp)
                if ok:
                    conn.execute(
                        "UPDATE fcm_tokens SET last_used_at = ? WHERE token = ?",
                        (time.time(), token),
                    )

            if any_ok:
                sent_ok += 1
                conn.execute(
                    "UPDATE notifications SET ok = 1, response = ? WHERE id = ?",
                    ("retried_ok\n---\n" + "\n---\n".join(responses))[:2000],
                    row["id"],
                )
            else:
                conn.execute(
                    "UPDATE notifications SET response = ? WHERE id = ?",
                    ("\n---\n".join(responses)
                     [:2000] or str(row["response"] or "retry_failed")),
                    row["id"],
                )

    return {"retried": retried, "sent_ok": sent_ok, "skipped_no_tokens": skipped_no_tokens}


def _push_tokens_for_test(device_id: Optional[str] = None, include_global: bool = True) -> list[str]:
    with _db() as conn:
        if device_id:
            if include_global:
                rows = conn.execute(
                    """
                    SELECT token FROM fcm_tokens
                    WHERE device_id = ? OR device_id IS NULL
                    """,
                    (device_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT token FROM fcm_tokens WHERE device_id = ?",
                    (device_id,),
                ).fetchall()
        else:
            rows = conn.execute("SELECT token FROM fcm_tokens").fetchall()
    # Deduplicate while preserving order.
    seen: set[str] = set()
    tokens: list[str] = []
    for row in rows:
        token = str(row["token"])
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _device_view(
    device_id: str,
    entry: dict[str, Any],
    geofence_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    active_cfg = geofence_config or server_config

    offline_timeout_s = 90

    last_seen = float(entry.get("last_seen", 0.0))
    data = entry.get("data") or {}
    name = entry.get("name") or data.get("name") or _default_name(device_id)

    force_oob = bool(entry.get("force_oob") or False)
    force_low_battery = bool(entry.get("force_low_battery") or False)
    freeze_lat = entry.get("freeze_lat")
    freeze_lng = entry.get("freeze_lng")

    age_s = max(0.0, time.time() - last_seen) if last_seen else Nones

    if last_seen and age_s is not None and age_s <= offline_timeout_s:
        status = "online"
    else:
        status = "offline"

    return {
        "device_id": device_id,
        "name": name,
        "last_seen": last_seen,
        "age_s": age_s,
        "status": status,
        "data": data,
        "geofence": _compute_geofence(data, geofence_config),
        "force_oob": force_oob,
        "force_low_battery": force_low_battery,
        "location_frozen": bool(
            force_low_battery and freeze_lat is not None and freeze_lng is not None
        ),
    }


@app.get("/")
def home() -> Any:
    user, _ = _session_user()
    if user:
        return redirect(url_for("dashboard"))
    return redirect(url_for("shelter_login"))


def _serialize_auth_user(user: Any) -> dict[str, Any]:
    if not user:
        return {}
    return {
        "id": getattr(user, "id", None),
        "email": getattr(user, "email", None),
        "phone": getattr(user, "phone", None),
        "role": getattr(user, "role", None),
        "aud": getattr(user, "aud", None),
    }


def _session_user() -> tuple[Optional[Any], Optional[str]]:
    if not supabase_client:
        return None, None
    token = session.get("sb_access_token")
    if not token:
        return None, None
    try:
        user_response = supabase_client.auth.get_user(token)
        user = getattr(user_response, "user", None)
        if not user:
            session.clear()
            return None, None
        return user, token
    except Exception:
        session.clear()
        return None, None


def _profile_for_user(user_id: str) -> Optional[dict[str, Any]]:
    if not supabase_client:
        return None
    try:
        result = supabase_client.table("profiles").select(
            "*").eq("user_id", user_id).maybe_single().execute()
        return result.data
    except Exception:
        return None


def _iter_geojson_coordinates(coords: Any):
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and isinstance(coords[0], (int, float)) and isinstance(coords[1], (int, float)):
            yield float(coords[0]), float(coords[1])
            return
        for item in coords:
            yield from _iter_geojson_coordinates(item)


def _bbox_from_geojson(geojson: Any) -> Optional[dict[str, float]]:
    if isinstance(geojson, str):
        try:
            geojson = json.loads(geojson)
        except Exception:
            return None
    if not isinstance(geojson, dict):
        return None

    geometry = geojson
    geo_type = str(geojson.get("type") or "")
    if geo_type == "Feature":
        geometry = geojson.get("geometry")
    elif geo_type == "FeatureCollection":
        features = geojson.get("features")
        if isinstance(features, list):
            points: list[tuple[float, float]] = []
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                part = _bbox_from_geojson(feature)
                if not part:
                    continue
                points.extend(
                    [
                        (part["min_lng"], part["min_lat"]),
                        (part["max_lng"], part["max_lat"]),
                    ]
                )
            if not points:
                return None
            lngs = [p[0] for p in points]
            lats = [p[1] for p in points]
            return {
                "min_lat": min(lats),
                "max_lat": max(lats),
                "min_lng": min(lngs),
                "max_lng": max(lngs),
            }
        return None

    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if coords is None:
        return None

    lngs: list[float] = []
    lats: list[float] = []
    for lng, lat in _iter_geojson_coordinates(coords):
        lngs.append(lng)
        lats.append(lat)
    if not lngs or not lats:
        return None
    return {
        "min_lat": min(lats),
        "max_lat": max(lats),
        "min_lng": min(lngs),
        "max_lng": max(lngs),
    }


def _shelter_boundary_bbox(user_id: str) -> Optional[dict[str, float]]:
    if not supabase_client or not user_id:
        return None
    try:
        result = (
            supabase_client.table("shelter_boundaries")
            .select("polygon_geojson")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        row = result.data or {}
        polygon = row.get("polygon_geojson") if isinstance(row, dict) else None
        return _bbox_from_geojson(polygon)
    except Exception:
        return None


def _effective_server_config_for_user(user_id: Optional[str]) -> dict[str, Any]:
    effective = dict(server_config)
    effective["gps_bbox_source"] = "manual"

    if user_id:
        boundary_bbox = _shelter_boundary_bbox(str(user_id))
        if boundary_bbox:
            effective["gps_mode"] = "bbox"
            effective["gps_bbox"] = boundary_bbox
            effective["gps_bbox_source"] = "shelter_boundary"
    return effective


def _effective_server_config_for_request() -> dict[str, Any]:
    user = getattr(request, "user", None)
    user_id = getattr(user, "id", None) if user else None
    return _effective_server_config_for_user(str(user_id) if user_id else None)


def require_web_session_page(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user, _ = _session_user()
        if not user:
            return redirect(url_for("shelter_login"))
        request.user = user  # type: ignore[attr-defined]
        return fn(*args, **kwargs)

    return wrapper


def require_web_session_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user, _ = _session_user()
        if not user:
            return jsonify({"status": "error", "error": "unauthorized"}), 401
        request.user = user  # type: ignore[attr-defined]
        return fn(*args, **kwargs)

    return wrapper


@app.get("/dashboard")
@require_web_session_page
def dashboard() -> str:
    # Prefill base station IP from how the user accessed this page.
    server_ip = (request.host or "").split(":", 1)[0]
    return render_template("index.html", server_ip=server_ip, user=_serialize_auth_user(getattr(request, "user", None)))


@app.get("/shelter/login")
def shelter_login() -> Any:
    if not supabase_client:
        return render_template(
            "auth_portal.html",
            error=_supabase_unavailable_message(),
            info=None,
        )
    user, _ = _session_user()
    if user:
        return redirect(url_for("shelter_dashboard"))
    return render_template("auth_portal.html", error=None, info=None)


@app.post("/shelter/sign-in")
def shelter_sign_in() -> Any:
    if not supabase_client:
        return render_template(
            "auth_portal.html",
            error=_supabase_unavailable_message(),
            info=None,
        ), 500
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if not email or not password:
        return render_template("auth_portal.html", error="Email and password are required.", info=None), 400
    try:
        auth_response = supabase_client.auth.sign_in_with_password(
            {"email": email, "password": password})
        user = getattr(auth_response, "user", None)
        auth_session = getattr(auth_response, "session", None)
        if not user or not auth_session:
            return render_template(
                "auth_portal.html",
                error="Sign-in did not return a session. Check email verification settings.",
                info=None,
            ), 401
        session["sb_access_token"] = getattr(
            auth_session, "access_token", None)
        session["sb_refresh_token"] = getattr(
            auth_session, "refresh_token", None)
        session["sb_user_id"] = getattr(user, "id", None)
        return redirect(url_for("shelter_dashboard"))
    except Exception as e:
        return render_template("auth_portal.html", error=f"Sign-in failed: {e}", info=None), 401


@app.post("/shelter/sign-up")
def shelter_sign_up() -> Any:
    if not supabase_client:
        return render_template(
            "auth_portal.html",
            error=_supabase_unavailable_message(),
            info=None,
        ), 500
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    first_name = (request.form.get("first_name") or "").strip()
    last_name = (request.form.get("last_name") or "").strip()
    if not email or not password:
        return render_template("auth_portal.html", error="Email and password are required.", info=None), 400
    try:
        supabase_client.auth.sign_up(
            {
                "email": email,
                "password": password,
                "options": {"data": {"first_name": first_name, "last_name": last_name}},
            }
        )
        return render_template(
            "auth_portal.html",
            error=None,
            info="Sign-up successful. If email confirmation is enabled, verify your email, then sign in.",
        )
    except Exception as e:
        return render_template("auth_portal.html", error=f"Sign-up failed: {e}", info=None), 400


@app.get("/shelter/logout")
def shelter_logout() -> Any:
    session.clear()
    return redirect(url_for("shelter_login"))


@app.get("/shelter")
def shelter_dashboard() -> Any:
    if not supabase_client:
        return redirect(url_for("shelter_login"))

    user, _ = _session_user()
    if not user:
        return redirect(url_for("shelter_login"))

    user_id = getattr(user, "id", None)
    if not user_id:
        session.clear()
        return redirect(url_for("shelter_login"))

    profile = _profile_for_user(user_id)
    role = (profile or {}).get("role")
    is_shelter = role == "shelter"

    animals = []
    error = None
    try:
        result = (
            supabase_client.table("adoptable_animals")
            .select("*")
            .eq("shelter_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        animals = result.data or []
    except Exception as e:
        error = f"Could not load adoptable_animals: {e}"

    return render_template(
        "shelter_dashboard.html",
        user=_serialize_auth_user(user),
        profile=profile,
        role=role,
        is_shelter=is_shelter,
        animals=animals,
        error=error,
    )


@app.get("/devices")
@require_web_session_api
def list_devices():
    effective_cfg = _effective_server_config_for_request()
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    include_unowned = request.args.get("include_unowned", "0") == "1"
    with _db() as conn:
        if include_unowned:
            rows = conn.execute(
                """
                SELECT d.device_id, d.name, d.last_seen, d.force_oob, d.force_low_battery, d.freeze_lat, d.freeze_lng,
                       r.raw_json AS raw_json
                FROM devices d
                LEFT JOIN readings r
                  ON r.id = (
                    SELECT id FROM readings
                    WHERE device_id = d.device_id
                    ORDER BY ts DESC
                    LIMIT 1
                  )
                WHERE d.shelter_user_id = ? OR d.shelter_user_id IS NULL
                ORDER BY d.last_seen DESC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT d.device_id, d.name, d.last_seen, d.force_oob, d.force_low_battery, d.freeze_lat, d.freeze_lng,
                       r.raw_json AS raw_json
                FROM devices d
                LEFT JOIN readings r
                  ON r.id = (
                    SELECT id FROM readings
                    WHERE device_id = d.device_id
                    ORDER BY ts DESC
                    LIMIT 1
                  )
                WHERE d.shelter_user_id = ?
                ORDER BY d.last_seen DESC
                """,
                (user_id,),
            ).fetchall()

    devices_view: list[dict[str, Any]] = []
    for row in rows:
        payload: dict[str, Any] = {}
        if row["raw_json"]:
            try:
                parsed = json.loads(row["raw_json"])
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
        devices_view.append(
            _device_view(
                row["device_id"],
                {
                    "last_seen": float(row["last_seen"]),
                    "data": payload,
                    "name": row["name"],
                    "force_oob": int(row["force_oob"] or 0),
                    "force_low_battery": int(row["force_low_battery"] or 0),
                    "freeze_lat": row["freeze_lat"],
                    "freeze_lng": row["freeze_lng"],
                },
                effective_cfg,
            )
        )

    return jsonify({"devices": devices_view})


@app.get("/config")
@require_web_session_api
def get_config():
    return jsonify({"config": _effective_server_config_for_request()})


@app.post("/config")
@require_web_session_api
def set_config():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        return jsonify({"status": "error", "error": "missing_config"}), 400

    effective_cfg = _effective_server_config_for_request()
    bbox_source = str(effective_cfg.get("gps_bbox_source") or "manual")
    bbox_locked = bbox_source == "shelter_boundary"

    gps_center = cfg.get("gps_center") or {}
    if not isinstance(gps_center, dict):
        gps_center = {}

    gps_bbox = cfg.get("gps_bbox")
    if gps_bbox is not None and not isinstance(gps_bbox, dict):
        gps_bbox = None

    server_config["gps_center"] = {
        "lat": _coerce_float(gps_center.get("lat"), server_config["gps_center"]["lat"]),
        "lng": _coerce_float(gps_center.get("lng"), server_config["gps_center"]["lng"]),
    }
    server_config["gps_radius_m"] = max(
        1.0, _coerce_float(cfg.get("gps_radius_m"),
                           server_config["gps_radius_m"])
    )

    # Optional bbox config (ignored when shelter boundary controls the bbox).
    if not bbox_locked:
        if isinstance(gps_bbox, dict):
            min_lat = _coerce_float(gps_bbox.get("min_lat"), 0.0)
            max_lat = _coerce_float(gps_bbox.get("max_lat"), 0.0)
            min_lng = _coerce_float(gps_bbox.get("min_lng"), 0.0)
            max_lng = _coerce_float(gps_bbox.get("max_lng"), 0.0)
            if min_lat > max_lat:
                min_lat, max_lat = max_lat, min_lat
            if min_lng > max_lng:
                min_lng, max_lng = max_lng, min_lng
            if (max_lat - min_lat) > 0 and (max_lng - min_lng) > 0:
                server_config["gps_bbox"] = {
                    "min_lat": min_lat,
                    "max_lat": max_lat,
                    "min_lng": min_lng,
                    "max_lng": max_lng,
                }
            else:
                server_config["gps_bbox"] = None
        elif gps_bbox is None:
            # Allow explicit clearing: gps_bbox=null
            server_config["gps_bbox"] = None

    gps_mode = str(cfg.get("gps_mode") or server_config.get(
        "gps_mode") or "radius").lower()
    if bbox_locked:
        gps_mode = "bbox"
    if gps_mode not in ("radius", "bbox"):
        gps_mode = "radius"
    server_config["gps_mode"] = gps_mode

    server_config["battery_drain"] = max(
        0, _coerce_int(cfg.get("battery_drain"),
                       server_config["battery_drain"])
    )
    server_config["battery_loop"] = bool(
        cfg.get("battery_loop", server_config["battery_loop"]))

    post_interval_min = _coerce_int(cfg.get("post_interval_min"), int(
        server_config.get("post_interval_min", 1)))
    if post_interval_min not in (1, 5, 15, 30):
        post_interval_min = 1
    server_config["post_interval_min"] = post_interval_min

    gps_check_every_n_posts = _coerce_int(
        cfg.get("gps_check_every_n_posts"),
        int(server_config.get("gps_check_every_n_posts", 1)),
    )
    if gps_check_every_n_posts not in (1, 2, 5):
        gps_check_every_n_posts = 1
    server_config["gps_check_every_n_posts"] = gps_check_every_n_posts

    _save_server_config()
    return jsonify({"status": "ok", "config": _effective_server_config_for_request()})


@app.post("/pairing/start")
@require_web_session_api
def pairing_start():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    expected_name = str(payload.get("expected_name") or "").strip()
    if not expected_name:
        return jsonify({"status": "error", "error": "missing_expected_name"}), 400

    ttl_s = max(30, min(600, _coerce_int(payload.get("ttl_s"), 180)))
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    now = time.time()
    expires_at = now + ttl_s
    claim_token = secrets.token_hex(8)
    with _db() as conn:
        # Best-effort cleanup.
        conn.execute(
            "DELETE FROM pairing_claims WHERE expires_at < ? OR claimed_at IS NOT NULL", (now,))
        conn.execute(
            """
            INSERT INTO pairing_claims(user_id, expected_name, claim_token, created_at, expires_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (user_id, expected_name, claim_token, now, expires_at),
        )
    return jsonify(
        {
            "status": "ok",
            "expected_name": expected_name,
            "claim_token": claim_token,
            "expires_at": expires_at,
        }
    )


@app.post("/data")
def receive_data():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    device_id = str(data.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"status": "error", "error": "missing_device_id"}), 400

    # Backward compatible: name/gps/battery may be missing.
    incoming_name = str(data.get("name") or "").strip()
    incoming_claim_token = str(data.get("claim_token") or "").strip()

    now = time.time()
    notify_tokens: list[str] = []
    notify_kind = ""
    notify_title = ""
    notify_body = ""
    notify_data: dict[str, Any] = {}
    should_notify = False
    force_oob_bool = False
    name = ""
    shelter_user_id: Optional[str] = None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT name, shelter_user_id, force_oob, force_low_battery, freeze_lat, freeze_lng,
                   last_oob, last_notify_ts, last_batt_low, last_batt_notify_ts
            FROM devices
            WHERE device_id = ?
            """,
            (device_id,),
        ).fetchone()
        stored_name = str(row["name"]).strip() if row else ""
        shelter_user_id = str(row["shelter_user_id"]).strip() if (
            row and row["shelter_user_id"]) else None
        force_oob = int(row["force_oob"] or 0) if row else 0
        force_low_battery = int(row["force_low_battery"] or 0) if row else 0
        freeze_lat = float(row["freeze_lat"]) if (
            row and row["freeze_lat"] is not None) else None
        freeze_lng = float(row["freeze_lng"]) if (
            row and row["freeze_lng"] is not None) else None
        prev_oob = int(row["last_oob"]) if (
            row and row["last_oob"] is not None) else None
        last_notify_ts = float(row["last_notify_ts"] or 0.0) if row else 0.0
        prev_batt_low = int(row["last_batt_low"]) if (
            row and row["last_batt_low"] is not None) else None
        last_batt_notify_ts = float(
            row["last_batt_notify_ts"] or 0.0) if row else 0.0
        name = incoming_name or stored_name or _default_name(device_id)
        force_oob_bool = bool(force_oob)

        if not shelter_user_id and incoming_claim_token:
            shelter_user_id = _resolve_pending_claim_user_id_by_token(
                incoming_claim_token, now)
        if not shelter_user_id:
            shelter_user_id = _resolve_pending_claim_user_id(name, now)

        # Upsert device.
        if row:
            conn.execute(
                "UPDATE devices SET name = ?, last_seen = ?, shelter_user_id = COALESCE(shelter_user_id, ?) WHERE device_id = ?",
                (name, now, shelter_user_id, device_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO devices(device_id, name, shelter_user_id, first_seen, last_seen, force_oob, force_low_battery, freeze_lat, freeze_lng)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (device_id, name, shelter_user_id, now, now,
                 force_oob, force_low_battery, None, None),
            )

        if shelter_user_id:
            conn.execute(
                """
                UPDATE pairing_claims
                SET claimed_device_id = ?, claimed_at = COALESCE(claimed_at, ?)
                WHERE user_id = ?
                  AND claimed_device_id IS NULL
                  AND (claim_token = ? OR expected_name = ?)
                """,
                (device_id, now, shelter_user_id, incoming_claim_token, name),
            )

        gps = data.get("gps") if isinstance(data.get("gps"), dict) else {}
        if force_low_battery and freeze_lat is not None and freeze_lng is not None:
            gps = {"lat": freeze_lat, "lng": freeze_lng}
            data["gps"] = gps
        lat = gps.get("lat")
        lng = gps.get("lng")
        try:
            lat_f = float(lat) if lat is not None else None
        except Exception:
            lat_f = None
        try:
            lng_f = float(lng) if lng is not None else None
        except Exception:
            lng_f = None

        # Safety: if force-low is on but freeze point wasn't set yet, lock to first seen point.
        if force_low_battery and freeze_lat is None and freeze_lng is None and lat_f is not None and lng_f is not None:
            conn.execute(
                "UPDATE devices SET freeze_lat = ?, freeze_lng = ? WHERE device_id = ?",
                (lat_f, lng_f, device_id),
            )

        battery = data.get("battery")
        try:
            battery_i = int(battery) if battery is not None else None
        except Exception:
            battery_i = None

        status = data.get("status")
        status_s = str(status) if status is not None else None

        conn.execute(
            """
            INSERT INTO readings(device_id, ts, status, battery, lat, lng, raw_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, now, status_s, battery_i, lat_f, lng_f, json.dumps(data)),
        )

        # Update last_oob + possibly trigger notification.
        geofence_config = _effective_server_config_for_user(shelter_user_id)
        geofence = _compute_geofence(data, geofence_config)
        out = geofence.get("out_of_bounds")
        out_i = 1 if out is True else 0 if out is False else None
        conn.execute(
            "UPDATE devices SET last_oob = ? WHERE device_id = ?",
            (out_i, device_id),
        )

        # Battery low tracking + notify (transition -> low, cooldown).
        # Allow demo forcing from server without changing ESP32 payload.
        effective_battery_i = battery_i
        if force_low_battery:
            forced_value = BATTERY_LOW_THRESHOLD - 1
            if effective_battery_i is None:
                effective_battery_i = forced_value
            else:
                effective_battery_i = min(effective_battery_i, forced_value)

        if effective_battery_i is not None:
            batt_low = 1 if effective_battery_i < BATTERY_LOW_THRESHOLD else 0
        else:
            batt_low = None
        conn.execute(
            "UPDATE devices SET last_batt_low = ? WHERE device_id = ?",
            (batt_low, device_id),
        )

        # Decide whether to notify (priority: OOB, else low battery).
        if out is True and prev_oob != 1 and (now - (last_notify_ts or 0.0) >= NOTIFY_COOLDOWN_S):
            should_notify = True
            reason = "forced" if force_oob_bool else str(
                geofence.get("reason") or "geofence")
            notify_kind = "oob"
            notify_title = "Dog out of bounds"
            notify_body = f"{name} left the safe area ({reason})."
            notify_data = {"device_id": device_id,
                           "name": name, "event": "oob", "reason": reason}
            rows = conn.execute(
                """
                SELECT token FROM fcm_tokens
                WHERE device_id = ? OR device_id IS NULL
                """,
                (device_id,),
            ).fetchall()
            notify_tokens = [str(r["token"]) for r in rows]
            # Reserve the cooldown immediately to avoid duplicates.
            conn.execute(
                "UPDATE devices SET last_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
            )
        elif (
            batt_low == 1
            and prev_batt_low != 1
            and (now - (last_batt_notify_ts or 0.0) >= NOTIFY_COOLDOWN_S)
        ):
            should_notify = True
            notify_kind = "low_battery"
            notify_title = "Low battery"
            notify_body = f"{name} battery is low ({effective_battery_i}%)."
            notify_data = {
                "device_id": device_id,
                "name": name,
                "event": "low_battery",
                "battery": effective_battery_i,
                "forced": bool(force_low_battery),
            }
            rows = conn.execute(
                """
                SELECT token FROM fcm_tokens
                WHERE device_id = ? OR device_id IS NULL
                """,
                (device_id,),
            ).fetchall()
            notify_tokens = [str(r["token"]) for r in rows]
            conn.execute(
                "UPDATE devices SET last_batt_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
            )

    response_config = _effective_server_config_for_user(shelter_user_id)
    response_config["force_oob"] = bool(force_oob)

    if should_notify:
        _record_and_send_notification(
            device_id=device_id,
            kind=notify_kind,
            title=notify_title,
            body=notify_body,
            now=now,
            tokens=notify_tokens,
            data=notify_data,
        )
        # Also bridge into Supabase notifications (Expo push pipeline) for mobile app delivery.
        _send_supabase_notification(
            device_id=device_id,
            category=f"tracker_{notify_kind}",
            title=notify_title,
            message=notify_body,
            route_path="/(shelter)/notifications",
            payload=notify_data,
        )
    return jsonify({"status": "ok", "config": response_config})


@app.post("/device/<device_id>/oob")
@require_web_session_api
def set_device_oob(device_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400
    force = payload.get("force")
    force_oob = 1 if bool(force) else 0
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")

    with _db() as conn:
        row = conn.execute(
            "SELECT device_id FROM devices WHERE device_id = ? AND shelter_user_id = ?",
            (device_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "error": "not_found"}), 404
        conn.execute(
            "UPDATE devices SET force_oob = ? WHERE device_id = ?",
            (force_oob, device_id),
        )

    return jsonify({"status": "ok", "device_id": device_id, "force_oob": bool(force_oob)})


@app.post("/device/<device_id>/low-battery")
@require_web_session_api
def set_device_low_battery(device_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400
    force = payload.get("force")
    force_low_battery = 1 if bool(force) else 0
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")

    with _db() as conn:
        row = conn.execute(
            "SELECT device_id FROM devices WHERE device_id = ? AND shelter_user_id = ?",
            (device_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "error": "not_found"}), 404

        freeze_lat = None
        freeze_lng = None
        if force_low_battery:
            # Lock to latest known location.
            latest = conn.execute(
                """
                SELECT lat, lng
                FROM readings
                WHERE device_id = ? AND lat IS NOT NULL AND lng IS NOT NULL
                ORDER BY ts DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            if latest:
                freeze_lat = latest["lat"]
                freeze_lng = latest["lng"]

        conn.execute(
            "UPDATE devices SET force_low_battery = ?, freeze_lat = ?, freeze_lng = ? WHERE device_id = ?",
            (force_low_battery, freeze_lat, freeze_lng, device_id),
        )

    return jsonify(
        {
            "status": "ok",
            "device_id": device_id,
            "force_low_battery": bool(force_low_battery),
            "freeze_lat": freeze_lat,
            "freeze_lng": freeze_lng,
        }
    )


@app.post("/device/<device_id>/claim")
@require_web_session_api
def claim_device(device_id: str):
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    with _db() as conn:
        row = conn.execute(
            "SELECT shelter_user_id, name FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "error": "not_found"}), 404
        owner_id = str(row["shelter_user_id"]).strip(
        ) if row["shelter_user_id"] else ""
        if owner_id and owner_id != user_id:
            return jsonify({"status": "error", "error": "owned_by_other_shelter"}), 409
        conn.execute(
            "UPDATE devices SET shelter_user_id = ? WHERE device_id = ?",
            (user_id, device_id),
        )

    return jsonify({"status": "ok", "device_id": device_id, "shelter_user_id": user_id})


@app.delete("/device/<device_id>")
@require_web_session_api
def delete_device(device_id: str):
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    with _db() as conn:
        row = conn.execute(
            "SELECT device_id FROM devices WHERE device_id = ? AND shelter_user_id = ?",
            (device_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "error": "not_found"}), 404
        conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))

    return jsonify({"status": "ok", "device_id": device_id})


@app.post("/fcm/register")
def register_fcm_token():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    token = str(payload.get("token") or "").strip()
    device_id = str(payload.get("device_id") or "").strip() or None
    platform = str(payload.get("platform") or "").strip() or None

    if not token:
        return jsonify({"status": "error", "error": "missing_token"}), 400

    now = time.time()
    with _db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO fcm_tokens(token, device_id, platform, created_at, last_used_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                  device_id=excluded.device_id,
                  platform=excluded.platform
                """,
                (token, device_id, platform, now, None),
            )
        except sqlite3.IntegrityError:
            # If a device_id was provided but doesn't exist yet, accept the token as global.
            conn.execute(
                """
                INSERT INTO fcm_tokens(token, device_id, platform, created_at, last_used_at)
                VALUES(?, NULL, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                  device_id=NULL,
                  platform=excluded.platform
                """,
                (token, platform, now, None),
            )

    return jsonify({"status": "ok"})


@app.get("/notifications/status")
@require_web_session_api
def notifications_status():
    return jsonify({"status": "ok", "fcm": _fcm_runtime_status()})


@app.post("/notifications/retry")
@require_web_session_api
def notifications_retry():
    payload = request.get_json(silent=True)
    limit = 50
    if isinstance(payload, dict):
        try:
            limit = int(payload.get("limit", 50))
        except Exception:
            limit = 50
    result = _retry_failed_notifications(limit=limit)
    return jsonify({"status": "ok", **result, "fcm": _fcm_runtime_status()})


@app.post("/notifications/test")
@require_web_session_api
def notifications_test_push():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}

    title = str(payload.get("title") or "Test alert")
    body = str(payload.get("body")
               or "This is a test push notification from Base Station.")
    device_id = str(payload.get("device_id") or "").strip() or None
    include_global = bool(payload.get("include_global", True))
    use_supabase = bool(payload.get("use_supabase", True))

    tokens = _push_tokens_for_test(
        device_id=device_id, include_global=include_global)

    now = time.time()
    sent = 0
    ok_count = 0
    fail_count = 0
    sample_responses: list[str] = []
    for token in tokens:
        sent += 1
        ok, resp = _send_fcm(
            token,
            title,
            body,
            {"event": "test", "device_id": device_id or "",
                "sent_at": str(int(now))},
        )
        if ok:
            ok_count += 1
            with _db() as conn:
                conn.execute(
                    "UPDATE fcm_tokens SET last_used_at = ? WHERE token = ?",
                    (now, token),
                )
        else:
            fail_count += 1
        if len(sample_responses) < 5:
            sample_responses.append(resp)

    supabase_result: dict[str, Any] = {
        "enabled": False, "sent": 0, "failed": 0, "reason": "disabled"}
    if use_supabase:
        supabase_result = _send_supabase_notification(
            category="tracker_test",
            title=title,
            message=body,
            route_path="/(shelter)/notifications",
            payload={"event": "test", "device_id": device_id or "",
                     "sent_at": str(int(now))},
        )

    if sent == 0 and int(supabase_result.get("sent", 0)) == 0:
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "no_recipients",
                    "message": "No FCM tokens and no Supabase target users found.",
                    "fcm": _fcm_runtime_status(),
                    "supabase": supabase_result,
                }
            ),
            400,
        )

    return jsonify(
        {
            "status": "ok",
            "sent": sent,
            "ok_count": ok_count,
            "fail_count": fail_count,
            "target_device_id": device_id,
            "include_global": include_global,
            "use_supabase": use_supabase,
            "responses_sample": sample_responses,
            "supabase": supabase_result,
            "fcm": _fcm_runtime_status(),
        }
    )


@app.get("/history/<device_id>")
@require_web_session_api
def history(device_id: str):
    limit = request.args.get("limit", "200")
    try:
        limit_i = max(1, min(2000, int(limit)))
    except Exception:
        limit_i = 200

    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")

    with _db() as conn:
        owner_row = conn.execute(
            "SELECT shelter_user_id FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if not owner_row or str(owner_row["shelter_user_id"] or "") != user_id:
            return jsonify({"status": "error", "error": "not_found"}), 404

        rows = conn.execute(
            """
            SELECT ts, status, battery, lat, lng, raw_json
            FROM readings
            WHERE device_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (device_id, limit_i),
        ).fetchall()

        drow = conn.execute(
            "SELECT device_id, name, first_seen, last_seen FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()

    if not drow:
        return jsonify({"status": "error", "error": "not_found"}), 404

    points: list[dict[str, Any]] = []
    for r in rows:
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(r["raw_json"])
        except Exception:
            payload = {}
        points.append(
            {
                "ts": float(r["ts"]),
                "status": r["status"],
                "battery": r["battery"],
                "lat": r["lat"],
                "lng": r["lng"],
                "data": payload,
            }
        )

    points.reverse()
    return jsonify(
        {
            "device": {
                "device_id": drow["device_id"],
                "name": drow["name"],
                "first_seen": float(drow["first_seen"]),
                "last_seen": float(drow["last_seen"]),
            },
            "points": points,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
