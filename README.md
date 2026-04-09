# ESP32 ↔ Flask Base Station (Dog Tracker Demo)

Local, offline-friendly ESP32 onboarding + telemetry demo:

- Pair ESP32 via **SoftAP provisioning** (`ESP32-XXXX` → `http://192.168.4.1`)
- ESP32 posts telemetry to Flask every **5 seconds**
- Base station persists devices + historical readings in **SQLite**
- Dashboard shows devices + provides **demo controls** (mock GPS/battery) + **history**

## 1) Base Station (Flask)

### Requirements
- Python 3
- `pip` can install `flask`

### Install
```bash
cd /Users/macbookair/thesis/basestation_flask
python3 -m pip install flask
```

### Run
```bash
cd /Users/macbookair/thesis/basestation_flask
python3 main.py
```

Open the dashboard in your browser:
- Use your **LAN IP**, not `localhost` (ESP32 can’t reach `localhost`)
- Example: `http://192.168.1.10:5000/`

### Data storage
- SQLite DB file: `/Users/macbookair/thesis/basestation_flask/basestation.db`
- Tables:
  - `devices` (one row per device)
  - `readings` (append-only historical telemetry)
  - `kv` (stores `server_config` used for ESP32 mock config sync)

### API quick reference
- `POST /data` (ESP32 → server)
  - Responds with `{"status":"ok","config":{...}}` for config sync.
- `GET /devices` (dashboard polling)
  - Returns latest reading per device.
- `GET /history/<device_id>?limit=300`
  - Returns historical points for analytics/history.
- `GET /config` / `POST /config`
  - Dashboard “Dog Tracker Demo Controls”.

## 2) ESP32 Firmware (Arduino)

### Board
- ESP32 DevKit V1 (Arduino framework)

### Libraries
Install in Arduino IDE (Library Manager):
- `ArduinoJson` (required)

### Flash
Open this file in Arduino IDE and upload:
- `/Users/macbookair/thesis/basestation_flask/ESP32_Pairing.ino`

## 3) “Add Device” / Provisioning Flow

This is the intended pairing UX:

1. Run the base station (`python3 main.py`)
2. Open dashboard using **LAN IP** (example `http://192.168.1.10:5000/`)
3. Power on the ESP32
4. ESP32 (unprovisioned) creates an AP like `ESP32-1a2b`
5. In the dashboard, click **Add Device**
6. Follow the modal instructions:
   - Connect your computer to WiFi `ESP32-XXXX`
   - Click **Continue**
   - Enter:
     - WiFi SSID / password (the *real* WiFi you want the ESP32 to join)
     - Base Station IP (the LAN IP you used to open the dashboard)
     - Optional device name (dog name). Blank → auto `Dog-XXXX`
7. ESP32 saves config to NVS and restarts
8. ESP32 joins your WiFi and starts posting to the base station → device appears in the dashboard

### Re-provision / change WiFi
- Hold the ESP32 **BOOT** button (GPIO0) during power-on for ~1 second to clear stored config (NVS).
- Then repeat the “Add Device” flow.

## 4) Dog Tracker Demo Controls

On the dashboard, you can set:
- GPS center (lat/lng)
- GPS radius (meters)
- Battery drain per update
- Battery looping behavior

These settings are saved server-side and returned to devices on the next `POST /data`.

