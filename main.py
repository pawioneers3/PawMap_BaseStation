from __future__ import annotations

import time
from typing import Any

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

HEARTBEAT_TIMEOUT_S = 10

# In-memory registry:
# devices = {
#   device_id: {
#     "last_seen": <unix_ts_float>,
#     "data": <payload_dict>,
#     "name": <display_name_str>
#   }
# }
devices: dict[str, dict[str, Any]] = {}

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
    view = [_device_view(did, entry) for did, entry in devices.items()]
    view.sort(key=lambda d: d.get("last_seen") or 0.0, reverse=True)
    return jsonify({"devices": view})


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

    existing = devices.get(device_id) or {}
    stored_name = str(existing.get("name") or "").strip()
    name = incoming_name or stored_name or _default_name(device_id)

    devices[device_id] = {"last_seen": time.time(), "data": data, "name": name}
    return jsonify({"status": "ok", "config": server_config})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
