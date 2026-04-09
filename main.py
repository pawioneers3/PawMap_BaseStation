from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

HEARTBEAT_TIMEOUT_S = 10
DB_PATH = Path(__file__).resolve().with_name("basestation.db")

# Global mock config pushed down to devices (dog tracker demo).
server_config: dict[str, Any] = {
    "gps_center": {"lat": 14.5995, "lng": 120.9842},  # Manila default
    "gps_radius_m": 150.0,
    "battery_drain": 1,
    "battery_loop": True,
}


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
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            )
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


def _load_server_config() -> None:
    global server_config
    with _db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k = ?", ("server_config",)).fetchone()
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


def _device_view(device_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    last_seen = float(entry.get("last_seen", 0.0))
    data = entry.get("data") or {}
    name = entry.get("name") or data.get("name") or _default_name(device_id)
    age_s = max(0.0, time.time() - last_seen) if last_seen else None
    status = "offline"
    if last_seen and age_s is not None and age_s <= HEARTBEAT_TIMEOUT_S:
        status = "online"
    return {
        "device_id": device_id,
        "name": name,
        "last_seen": last_seen,
        "age_s": age_s,
        "status": status,
        "data": data,
    }


@app.get("/")
def index() -> str:
    # Prefill base station IP from how the user accessed this page.
    server_ip = (request.host or "").split(":", 1)[0]
    return render_template("index.html", server_ip=server_ip)


@app.get("/devices")
def list_devices():
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id, d.name, d.last_seen,
                   r.raw_json AS raw_json
            FROM devices d
            LEFT JOIN readings r
              ON r.id = (
                SELECT id FROM readings
                WHERE device_id = d.device_id
                ORDER BY ts DESC
                LIMIT 1
              )
            ORDER BY d.last_seen DESC
            """
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
                {"last_seen": float(row["last_seen"]), "data": payload, "name": row["name"]},
            )
        )

    return jsonify({"devices": devices_view})


@app.get("/config")
def get_config():
    return jsonify({"config": server_config})


@app.post("/config")
def set_config():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        return jsonify({"status": "error", "error": "missing_config"}), 400

    gps_center = cfg.get("gps_center") or {}
    if not isinstance(gps_center, dict):
        gps_center = {}

    server_config["gps_center"] = {
        "lat": _coerce_float(gps_center.get("lat"), server_config["gps_center"]["lat"]),
        "lng": _coerce_float(gps_center.get("lng"), server_config["gps_center"]["lng"]),
    }
    server_config["gps_radius_m"] = max(
        1.0, _coerce_float(cfg.get("gps_radius_m"), server_config["gps_radius_m"])
    )
    server_config["battery_drain"] = max(
        0, _coerce_int(cfg.get("battery_drain"), server_config["battery_drain"])
    )
    server_config["battery_loop"] = bool(cfg.get("battery_loop", server_config["battery_loop"]))

    _save_server_config()
    return jsonify({"status": "ok", "config": server_config})


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

    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT name FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        stored_name = str(row["name"]).strip() if row else ""
        name = incoming_name or stored_name or _default_name(device_id)

        # Upsert device.
        if row:
            conn.execute(
                "UPDATE devices SET name = ?, last_seen = ? WHERE device_id = ?",
                (name, now, device_id),
            )
        else:
            conn.execute(
                "INSERT INTO devices(device_id, name, first_seen, last_seen) VALUES(?, ?, ?, ?)",
                (device_id, name, now, now),
            )

        gps = data.get("gps") if isinstance(data.get("gps"), dict) else {}
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

    return jsonify({"status": "ok", "config": server_config})


@app.get("/history/<device_id>")
def history(device_id: str):
    limit = request.args.get("limit", "200")
    try:
        limit_i = max(1, min(2000, int(limit)))
    except Exception:
        limit_i = 200

    with _db() as conn:
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
