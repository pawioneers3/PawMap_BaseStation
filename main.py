from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
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
    from supabase import Client, create_client
except Exception:
    Client = Any
    create_client = None

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_TRACKER_INGEST_URL = (
    os.environ.get("SUPABASE_TRACKER_INGEST_URL")
    or (f"{SUPABASE_URL.rstrip('/')}/functions/v1/tracker-ingest" if SUPABASE_URL else "")
)
supabase_client: Optional[Client] = None
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
NOTIFY_COOLDOWN_S = 30
BATTERY_LOW_THRESHOLD = 20
BATTERY_EMPTY_THRESHOLD = 0
OFFLINE_MONITOR_INTERVAL_S = 15


server_config: dict[str, Any] = {
    "gps_center": {"lat": 14.5995, "lng": 120.9842},
    "gps_radius_m": 150.0,

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
    "force_wait_for_gps_lock": False,
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


def _is_low_battery(value: Optional[int]) -> bool:
    return value is not None and value <= BATTERY_LOW_THRESHOLD


def _offline_timeout_s(cfg: Optional[dict[str, Any]] = None) -> int:
    active_cfg = cfg or server_config
    post_interval_min = _coerce_int(active_cfg.get("post_interval_min"), 1)
    if post_interval_min not in (1, 5, 15, 30):
        post_interval_min = 1
    return int((post_interval_min + 5) * 60)


def _is_empty_battery(value: Optional[int]) -> bool:
    return value is not None and value <= BATTERY_EMPTY_THRESHOLD


def _battery_health(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    if _is_empty_battery(value):
        return "empty"
    if _is_low_battery(value):
        return "low"
    return "good"


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
                last_batt_notify_ts REAL NOT NULL DEFAULT 0,
                last_offline INTEGER NOT NULL DEFAULT 0,
                last_offline_notify_ts REAL NOT NULL DEFAULT 0,
                last_recovery_notify_ts REAL NOT NULL DEFAULT 0,
                gps_waiting INTEGER NOT NULL DEFAULT 0,
                gps_wait_reason TEXT,
                gps_wait_attempts INTEGER NOT NULL DEFAULT 0,
                gps_wait_updated_at REAL,
                gps_debug_json TEXT
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
                replace_existing INTEGER NOT NULL DEFAULT 0,
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
                "ALTER TABLE pairing_claims ADD COLUMN replace_existing INTEGER NOT NULL DEFAULT 0")
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
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN last_offline INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN last_offline_notify_ts REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN last_recovery_notify_ts REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN gps_waiting INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN gps_wait_reason TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN gps_wait_attempts INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN gps_wait_updated_at REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN gps_debug_json TEXT")
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


def _pending_pairing_claim(
    *,
    claim_token: str = "",
    expected_name: str = "",
    now_ts: float,
) -> Optional[dict[str, Any]]:
    if not claim_token and not expected_name:
        return None
    with _db() as conn:
        if claim_token:
            row = conn.execute(
                """
                SELECT id, user_id, expected_name, replace_existing
                FROM pairing_claims
                WHERE claim_token = ?
                  AND claimed_at IS NULL
                  AND expires_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (claim_token, now_ts),
            ).fetchone()
            if row:
                return {
                    "id": int(row["id"]),
                    "user_id": str(row["user_id"]),
                    "expected_name": str(row["expected_name"] or ""),
                    "replace_existing": bool(row["replace_existing"] or 0),
                }
        if expected_name:
            row = conn.execute(
                """
                SELECT id, user_id, expected_name, replace_existing
                FROM pairing_claims
                WHERE expected_name = ?
                  AND claimed_at IS NULL
                  AND expires_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (expected_name, now_ts),
            ).fetchone()
            if row:
                return {
                    "id": int(row["id"]),
                    "user_id": str(row["user_id"]),
                    "expected_name": str(row["expected_name"] or ""),
                    "replace_existing": bool(row["replace_existing"] or 0),
                }
    return None


def _mark_pairing_claim_claimed(claim_id: int, device_id: str, now_ts: float) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE pairing_claims SET claimed_at = ?, claimed_device_id = ? WHERE id = ?",
            (now_ts, device_id, claim_id),
        )


def _pending_claim_user_id(
    *,
    claim_token: str = "",
    expected_name: str = "",
    now_ts: float,
) -> Optional[str]:
    if not claim_token and not expected_name:
        return None
    with _db() as conn:
        if claim_token:
            row = conn.execute(
                """
                SELECT user_id
                FROM pairing_claims
                WHERE claim_token = ?
                  AND claimed_at IS NULL
                  AND expires_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (claim_token, now_ts),
            ).fetchone()
            if row and row["user_id"]:
                return str(row["user_id"])
        if expected_name:
            row = conn.execute(
                """
                SELECT user_id
                FROM pairing_claims
                WHERE expected_name = ?
                  AND claimed_at IS NULL
                  AND expires_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (expected_name, now_ts),
            ).fetchone()
            if row and row["user_id"]:
                return str(row["user_id"])
    return None


def _reset_device_tracking_state(conn: sqlite3.Connection, device_id: str, now_ts: float) -> None:
    conn.execute("DELETE FROM readings WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM notifications WHERE device_id = ?", (device_id,))
    conn.execute(
        """
        UPDATE devices
        SET first_seen = ?,
            last_oob = NULL,
            last_notify_ts = 0,
            last_batt_low = NULL,
            last_batt_notify_ts = 0,
            last_offline = 0,
            last_offline_notify_ts = 0,
            last_recovery_notify_ts = 0,
            force_oob = 0,
            force_low_battery = 0,
            freeze_lat = NULL,
            freeze_lng = NULL
        WHERE device_id = ?
        """,
        (now_ts, device_id),
    )


def _pairing_name_conflicts(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    expected_name: str,
    exclude_device_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    timeout_s = _offline_timeout_s()
    params: list[Any] = [user_id, expected_name]
    sql = """
        SELECT device_id, name, last_seen
        FROM devices
        WHERE shelter_user_id = ?
          AND LOWER(name) = LOWER(?)
    """
    if exclude_device_id:
        sql += " AND device_id <> ?"
        params.append(exclude_device_id)
    sql += " ORDER BY last_seen DESC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    conflicts: list[dict[str, Any]] = []
    now_ts = time.time()
    for row in rows:
        last_seen = float(row["last_seen"] or 0.0)
        age_s = max(0.0, now_ts - last_seen) if last_seen else None
        status = "online" if last_seen and age_s is not None and age_s <= timeout_s else "offline"
        conflicts.append(
            {
                "device_id": str(row["device_id"]),
                "name": str(row["name"] or ""),
                "last_seen": last_seen,
                "status": status,
            }
        )
    return conflicts


def _delete_supabase_tracker_records(device_id: str) -> dict[str, Any]:
    client = supabase_admin_client
    if not client:
        return {"enabled": False, "ok": False, "reason": "missing_service_role_client"}
    try:
        result = client.table("animal_locations").delete().eq(
            "tracker_id", device_id).execute()
        rows = result.data if isinstance(result.data, list) else []
        return {"enabled": True, "ok": True, "deleted_rows": len(rows)}
    except Exception as exc:
        return {"enabled": True, "ok": False, "reason": str(exc)[:500]}


def _rename_local_tracker_records(conn: sqlite3.Connection, device_id: str, name: str) -> None:
    conn.execute(
        "UPDATE devices SET name = ? WHERE device_id = ?",
        (name, device_id),
    )
    rows = conn.execute(
        "SELECT id, raw_json FROM readings WHERE device_id = ?",
        (device_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["raw_json"] or "{}"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload["name"] = name
        conn.execute(
            "UPDATE readings SET raw_json = ? WHERE id = ?",
            (json.dumps(payload), row["id"]),
        )


def _rename_supabase_tracker_records(device_id: str, name: str) -> dict[str, Any]:
    client = supabase_admin_client
    if not client:
        return {"enabled": False, "ok": False, "reason": "missing_service_role_client"}
    if not device_id or not name:
        return {"enabled": True, "ok": False, "reason": "missing_device_id_or_name"}
    updated_geofence_rows = 0
    geofence_failed = 0
    try:
        select_result = (
            client.table("animal_locations")
            .select("id, geofence")
            .eq("tracker_id", device_id)
            .execute()
        )
        for row in select_result.data or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            geofence = row.get("geofence")
            if isinstance(geofence, str):
                try:
                    geofence = json.loads(geofence)
                except Exception:
                    geofence = {}
            if not isinstance(geofence, dict):
                geofence = {}
            updated_geofence = _geofence_with_tracker_meta(
                geofence,
                device_id=device_id,
                name=name,
                shelter_user_id=_row_owner_user_id(row),
                gps_source=str(_row_tracker_meta(row).get("gps_source") or ""),
                force_oob=bool(_row_tracker_meta(
                    row).get("force_oob") or False),
                force_low_battery=bool(_row_tracker_meta(
                    row).get("force_low_battery") or False),
                freeze_lat=_row_tracker_meta(row).get("freeze_lat"),
                freeze_lng=_row_tracker_meta(row).get("freeze_lng"),
            )
            try:
                (
                    client.table("animal_locations")
                    .update({"geofence": updated_geofence})
                    .eq("id", row["id"])
                    .execute()
                )
                updated_geofence_rows += 1
            except Exception:
                geofence_failed += 1
    except Exception:
        geofence_failed += 1

    try:
        result = (
            client.table("animal_locations")
            .update({"animal_id": name, "name": name})
            .eq("tracker_id", device_id)
            .execute()
        )
        rows = result.data if isinstance(result.data, list) else []
        notifications_result = _rename_supabase_notification_records(
            device_id, name)
        return {
            "enabled": True,
            "ok": True,
            "updated_rows": len(rows),
            "updated_geofence_rows": updated_geofence_rows,
            "geofence_failed": geofence_failed,
            "notifications": notifications_result,
            "mode": "with_name",
        }
    except Exception as enriched_error:
        try:
            result = (
                client.table("animal_locations")
                .update({"animal_id": name})
                .eq("tracker_id", device_id)
                .execute()
            )
            rows = result.data if isinstance(result.data, list) else []
            notifications_result = _rename_supabase_notification_records(
                device_id, name)
            return {
                "enabled": True,
                "ok": True,
                "updated_rows": len(rows),
                "updated_geofence_rows": updated_geofence_rows,
                "geofence_failed": geofence_failed,
                "notifications": notifications_result,
                "mode": "animal_id_only",
                "enriched_error": str(enriched_error)[:500],
            }
        except Exception as exc:
            return {
                "enabled": True,
                "ok": False,
                "reason": str(exc)[:500],
                "updated_geofence_rows": updated_geofence_rows,
                "geofence_failed": geofence_failed,
                "enriched_error": str(enriched_error)[:500],
            }


def _rename_supabase_notification_records(device_id: str, name: str) -> dict[str, Any]:
    client = supabase_admin_client
    if not client:
        return {"enabled": False, "ok": False, "reason": "missing_service_role_client"}
    updated = 0
    failed = 0
    try:
        result = (
            client.table("notifications")
            .select("id, message, payload")
            .contains("payload", {"device_id": device_id})
            .execute()
        )
    except Exception as exc:
        try:
            result = (
                client.table("notifications")
                .select("id, message, payload")
                .contains("payload", {"tracker_id": device_id})
                .execute()
            )
        except Exception as fallback_exc:
            return {
                "enabled": True,
                "ok": False,
                "reason": str(fallback_exc)[:500],
                "primary_error": str(exc)[:500],
                "updated_rows": 0,
            }

    for row in result.data or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload_device_id = str(
            payload.get("device_id")
            or payload.get("deviceId")
            or payload.get("tracker_id")
            or payload.get("trackerId")
            or ""
        ).strip()
        if payload_device_id != device_id:
            continue

        old_name = str(
            payload.get("name")
            or payload.get("device_name")
            or payload.get("deviceName")
            or payload.get("animal_id")
            or payload.get("animalId")
            or ""
        ).strip()
        updated_payload = dict(payload)
        updated_payload["name"] = name
        updated_payload["animal_id"] = name
        message = row.get("message")
        updated_message = message
        if isinstance(message, str) and old_name and old_name != name:
            updated_message = message.replace(old_name, name)

        try:
            (
                client.table("notifications")
                .update({"payload": updated_payload, "message": updated_message})
                .eq("id", row["id"])
                .execute()
            )
            updated += 1
        except Exception:
            failed += 1

    return {"enabled": True, "ok": failed == 0, "updated_rows": updated, "failed": failed}


def _parse_supabase_ts(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _row_geofence(row: dict[str, Any]) -> dict[str, Any]:
    geofence = row.get("geofence")
    if isinstance(geofence, str):
        try:
            geofence = json.loads(geofence)
        except Exception:
            geofence = {}
    return geofence if isinstance(geofence, dict) else {}


def _row_tracker_meta(row: dict[str, Any]) -> dict[str, Any]:
    geofence = _row_geofence(row)
    meta = geofence.get("tracker_meta")
    return meta if isinstance(meta, dict) else {}


def _row_owner_user_id(row: dict[str, Any]) -> str:
    meta = _row_tracker_meta(row)
    for key in ("shelter_user_id", "user_id", "owner_user_id"):
        value = row.get(key)
        if value:
            return str(value)
        value = meta.get(key)
        if value:
            return str(value)
    return ""


def _row_tracker_name(row: dict[str, Any], tracker_id: str) -> str:
    meta = _row_tracker_meta(row)
    for value in (
        row.get("name"),
        row.get("animal_name"),
        meta.get("name"),
        row.get("animal_id"),
    ):
        if value:
            return str(value)
    return _default_name(tracker_id)


def _geofence_with_tracker_meta(
    geofence: Optional[dict[str, Any]],
    *,
    device_id: str,
    name: str,
    shelter_user_id: Optional[str],
    gps_source: Optional[str],
    force_oob: bool,
    force_low_battery: bool,
    freeze_lat: Optional[float] = None,
    freeze_lng: Optional[float] = None,
) -> dict[str, Any]:
    enriched = dict(geofence or {})
    meta = dict(enriched.get("tracker_meta") or {})
    meta.update(
        {
            "device_id": device_id,
            "name": name,
            "shelter_user_id": shelter_user_id or "",
            "gps_source": gps_source or "",
            "force_oob": bool(force_oob),
            "force_low_battery": bool(force_low_battery),
            "freeze_lat": freeze_lat,
            "freeze_lng": freeze_lng,
        }
    )
    enriched["tracker_meta"] = meta
    return enriched


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


def _tracker_location_payload(
    *,
    device_id: str,
    name: str,
    shelter_user_id: Optional[str],
    lat: Optional[float],
    lng: Optional[float],
    battery: Optional[int],
    effective_battery: Optional[int],
    battery_health: Optional[str],
    battery_low: Optional[bool],
    geofence: Optional[dict[str, Any]],
    gps_source: Optional[str],
    status: Optional[str],
    recorded_ts: float,
    force_oob: bool = False,
    force_low_battery: bool = False,
    freeze_lat: Optional[float] = None,
    freeze_lng: Optional[float] = None,
) -> dict[str, Any]:
    recorded_at = datetime.fromtimestamp(
        recorded_ts, timezone.utc).isoformat().replace("+00:00", "Z")
    enriched_geofence = _geofence_with_tracker_meta(
        geofence,
        device_id=device_id,
        name=name,
        shelter_user_id=shelter_user_id,
        gps_source=gps_source,
        force_oob=force_oob,
        force_low_battery=force_low_battery,
        freeze_lat=freeze_lat,
        freeze_lng=freeze_lng,
    )
    return {
        "device_id": device_id,
        "name": name,
        "shelter_user_id": shelter_user_id,
        "gps": {"lat": lat, "lng": lng},
        "gps_source": gps_source or "",
        "battery": battery,
        "effective_battery": effective_battery,
        "battery_health": battery_health,
        "battery_low": battery_low,
        "geofence": enriched_geofence,
        "status": status,
        "force_oob": bool(force_oob),
        "force_low_battery": bool(force_low_battery),
        "freeze_lat": freeze_lat,
        "freeze_lng": freeze_lng,
        "recorded_at": recorded_at,
    }


def _animal_location_insert_rows(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    gps = payload.get("gps") if isinstance(payload.get("gps"), dict) else {}
    base_row = {
        "tracker_id": payload.get("device_id"),
        "animal_id": payload.get("animal_id") or payload.get("name") or payload.get("device_id"),
        "latitude": gps.get("lat"),
        "longitude": gps.get("lng"),
        "recorded_at": payload.get("recorded_at"),
        "battery": payload.get("battery"),
    }
    enriched_row = {
        **base_row,
        "name": payload.get("name"),
        "shelter_user_id": payload.get("shelter_user_id"),
        "force_oob": payload.get("force_oob"),
        "force_low_battery": payload.get("force_low_battery"),
        "effective_battery": payload.get("effective_battery"),
        "battery_health": payload.get("battery_health"),
        "battery_low": payload.get("battery_low"),
        "status": payload.get("status"),
        "geofence": payload.get("geofence"),
    }
    return base_row, enriched_row


def _insert_supabase_animal_location_direct(payload: dict[str, Any]) -> dict[str, Any]:
    client = supabase_admin_client
    if not client:
        return {"enabled": False, "ok": False, "reason": "missing_service_role_client"}

    gps = payload.get("gps") if isinstance(payload.get("gps"), dict) else {}
    if gps.get("lat") is None or gps.get("lng") is None:
        return {"enabled": True, "ok": False, "reason": "missing_gps"}

    base_row, enriched_row = _animal_location_insert_rows(payload)
    compatible_row = {
        key: value
        for key, value in enriched_row.items()
        if key
        in {
            "tracker_id",
            "animal_id",
            "latitude",
            "longitude",
            "recorded_at",
            "battery",
            "effective_battery",
            "battery_health",
            "battery_low",
            "status",
            "geofence",
        }
    }
    try:
        client.table("animal_locations").insert(enriched_row).execute()
        return {"enabled": True, "ok": True, "path": "direct", "mode": "enriched"}
    except Exception as enriched_error:
        try:
            client.table("animal_locations").insert(compatible_row).execute()
            return {
                "enabled": True,
                "ok": True,
                "path": "direct",
                "mode": "compatible",
                "enriched_error": str(enriched_error)[:500],
            }
        except Exception as compatible_error:
            try:
                client.table("animal_locations").insert(base_row).execute()
                return {
                    "enabled": True,
                    "ok": True,
                    "path": "direct",
                    "mode": "base",
                    "enriched_error": str(enriched_error)[:500],
                    "compatible_error": str(compatible_error)[:500],
                }
            except Exception as base_error:
                return {
                    "enabled": True,
                    "ok": False,
                    "path": "direct",
                    "reason": str(base_error)[:500],
                    "enriched_error": str(enriched_error)[:500],
                    "compatible_error": str(compatible_error)[:500],
                }


def _send_supabase_tracker_heartbeat(
    *,
    device_id: str,
    name: str,
    shelter_user_id: Optional[str],
    lat: Optional[float],
    lng: Optional[float],
    battery: Optional[int],
    effective_battery: Optional[int],
    battery_health: Optional[str],
    battery_low: Optional[bool],
    geofence: Optional[dict[str, Any]],
    gps_source: Optional[str],
    status: Optional[str],
    recorded_ts: float,
    force_oob: bool = False,
    force_low_battery: bool = False,
    freeze_lat: Optional[float] = None,
    freeze_lng: Optional[float] = None,
) -> dict[str, Any]:
    if lat is None or lng is None:
        return {"enabled": True, "ok": False, "reason": "missing_gps"}

    payload = _tracker_location_payload(
        device_id=device_id,
        name=name,
        shelter_user_id=shelter_user_id,
        lat=lat,
        lng=lng,
        battery=battery,
        effective_battery=effective_battery,
        battery_health=battery_health,
        battery_low=battery_low,
        geofence=geofence,
        gps_source=gps_source,
        status=status,
        recorded_ts=recorded_ts,
        force_oob=force_oob,
        force_low_battery=force_low_battery,
        freeze_lat=freeze_lat,
        freeze_lng=freeze_lng,
    )

    direct_result = _insert_supabase_animal_location_direct(payload)
    if direct_result.get("ok") is True:
        return {**direct_result, "edge": {"skipped": True, "reason": "direct_insert_ok"}}

    if not SUPABASE_TRACKER_INGEST_URL:
        return {**direct_result, "edge": {"enabled": False, "ok": False, "reason": "missing_ingest_url"}}
    if not SUPABASE_KEY:
        return {**direct_result, "edge": {"enabled": False, "ok": False, "reason": "missing_supabase_key"}}

    try:
        import requests  # type: ignore
    except Exception:
        return {**direct_result, "edge": {"enabled": False, "ok": False, "reason": "missing_requests"}}

    try:
        resp = requests.post(
            SUPABASE_TRACKER_INGEST_URL,
            headers={
                "Content-Type": "application/json",
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            json=payload,
            timeout=6,
        )
        ok = 200 <= resp.status_code < 300
        return {
            "enabled": True,
            "ok": ok,
            "path": "edge",
            "direct": direct_result,
            "status_code": resp.status_code,
            "response": resp.text[:500],
        }
    except Exception as e:
        return {**direct_result, "edge": {"enabled": True, "ok": False, "reason": f"exception:{e!r}"}}


def _latest_supabase_tracker_row(device_id: str) -> Optional[dict[str, Any]]:
    client = supabase_admin_client or supabase_client
    if not client:
        return None
    try:
        result = (
            client.table("animal_locations")
            .select("*")
            .eq("tracker_id", device_id)
            .order("recorded_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        row = rows[0] if rows else None
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def _insert_supabase_control_snapshot(
    *,
    device_id: str,
    name: str,
    shelter_user_id: str,
    force_oob: bool,
    force_low_battery: bool,
    freeze_lat: Optional[float] = None,
    freeze_lng: Optional[float] = None,
) -> dict[str, Any]:
    now_ts = time.time()
    lat: Optional[float] = None
    lng: Optional[float] = None
    battery: Optional[int] = None
    status = "unknown"
    geofence: dict[str, Any] = {}
    gps_source = "supabase"

    latest = _latest_supabase_tracker_row(device_id)
    if latest:
        latest_name = _row_tracker_name(latest, device_id)
        if latest_name and latest_name != name:
            _rename_supabase_tracker_records(device_id, name)
        try:
            lat = float(latest.get("latitude")) if latest.get(
                "latitude") is not None else None
        except Exception:
            lat = None
        try:
            lng = float(latest.get("longitude")) if latest.get(
                "longitude") is not None else None
        except Exception:
            lng = None
        try:
            battery = int(latest.get("battery")) if latest.get(
                "battery") is not None else None
        except Exception:
            battery = None
        status = str(latest.get("status") or status)
        geofence = _row_geofence(latest)
        gps_source = str(_row_tracker_meta(latest).get("gps_source") or gps_source)

    if lat is None or lng is None:
        with _db() as conn:
            local = conn.execute(
                """
                SELECT lat, lng, battery, status
                FROM readings
                WHERE device_id = ? AND lat IS NOT NULL AND lng IS NOT NULL
                ORDER BY ts DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            if local:
                lat = float(local["lat"]) if local["lat"] is not None else None
                lng = float(local["lng"]) if local["lng"] is not None else None
                battery = int(
                    local["battery"]) if local["battery"] is not None else battery
                status = str(local["status"] or status)

    if force_oob:
        geofence["out_of_bounds"] = True
        geofence["in_bounds"] = False
        geofence["reason"] = "forced"

    effective_battery = battery
    if force_low_battery:
        forced_value = BATTERY_LOW_THRESHOLD - 1
        effective_battery = forced_value if effective_battery is None else min(
            effective_battery, forced_value)

    battery_low = None if effective_battery is None else _is_low_battery(
        effective_battery)
    battery_health = _battery_health(effective_battery)
    geofence_out = geofence.get("out_of_bounds")
    if _is_empty_battery(effective_battery):
        status = "offline"
    elif geofence_out is True:
        status = "out_of_bounds"
    elif geofence_out is False:
        status = "in_bounds"

    return _send_supabase_tracker_heartbeat(
        device_id=device_id,
        name=name,
        shelter_user_id=shelter_user_id,
        lat=lat,
        lng=lng,
        battery=battery,
        effective_battery=effective_battery,
        battery_health=battery_health,
        battery_low=battery_low,
        geofence=geofence,
        gps_source=gps_source,
        status=status,
        recorded_ts=now_ts,
        force_oob=force_oob,
        force_low_battery=force_low_battery,
        freeze_lat=freeze_lat,
        freeze_lng=freeze_lng,
    )


def _sync_supabase_tracker_row_to_local(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    row: dict[str, Any],
) -> Optional[dict[str, Any]]:
    tracker_id = str(row.get("tracker_id") or "").strip()
    if not tracker_id:
        return None

    owner_id = _row_owner_user_id(row)
    if owner_id and owner_id != user_id:
        return None

    meta = _row_tracker_meta(row)
    supabase_name = _row_tracker_name(row, tracker_id)
    last_seen_ts = _parse_supabase_ts(row.get("recorded_at"))
    if last_seen_ts <= 0:
        last_seen_ts = time.time()

    force_oob = bool(row.get("force_oob") or meta.get("force_oob") or False)
    force_low_battery = bool(
        row.get("force_low_battery") or meta.get("force_low_battery") or False
    )
    gps_source = str(meta.get("gps_source") or "supabase")
    freeze_lat = meta.get("freeze_lat")
    freeze_lng = meta.get("freeze_lng")
    try:
        freeze_lat = float(freeze_lat) if freeze_lat is not None else None
    except Exception:
        freeze_lat = None
    try:
        freeze_lng = float(freeze_lng) if freeze_lng is not None else None
    except Exception:
        freeze_lng = None

    existing = conn.execute(
        "SELECT device_id, first_seen, name FROM devices WHERE device_id = ?",
        (tracker_id,),
    ).fetchone()
    name = supabase_name or _default_name(tracker_id)
    if existing:
        conn.execute(
            """
            UPDATE devices
            SET name = ?,
                shelter_user_id = COALESCE(NULLIF(?, ''), shelter_user_id, ?),
                last_seen = MAX(last_seen, ?),
                force_oob = ?,
                force_low_battery = ?,
                freeze_lat = ?,
                freeze_lng = ?
            WHERE device_id = ?
            """,
            (
                name,
                owner_id,
                user_id,
                last_seen_ts,
                1 if force_oob else 0,
                1 if force_low_battery else 0,
                freeze_lat,
                freeze_lng,
                tracker_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO devices(device_id, name, shelter_user_id, first_seen, last_seen, force_oob, force_low_battery, freeze_lat, freeze_lng)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tracker_id,
                name,
                owner_id or user_id,
                last_seen_ts,
                last_seen_ts,
                1 if force_oob else 0,
                1 if force_low_battery else 0,
                freeze_lat,
                freeze_lng,
            ),
        )

    lat = row.get("latitude")
    lng = row.get("longitude")
    try:
        lat_f = float(lat) if lat is not None else None
    except Exception:
        lat_f = None
    try:
        lng_f = float(lng) if lng is not None else None
    except Exception:
        lng_f = None
    try:
        battery_i = int(row.get("battery")) if row.get(
            "battery") is not None else None
    except Exception:
        battery_i = None

    raw_json = {
        "device_id": tracker_id,
        "name": name,
        "battery": battery_i,
        "effective_battery": row.get("effective_battery"),
        "battery_low": row.get("battery_low"),
        "battery_health": row.get("battery_health"),
        "status": row.get("status"),
        "gps": {"lat": lat_f, "lng": lng_f},
        "gps_source": gps_source,
        "geofence": _row_geofence(row),
    }
    duplicate = conn.execute(
        "SELECT id FROM readings WHERE device_id = ? AND ABS(ts - ?) < 0.001 LIMIT 1",
        (tracker_id, last_seen_ts),
    ).fetchone()
    if not duplicate:
        conn.execute(
            """
            INSERT INTO readings(device_id, ts, status, battery, lat, lng, raw_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tracker_id,
                last_seen_ts,
                str(row.get("status") or ""),
                battery_i,
                lat_f,
                lng_f,
                json.dumps(raw_json),
            ),
        )

    return {
        "device_id": tracker_id,
        "name": name,
        "last_seen": last_seen_ts,
        "data": raw_json,
        "force_oob": force_oob,
        "force_low_battery": force_low_battery,
        "freeze_lat": freeze_lat,
        "freeze_lng": freeze_lng,
    }


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
        "critical_events": ["oob", "low_battery", "offline"],
        "recovery_events": ["online", "in_bounds"],
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


def _scan_and_notify_offline_devices(now: Optional[float] = None) -> int:
    now_ts = time.time() if now is None else now
    offline_before = now_ts - _offline_timeout_s()
    pending: list[dict[str, Any]] = []

    with _db() as conn:
        rows = conn.execute(
            """
            SELECT device_id, name, last_seen, last_offline, last_offline_notify_ts
            FROM devices
            WHERE last_seen > 0
              AND last_seen <= ?
              AND COALESCE(last_offline, 0) != 1
              AND (? - COALESCE(last_offline_notify_ts, 0)) >= ?
            """,
            (offline_before, now_ts, NOTIFY_COOLDOWN_S),
        ).fetchall()

        for row in rows:
            device_id = str(row["device_id"])
            name = str(row["name"] or _default_name(device_id))
            last_seen = float(row["last_seen"] or 0.0)
            age_s = max(0, int(now_ts - last_seen)) if last_seen else None
            title = "Tracker disconnected"
            if age_s is not None:
                body = (
                    f"{name} has been offline for {age_s}s. "
                    "Check battery, Wi-Fi, or location."
                )
            else:
                body = f"{name} is offline. Check battery, Wi-Fi, or location."
            data = {
                "device_id": device_id,
                "name": name,
                "event": "offline",
                "last_seen": int(last_seen),
                "offline_for_s": age_s if age_s is not None else "",
            }
            token_rows = conn.execute(
                """
                SELECT token FROM fcm_tokens
                WHERE device_id = ? OR device_id IS NULL
                """,
                (device_id,),
            ).fetchall()
            pending.append(
                {
                    "device_id": device_id,
                    "title": title,
                    "body": body,
                    "data": data,
                    "tokens": [str(r["token"]) for r in token_rows],
                }
            )
            conn.execute(
                """
                UPDATE devices
                SET last_offline = 1,
                    last_offline_notify_ts = ?
                WHERE device_id = ?
                """,
                (now_ts, device_id),
            )

    for item in pending:
        _record_and_send_notification(
            device_id=str(item["device_id"]),
            kind="offline",
            title=str(item["title"]),
            body=str(item["body"]),
            now=now_ts,
            tokens=item["tokens"],
            data=item["data"],
        )
        _send_supabase_notification(
            device_id=str(item["device_id"]),
            category="tracker_offline",
            title=str(item["title"]),
            message=str(item["body"]),
            route_path="/(shelter)/notifications",
            payload=item["data"],
        )

    return len(pending)


_offline_monitor_started = False
_offline_monitor_lock = threading.Lock()


def _offline_alert_monitor_loop() -> None:
    while True:
        try:
            _scan_and_notify_offline_devices()
        except Exception:
            pass
        time.sleep(OFFLINE_MONITOR_INTERVAL_S)


def _start_offline_alert_monitor() -> None:
    global _offline_monitor_started
    with _offline_monitor_lock:
        if _offline_monitor_started:
            return
        _offline_monitor_started = True
        thread = threading.Thread(
            target=_offline_alert_monitor_loop,
            name="offline-alert-monitor",
            daemon=True,
        )
        thread.start()


@app.before_request
def _ensure_offline_alert_monitor_running() -> None:
    _start_offline_alert_monitor()


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
    timeout_s = _offline_timeout_s(active_cfg)

    last_seen = float(entry.get("last_seen", 0.0))
    data = entry.get("data") or {}
    name = entry.get("name") or data.get("name") or _default_name(device_id)

    force_oob = bool(entry.get("force_oob") or False)
    force_low_battery = bool(entry.get("force_low_battery") or False)
    freeze_lat = entry.get("freeze_lat")
    freeze_lng = entry.get("freeze_lng")

    age_s = max(0.0, time.time() - last_seen) if last_seen else None

    if last_seen and age_s is not None and age_s <= timeout_s:
        status = "online"
    else:
        status = "offline"

    battery_value: Optional[int] = None
    for key in ("effective_battery", "battery"):
        try:
            raw_battery = data.get(key)
            if raw_battery is not None:
                battery_value = int(raw_battery)
                break
        except Exception:
            continue
    if _is_empty_battery(battery_value):
        status = "offline"
    elif str(data.get("status") or "").strip().lower() == "offline":
        status = "offline"

    return {
        "device_id": device_id,
        "name": name,
        "last_seen": last_seen,
        "age_s": age_s,
        "status": status,
        "data": data,
        "geofence": (
            {
                "out_of_bounds": True,
                "in_bounds": False,
                "reason": "forced",
            }
            if force_oob
            else _compute_geofence(data, geofence_config)
        ),
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
    server_ip = (request.host or "").split(":", 1)[0]
    user = getattr(request, "user", None)
    user_id = getattr(user, "id", None)

    profile = _profile_for_user(user_id) if user_id else None
    shelter_name = (
        (profile or {}).get("shelter_name")
        or (profile or {}).get("organization_name")
        or (profile or {}).get("name")
        or "Shelter"
    )

    return render_template(
        "index.html",
        server_ip=server_ip,
        user=_serialize_auth_user(user),
        shelter_name=shelter_name,
    )


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
    shelter_name = (
        (profile or {}).get("shelter_name")
        or (profile or {}).get("organization_name")
        or (profile or {}).get("name")
        or "Shelter"
    )

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
        shelter_name=shelter_name,
    )


@app.get("/devices")
@require_web_session_api
def list_devices():
    if not supabase_client:
        return jsonify({
            "devices": [],
            "source": "supabase",
            "error": "supabase_unavailable"
        })

    effective_cfg = _effective_server_config_for_request()

    try:
        result = (
            supabase_client
            .table("animal_locations")
            .select("*")
            .order("recorded_at", desc=True)
            .limit(100)
            .execute()
        )
        rows = result.data or []
    except Exception as e:
        return jsonify({
            "devices": [],
            "source": "supabase",
            "error": str(e)
        }), 500

    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    include_unowned = request.args.get(
        "include_unowned") in ("1", "true", "yes")

    latest_by_tracker: dict[str, dict[str, Any]] = {}

    for row in rows:
        tracker_id = str(row.get("tracker_id") or "").strip()
        if not tracker_id:
            continue
        owner_id = _row_owner_user_id(row)
        if owner_id and owner_id != user_id and not include_unowned:
            continue

        if tracker_id not in latest_by_tracker:
            latest_by_tracker[tracker_id] = row

    devices_view = []

    with _db() as conn:
        for tracker_id, row in latest_by_tracker.items():
            entry = _sync_supabase_tracker_row_to_local(
                conn,
                user_id=user_id,
                row=row,
            )
            if not entry:
                continue

            # Supabase is authoritative for tracker identity/name so dashboards
            # on other logged-in devices see the same value. Local state is only
            # used for base-station control flags.
            local = conn.execute(
                """
                SELECT force_oob, force_low_battery, freeze_lat, freeze_lng, last_seen,
                       gps_waiting, gps_wait_reason, gps_wait_attempts, gps_wait_updated_at
                       ,gps_debug_json
                FROM devices
                WHERE device_id = ? AND shelter_user_id = ?
                """,
                (tracker_id, user_id),
            ).fetchone()
            if local:
                entry["force_oob"] = bool(local["force_oob"] or 0)
                entry["force_low_battery"] = bool(
                    local["force_low_battery"] or 0)
                entry["freeze_lat"] = local["freeze_lat"]
                entry["freeze_lng"] = local["freeze_lng"]
                entry["last_seen"] = float(
                    local["last_seen"] or entry["last_seen"])
                entry_data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
                entry_data["gps_waiting"] = bool(local["gps_waiting"] or 0)
                entry_data["gps_wait_reason"] = str(local["gps_wait_reason"] or "")
                entry_data["gps_wait_attempts"] = int(local["gps_wait_attempts"] or 0)
                entry_data["gps_wait_updated_at"] = float(local["gps_wait_updated_at"] or 0.0)
                if local["gps_debug_json"]:
                    try:
                        gps_debug = json.loads(str(local["gps_debug_json"]))
                        if isinstance(gps_debug, dict):
                            entry_data["gps_debug"] = gps_debug
                    except Exception:
                        pass
                entry["data"] = entry_data

            devices_view.append(_device_view(tracker_id, entry, effective_cfg))

    return jsonify({
        "devices": devices_view,
        "source": "supabase"
    })


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
    server_config["force_wait_for_gps_lock"] = bool(
        cfg.get("force_wait_for_gps_lock", server_config.get("force_wait_for_gps_lock", False))
    )

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
    replace_existing = bool(payload.get("replace_existing"))

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
            INSERT INTO pairing_claims(user_id, expected_name, claim_token, replace_existing, created_at, expires_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (user_id, expected_name, claim_token,
             1 if replace_existing else 0, now, expires_at),
        )
    return jsonify(
        {
            "status": "ok",
            "expected_name": expected_name,
            "claim_token": claim_token,
            "replace_existing": replace_existing,
            "expires_at": expires_at,
        }
    )


@app.post("/pairing/check")
@require_web_session_api
def pairing_check():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    expected_name = str(payload.get("expected_name") or "").strip()
    if not expected_name:
        return jsonify({"status": "error", "error": "missing_expected_name"}), 400

    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    with _db() as conn:
        conflicts = _pairing_name_conflicts(
            conn,
            user_id=user_id,
            expected_name=expected_name,
        )

    return jsonify(
        {
            "status": "ok",
            "expected_name": expected_name,
            "has_conflict": bool(conflicts),
            "conflicts": conflicts,
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

    incoming_name = str(data.get("name") or "").strip()
    incoming_claim_token = str(data.get("claim_token") or "").strip()

    now = time.time()
    queued_notifications: list[dict[str, Any]] = []
    force_oob_bool = False
    fresh_pairing = False
    name = ""
    shelter_user_id: Optional[str] = None
    heartbeat_lat: Optional[float] = None
    heartbeat_lng: Optional[float] = None
    heartbeat_battery: Optional[int] = None
    heartbeat_effective_battery: Optional[int] = None
    heartbeat_battery_health: Optional[str] = None
    heartbeat_battery_low: Optional[bool] = None
    heartbeat_geofence: Optional[dict[str, Any]] = None
    heartbeat_status: Optional[str] = None
    pending_claim_id: Optional[int] = None
    expected_claim_name = ""
    replace_existing_pairing = False
    replaced_devices: list[str] = []
    rename_supabase_history = False
    gps_debug_json_text: Optional[str] = None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT name, shelter_user_id, force_oob, force_low_battery, freeze_lat, freeze_lng,
                   last_oob, last_notify_ts, last_batt_low, last_batt_notify_ts,
                   last_offline, last_offline_notify_ts, last_recovery_notify_ts
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
        prev_offline = int(row["last_offline"] or 0) if row else 0
        last_offline_notify_ts = float(
            row["last_offline_notify_ts"] or 0.0) if row else 0.0
        last_recovery_notify_ts = float(
            row["last_recovery_notify_ts"] or 0.0) if row else 0.0
        # For normal heartbeats, keep the server-side name as the source of
        # truth. During a valid pairing claim, the claim's expected name is the
        # new source of truth and overrides the stored name below.
        name = stored_name or incoming_name or _default_name(device_id)
        force_oob_bool = bool(force_oob)
        rename_supabase_history = False

        pending_claim = _pending_pairing_claim(
            claim_token=incoming_claim_token,
            expected_name="",
            now_ts=now,
        )
        if not pending_claim and incoming_name and incoming_name != stored_name:
            pending_claim = _pending_pairing_claim(
                claim_token="",
                expected_name=incoming_name,
                now_ts=now,
            )
        if pending_claim:
            pending_user_id = str(pending_claim["user_id"])
            if not shelter_user_id or pending_user_id == shelter_user_id:
                shelter_user_id = pending_user_id
                fresh_pairing = True
                pending_claim_id = int(pending_claim["id"])
                replace_existing_pairing = bool(
                    pending_claim["replace_existing"])
                expected_claim_name = str(
                    pending_claim.get("expected_name") or "").strip()
                if expected_claim_name:
                    name = expected_claim_name
                    rename_supabase_history = True

        # Upsert device.
        if row:
            conn.execute(
                """
                UPDATE devices
                SET name = ?,
                    last_seen = ?,
                    shelter_user_id = COALESCE(shelter_user_id, ?),
                    last_offline = 0,
                    gps_waiting = 0,
                    gps_wait_reason = NULL
                WHERE device_id = ?
                """,
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

        if pending_claim_id is not None:
            conn.execute(
                "UPDATE pairing_claims SET claimed_at = ?, claimed_device_id = ? WHERE id = ?",
                (now, device_id, pending_claim_id),
            )

        if replace_existing_pairing and shelter_user_id:
            conflicts = _pairing_name_conflicts(
                conn,
                user_id=shelter_user_id,
                expected_name=name,
                exclude_device_id=device_id,
            )
            for conflict in conflicts:
                conflict_device_id = str(conflict["device_id"])
                conn.execute(
                    "DELETE FROM devices WHERE device_id = ?", (conflict_device_id,))
                replaced_devices.append(conflict_device_id)

        def push_tokens_for_device() -> list[str]:
            rows = conn.execute(
                """
                SELECT token FROM fcm_tokens
                WHERE device_id = ? OR device_id IS NULL
                """,
                (device_id,),
            ).fetchall()
            return [str(r["token"]) for r in rows]

        if (
            row
            and not fresh_pairing
            and prev_offline == 1
            and (now - (last_recovery_notify_ts or 0.0) >= NOTIFY_COOLDOWN_S)
        ):
            notify_data = {
                "device_id": device_id,
                "name": name,
                "event": "online",
            }
            queued_notifications.append(
                {
                    "kind": "online",
                    "title": "Tracker back online",
                    "body": f"{name} is connected again.",
                    "data": notify_data,
                    "tokens": push_tokens_for_device(),
                }
            )
            last_recovery_notify_ts = now
            conn.execute(
                "UPDATE devices SET last_recovery_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
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
        gps_source_s = str(data.get("gps_source") or "").strip()
        gps_debug_payload = data.get("gps_debug")
        if isinstance(gps_debug_payload, dict):
            try:
                gps_debug_json_text = json.dumps(gps_debug_payload)
            except Exception:
                gps_debug_json_text = None
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

        # Keep a reference point for "last known location" UI metadata, but do
        # not replace incoming GPS. The tracker can still report live movement
        # while the demo low-battery flag is enabled.
        if force_low_battery and freeze_lat is None and freeze_lng is None and lat_f is not None and lng_f is not None:
            freeze_lat = lat_f
            freeze_lng = lng_f
            conn.execute(
                "UPDATE devices SET freeze_lat = ?, freeze_lng = ? WHERE device_id = ?",
                (lat_f, lng_f, device_id),
            )

        # Persist the canonical server-side name in local history too. The ESP32
        # may still include an old setup name in its heartbeat payload.
        data["name"] = name

        battery = data.get("battery")
        try:
            battery_i = int(battery) if battery is not None else None
        except Exception:
            battery_i = None
        heartbeat_lat = lat_f
        heartbeat_lng = lng_f
        heartbeat_battery = battery_i

        status = data.get("status")
        status_s = str(status) if status is not None else None

        conn.execute(
            """
            INSERT INTO readings(device_id, ts, status, battery, lat, lng, raw_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, now, status_s, battery_i, lat_f, lng_f, json.dumps(data)),
        )
        if gps_debug_json_text is not None:
            conn.execute(
                "UPDATE devices SET gps_debug_json = ? WHERE device_id = ?",
                (gps_debug_json_text, device_id),
            )

        # Update last_oob + possibly trigger notification.
        geofence_config = _effective_server_config_for_user(shelter_user_id)
        geofence = _compute_geofence(data, geofence_config)

        if force_oob_bool:
            geofence["out_of_bounds"] = True
            geofence["in_bounds"] = False
            geofence["reason"] = "forced"

        heartbeat_geofence = geofence
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

        batt_low = 1 if _is_low_battery(
            effective_battery_i) else 0 if effective_battery_i is not None else None
        heartbeat_effective_battery = effective_battery_i
        heartbeat_battery_low = bool(
            batt_low) if batt_low is not None else None
        heartbeat_battery_health = _battery_health(effective_battery_i)
        heartbeat_status = (
            "offline"
            if _is_empty_battery(effective_battery_i)
            else
            "out_of_bounds"
            if out is True
            else "in_bounds"
            if out is False
            else "unknown"
        )
        if _is_empty_battery(effective_battery_i):
            queued_notifications = [
                notification
                for notification in queued_notifications
                if notification.get("kind") != "online"
            ]
            if (
                prev_offline != 1
                and (now - last_offline_notify_ts >= NOTIFY_COOLDOWN_S)
            ):
                notify_data = {
                    "device_id": device_id,
                    "name": name,
                    "event": "offline",
                    "battery": effective_battery_i,
                    "reason": "battery_empty",
                }
                queued_notifications.append(
                    {
                        "kind": "offline",
                        "title": "Tracker disconnected",
                        "body": f"{name} battery is empty. The tracker is offline.",
                        "data": notify_data,
                        "tokens": push_tokens_for_device(),
                    }
                )
            conn.execute(
                "UPDATE devices SET last_offline = 1, last_offline_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
            )
        conn.execute(
            "UPDATE devices SET last_batt_low = ? WHERE device_id = ?",
            (batt_low, device_id),
        )

        if out is True and prev_oob != 1 and (now - (last_notify_ts or 0.0) >= NOTIFY_COOLDOWN_S):
            reason = "forced" if force_oob_bool else str(
                geofence.get("reason") or "geofence")
            notify_data = {"device_id": device_id,
                           "name": name, "event": "oob", "reason": reason}
            queued_notifications.append(
                {
                    "kind": "oob",
                    "title": "Dog out of bounds",
                    "body": f"{name} left the safe area ({reason}).",
                    "data": notify_data,
                    "tokens": push_tokens_for_device(),
                }
            )
            # Reserve the cooldown immediately to avoid duplicates.
            conn.execute(
                "UPDATE devices SET last_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
            )
        elif (
            out is False
            and prev_oob == 1
            and (now - (last_recovery_notify_ts or 0.0) >= NOTIFY_COOLDOWN_S)
        ):
            notify_data = {
                "device_id": device_id,
                "name": name,
                "event": "in_bounds",
            }
            queued_notifications.append(
                {
                    "kind": "in_bounds",
                    "title": "Dog back in bounds",
                    "body": f"{name} is back inside the safe area.",
                    "data": notify_data,
                    "tokens": push_tokens_for_device(),
                }
            )
            last_recovery_notify_ts = now
            conn.execute(
                "UPDATE devices SET last_recovery_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
            )

        if (
            batt_low == 1
            and prev_batt_low != 1
            and (now - (last_batt_notify_ts or 0.0) >= NOTIFY_COOLDOWN_S)
        ):
            notify_data = {
                "device_id": device_id,
                "name": name,
                "event": "low_battery",
                "battery": effective_battery_i,
                "forced": bool(force_low_battery),
            }
            queued_notifications.append(
                {
                    "kind": "low_battery",
                    "title": "Low battery",
                    "body": f"{name} battery is low ({effective_battery_i}%).",
                    "data": notify_data,
                    "tokens": push_tokens_for_device(),
                }
            )
            conn.execute(
                "UPDATE devices SET last_batt_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
            )

    for replaced_device_id in replaced_devices:
        _delete_supabase_tracker_records(replaced_device_id)

    rename_result: dict[str, Any] = {
        "enabled": False, "ok": False, "reason": "not_needed"}
    if rename_supabase_history:
        rename_result = _rename_supabase_tracker_records(device_id, name)

    heartbeat_result = _send_supabase_tracker_heartbeat(
        device_id=device_id,
        name=name,
        shelter_user_id=shelter_user_id,
        lat=heartbeat_lat,
        lng=heartbeat_lng,
        battery=heartbeat_battery,
        effective_battery=heartbeat_effective_battery,
        battery_health=heartbeat_battery_health,
        battery_low=heartbeat_battery_low,
        geofence=heartbeat_geofence,
        gps_source=gps_source_s,
        status=heartbeat_status,
        recorded_ts=now,
        force_oob=bool(force_oob),
        force_low_battery=bool(force_low_battery),
        freeze_lat=freeze_lat,
        freeze_lng=freeze_lng,
    )

    response_config = _effective_server_config_for_user(shelter_user_id)
    response_config["force_oob"] = bool(force_oob)

    for notification in queued_notifications:
        _record_and_send_notification(
            device_id=device_id,
            kind=str(notification["kind"]),
            title=str(notification["title"]),
            body=str(notification["body"]),
            now=now,
            tokens=notification["tokens"],
            data=notification["data"],
        )
        # Also bridge into Supabase notifications (Expo push pipeline) for mobile app delivery.
        _send_supabase_notification(
            device_id=device_id,
            category=f"tracker_{notification['kind']}",
            title=str(notification["title"]),
            message=str(notification["body"]),
            route_path="/(shelter)/notifications",
            payload=notification["data"],
        )
    return jsonify({
        "status": "ok",
        "config": response_config,
        "tracker_ingest": heartbeat_result,
        "tracker_rename": rename_result,
    })


@app.post("/gps_status")
def receive_gps_status():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"status": "error", "error": "missing_device_id"}), 400

    gps_waiting = bool(payload.get("gps_waiting", True))
    gps_wait_reason = str(payload.get("gps_wait_reason") or "").strip()
    gps_wait_attempts = max(0, _coerce_int(payload.get("gps_wait_attempts"), 0))
    gps_debug_payload = payload.get("gps_debug")
    gps_debug_json_text: Optional[str] = None
    if isinstance(gps_debug_payload, dict):
        try:
            gps_debug_json_text = json.dumps(gps_debug_payload)
        except Exception:
            gps_debug_json_text = None
    now = time.time()
    shelter_user_id: Optional[str] = None

    with _db() as conn:
        row = conn.execute(
            "SELECT name, shelter_user_id FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if not row:
            return jsonify({"status": "ok", "config": _effective_server_config_for_user(None)})
        shelter_user_id = str(row["shelter_user_id"]).strip() if row["shelter_user_id"] else None
        conn.execute(
            """
            UPDATE devices
            SET last_seen = ?,
                gps_waiting = ?,
                gps_wait_reason = ?,
                gps_wait_attempts = ?,
                gps_wait_updated_at = ?,
                gps_debug_json = COALESCE(?, gps_debug_json)
            WHERE device_id = ?
            """,
            (now, 1 if gps_waiting else 0, gps_wait_reason, gps_wait_attempts, now, gps_debug_json_text, device_id),
        )

    return jsonify({"status": "ok", "config": _effective_server_config_for_user(shelter_user_id)})


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
    device_name = _default_name(device_id)
    force_low_battery = False
    freeze_lat: Optional[float] = None
    freeze_lng: Optional[float] = None

    rename_supabase_history = False

    with _db() as conn:
        row = conn.execute(
            "SELECT device_id, name, force_low_battery, freeze_lat, freeze_lng FROM devices WHERE device_id = ? AND shelter_user_id = ?",
            (device_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "error": "not_found"}), 404
        device_name = str(row["name"] or device_name)
        force_low_battery = bool(row["force_low_battery"] or 0)
        freeze_lat = float(
            row["freeze_lat"]) if row["freeze_lat"] is not None else None
        freeze_lng = float(
            row["freeze_lng"]) if row["freeze_lng"] is not None else None
        conn.execute(
            "UPDATE devices SET force_oob = ? WHERE device_id = ?",
            (force_oob, device_id),
        )

    supabase_result = _insert_supabase_control_snapshot(
        device_id=device_id,
        name=device_name,
        shelter_user_id=user_id,
        force_oob=bool(force_oob),
        force_low_battery=force_low_battery,
        freeze_lat=freeze_lat,
        freeze_lng=freeze_lng,
    )

    return jsonify({"status": "ok", "device_id": device_id, "force_oob": bool(force_oob), "supabase": supabase_result})


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
    device_name = _default_name(device_id)
    existing_force_oob = False
    effective_battery_i = BATTERY_LOW_THRESHOLD - 1
    should_notify_low_battery = False
    tokens: list[str] = []
    now = time.time()

    with _db() as conn:
        row = conn.execute(
            """
            SELECT device_id, name, force_oob, last_batt_low, last_batt_notify_ts
            FROM devices
            WHERE device_id = ? AND shelter_user_id = ?
            """,
            (device_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "error": "not_found"}), 404
        device_name = str(row["name"] or device_name)
        existing_force_oob = bool(row["force_oob"] or 0)
        prev_batt_low = int(row["last_batt_low"]
                            ) if row["last_batt_low"] is not None else None
        last_batt_notify_ts = float(row["last_batt_notify_ts"] or 0.0)

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
            """
            UPDATE devices
            SET force_low_battery = ?,
                freeze_lat = ?,
                freeze_lng = ?,
                last_batt_low = ?
            WHERE device_id = ?
            """,
            (
                force_low_battery,
                freeze_lat,
                freeze_lng,
                1 if force_low_battery else 0,
                device_id,
            ),
        )

        if (
            force_low_battery
            and prev_batt_low != 1
            and (now - (last_batt_notify_ts or 0.0) >= NOTIFY_COOLDOWN_S)
        ):
            should_notify_low_battery = True
            conn.execute(
                "UPDATE devices SET last_batt_notify_ts = ? WHERE device_id = ?",
                (now, device_id),
            )
            token_rows = conn.execute(
                """
                SELECT token FROM fcm_tokens
                WHERE device_id = ? OR device_id IS NULL
                """,
                (device_id,),
            ).fetchall()
            tokens = [str(r["token"]) for r in token_rows]

    supabase_result = _insert_supabase_control_snapshot(
        device_id=device_id,
        name=device_name,
        shelter_user_id=user_id,
        force_oob=existing_force_oob,
        force_low_battery=bool(force_low_battery),
        freeze_lat=freeze_lat,
        freeze_lng=freeze_lng,
    )

    alert_result: dict[str, Any] = {"sent": False}
    if should_notify_low_battery:
        notify_data = {
            "device_id": device_id,
            "name": device_name,
            "event": "low_battery",
            "battery": effective_battery_i,
            "forced": True,
        }
        _record_and_send_notification(
            device_id=device_id,
            kind="low_battery",
            title="Low battery",
            body=f"{device_name} battery is low ({effective_battery_i}%).",
            now=now,
            tokens=tokens,
            data=notify_data,
        )
        alert_result = _send_supabase_notification(
            device_id=device_id,
            category="tracker_low_battery",
            title="Low battery",
            message=f"{device_name} battery is low ({effective_battery_i}%).",
            route_path="/(shelter)/notifications",
            payload=notify_data,
        )
        alert_result["sent"] = True

    return jsonify(
        {
            "status": "ok",
            "device_id": device_id,
            "force_low_battery": bool(force_low_battery),
            "freeze_lat": freeze_lat,
            "freeze_lng": freeze_lng,
            "supabase": supabase_result,
            "alert": alert_result,
        }
    )


@app.post("/device/<device_id>/claim")
@require_web_session_api
def claim_device(device_id: str):
    payload = request.get_json(silent=True)
    reset_history = bool(payload.get("reset_history")) if isinstance(
        payload, dict) else False
    expected_name = str(payload.get("expected_name") or "").strip() if isinstance(
        payload, dict) else ""
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    current_name = ""
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
        if reset_history:
            _reset_device_tracking_state(conn, device_id, time.time())
        current_name = str(row["name"] or "")
        if expected_name:
            _rename_local_tracker_records(conn, device_id, expected_name)
            current_name = expected_name
        conn.execute(
            "UPDATE devices SET shelter_user_id = ? WHERE device_id = ?",
            (user_id, device_id),
        )

    supabase_result = (
        _rename_supabase_tracker_records(device_id, expected_name)
        if expected_name
        else {"enabled": False, "ok": False, "reason": "not_needed"}
    )

    return jsonify({
        "status": "ok",
        "device_id": device_id,
        "name": current_name,
        "shelter_user_id": user_id,
        "supabase": supabase_result,
    })


@app.post("/device/<device_id>/rename")
@require_web_session_api
def rename_device(device_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    new_name = str(payload.get("name") or "").strip()
    if not new_name:
        return jsonify({"status": "error", "error": "missing_name"}), 400

    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    latest = _latest_supabase_tracker_row(device_id)
    latest_owner_id = _row_owner_user_id(latest) if latest else None
    if latest_owner_id and latest_owner_id != user_id:
        return jsonify({"status": "error", "error": "owned_by_other_shelter"}), 409

    now = time.time()
    force_oob = False
    force_low_battery = False
    freeze_lat: Optional[float] = None
    freeze_lng: Optional[float] = None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT device_id, name, shelter_user_id, first_seen, last_seen,
                   force_oob, force_low_battery, freeze_lat, freeze_lng
            FROM devices
            WHERE device_id = ?
            """,
            (device_id,),
        ).fetchone()
        if row:
            owner_id = str(row["shelter_user_id"] or "").strip()
            if owner_id and owner_id != user_id and not latest_owner_id:
                return jsonify({"status": "error", "error": "owned_by_other_shelter"}), 409
            force_oob = bool(row["force_oob"] or 0)
            force_low_battery = bool(row["force_low_battery"] or 0)
            freeze_lat = float(
                row["freeze_lat"]) if row["freeze_lat"] is not None else None
            freeze_lng = float(
                row["freeze_lng"]) if row["freeze_lng"] is not None else None
            _rename_local_tracker_records(conn, device_id, new_name)
            conn.execute(
                """
                UPDATE devices
                SET shelter_user_id = COALESCE(NULLIF(shelter_user_id, ''), ?)
                WHERE device_id = ?
                """,
                (user_id, device_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO devices(device_id, name, shelter_user_id, first_seen, last_seen)
                VALUES(?, ?, ?, ?, ?)
                """,
                (device_id, new_name, user_id, now, now),
            )

    supabase_result = _rename_supabase_tracker_records(device_id, new_name)
    snapshot_result = _insert_supabase_control_snapshot(
        device_id=device_id,
        name=new_name,
        shelter_user_id=user_id,
        force_oob=force_oob,
        force_low_battery=force_low_battery,
        freeze_lat=freeze_lat,
        freeze_lng=freeze_lng,
    )

    return jsonify({
        "status": "ok",
        "device_id": device_id,
        "name": new_name,
        "supabase": supabase_result,
        "snapshot": snapshot_result,
    })


@app.delete("/device/<device_id>")
@require_web_session_api
def delete_device(device_id: str):
    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    deleted_name = ""
    with _db() as conn:
        row = conn.execute(
            "SELECT device_id, name FROM devices WHERE device_id = ? AND shelter_user_id = ?",
            (device_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "error": "not_found"}), 404
        deleted_name = str(row["name"] or "")
        conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    supabase_result = _delete_supabase_tracker_records(device_id)

    return jsonify(
        {
            "status": "ok",
            "device_id": device_id,
            "name": deleted_name,
            "supabase": supabase_result,
        }
    )


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
        if not owner_row:
            latest = _latest_supabase_tracker_row(device_id)
            if latest:
                _sync_supabase_tracker_row_to_local(
                    conn,
                    user_id=user_id,
                    row=latest,
                )
                owner_row = conn.execute(
                    "SELECT shelter_user_id FROM devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
        if not owner_row or str(owner_row["shelter_user_id"] or "") != user_id:
            return jsonify({"status": "error", "error": "not_found"}), 404

        drow = conn.execute(
            "SELECT device_id, name, first_seen, last_seen FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if not drow:
            return jsonify({"status": "error", "error": "not_found"}), 404

        rows = conn.execute(
            """
            SELECT ts, status, battery, lat, lng, raw_json
            FROM readings
            WHERE device_id = ?
              AND ts >= ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (device_id, float(drow["first_seen"]), limit_i),
        ).fetchall()

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
    supabase_points: list[dict[str, Any]] = []
    if supabase_client:
        try:
            result = (
                supabase_client.table("animal_locations")
                .select("*")
                .eq("tracker_id", device_id)
                .order("recorded_at", desc=False)
                .limit(limit_i)
                .execute()
            )
            for row in result.data or []:
                if not isinstance(row, dict):
                    continue
                owner_id = _row_owner_user_id(row)
                if owner_id and owner_id != user_id:
                    continue
                ts = _parse_supabase_ts(row.get("recorded_at"))
                if ts <= 0:
                    continue
                tracker_id = str(row.get("tracker_id") or device_id)
                name = _row_tracker_name(row, tracker_id)
                try:
                    lat = float(row.get("latitude")) if row.get(
                        "latitude") is not None else None
                except Exception:
                    lat = None
                try:
                    lng = float(row.get("longitude")) if row.get(
                        "longitude") is not None else None
                except Exception:
                    lng = None
                try:
                    battery = int(row.get("battery")) if row.get(
                        "battery") is not None else None
                except Exception:
                    battery = None
                supabase_points.append(
                    {
                        "ts": ts,
                        "status": row.get("status"),
                        "battery": battery,
                        "lat": lat,
                        "lng": lng,
                        "data": {
                            "device_id": tracker_id,
                            "name": name,
                            "battery": battery,
                            "gps": {"lat": lat, "lng": lng},
                            "gps_source": str(_row_tracker_meta(row).get("gps_source") or "supabase"),
                            "geofence": _row_geofence(row),
                        },
                    }
                )
        except Exception:
            supabase_points = []

    if supabase_points:
        merged_by_ts: dict[int, dict[str, Any]] = {}
        # Supabase can contain more historical rows, while local SQLite can
        # have the freshest heartbeat if the cloud sync is delayed. Merge both
        # feeds and prefer local rows for near-identical timestamps.
        for point in supabase_points:
            merged_by_ts[int(float(point["ts"]) * 1000)] = point
        for point in points:
            merged_by_ts[int(float(point["ts"]) * 1000)] = point
        points = sorted(merged_by_ts.values(), key=lambda p: float(p["ts"]))
        points = points[-limit_i:]
        if points:
            drow = {
                "device_id": device_id,
                "name": points[-1]["data"].get("name") or drow["name"],
                "first_seen": points[0]["ts"],
                "last_seen": points[-1]["ts"],
            }

    return jsonify(
        {
            "device": {
                "device_id": drow["device_id"],
                "name": drow["name"],
                "first_seen": float(drow["first_seen"]),
                "last_seen": float(drow["last_seen"]),
            },
            "config": _effective_server_config_for_user(user_id),
            "points": points,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
