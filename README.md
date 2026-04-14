# ESP32 + Flask Base Station (Dog Tracker Demo)

This is a simple local IoT demo made for a “dog tracker” style project.

What it does (in plain terms):
- The ESP32 can be paired without editing firmware (it makes its own WiFi hotspot first).
- After pairing, the ESP32 sends data to the base station every ~5 seconds.
- The base station shows the devices in a dashboard and saves history in SQLite.
- For demo purposes, GPS + battery are “fake” (simulated).

## Folder / files
- Flask server: `/Users/macbookair/thesis/basestation_flask/main.py`
- Dashboard HTML: `/Users/macbookair/thesis/basestation_flask/templates/index.html`
- ESP32 sketch: `/Users/macbookair/thesis/basestation_flask/ESP32_Pairing/ESP32_Pairing.ino`
- SQLite DB: `/Users/macbookair/thesis/basestation_flask/basestation.db`

## 1) Run the Base Station (Flask)

### Requirements
- Python 3
- Internet once (to install packages). After that, it can run offline.

### Install packages
```bash
cd /Users/macbookair/thesis/basestation_flask
python3 -m pip install -r requirements.txt
```

### Start the server
```bash
cd /Users/macbookair/thesis/basestation_flask
python3 main.py
```

### Open the dashboard
Important: open it using your **LAN IP**, not `localhost`.

Example:
- `http://192.168.1.10:5000/`

If you open it as `http://localhost:5000/`, the ESP32 will not be able to reach your computer.

## 2) Upload the ESP32 sketch (Arduino IDE)

### Board
- ESP32 DevKit V1

### Arduino library needed
In Arduino IDE → Library Manager, install:
- `ArduinoJson`

### Upload
Open this sketch and upload it:
- `/Users/macbookair/thesis/basestation_flask/ESP32_Pairing/ESP32_Pairing.ino`

## 3) Pair / Add Device (SoftAP provisioning)

This is the “beginner friendly” pairing flow:

1. Run the base station (`python3 main.py`)
2. Open dashboard using LAN IP (example `http://192.168.1.10:5000/`)
3. Power on the ESP32
4. On your laptop, connect to the ESP32 WiFi hotspot like `ESP32-XXXX`
5. In the dashboard, click **Add Device**
6. Click **Continue** and fill in:
   - WiFi SSID + password (the real WiFi you want the ESP32 to join)
   - Base Station IP (the same LAN IP you used in the browser)
   - Optional device name (ex: “Brownie”). If blank, it will auto-name like `Dog-1234`.
7. The ESP32 restarts and connects to your WiFi
8. It should show up in the dashboard within a few seconds

### Reset / change WiFi (ESP32)
If you want to pair again or you typed the wrong WiFi:
- While the ESP32 is ON and running, press and hold **BOOT** for ~2–3 seconds.
- It will clear its saved WiFi config and restart into setup mode again.

## 4) Dog Tracker demo controls (GPS + battery)

The dashboard has controls you can change for the demo:
- GPS bounding box (min/max lat/lng)
- battery drain

These settings are sent to the ESP32 automatically on the next update (the ESP32 reads the response from `POST /data`).

## 5) Notifications (Firebase Cloud Messaging)

The base station can send:
- **Out of bounds** notification (when the GPS first goes outside the box)
- **Low battery** notification (when `battery < 10%`)

Important note: the “Force OOB” button is only a demo toggle. The base station sends a flag to the ESP32, and the ESP32 is the one that starts reporting GPS outside the bounding box (so the data flow stays realistic).

### Setup (server side)
1. Create a Firebase project and enable Cloud Messaging (FCM).
2. Download a **service account JSON** file.
3. Set environment variables before running `main.py`:
   - `FCM_SERVICE_ACCOUNT_FILE=/absolute/path/to/service-account.json`
   - Optional: `FCM_PROJECT_ID=<firebase_project_id>` (usually read from the JSON)

If you don’t set these, the system will still work, but it won’t be able to actually send the push notification.

### Setup (app side)
Your mobile app should register its FCM token to the base station:
- Endpoint: `POST /fcm/register`
- Body:
  - `{"token":"<fcm_token>","device_id":"<optional>","platform":"android"}`

If `device_id` is omitted, that token will receive notifications for all devices.
