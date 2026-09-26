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

### Login flow
- `/` now redirects to `/dashboard` after sign in.
- If not signed in yet, you are redirected to `/shelter/login`.
- Dashboard APIs are auth-guarded.

## 2) Upload the ESP32 sketch (Arduino IDE)

### Board
- ESP32 DevKit V1

### Arduino library needed
In Arduino IDE → Library Manager, install:
- `ArduinoJson`

### Upload
Open this sketch and upload it:
- `/Users/macbookair/thesis/basestation_flask/ESP32_Pairing/ESP32_Pairing.ino`

### Optional: A9G UART test sketch
If you want to test A9G module connection first, upload:
- `/Users/macbookair/thesis/basestation_flask/A9G_UART_Test/A9G_UART_Test.ino`

Default UART wiring used by this test:
- ESP32 `GPIO17 (TX2)` -> A9G `RX` (AT UART)
- ESP32 `GPIO16 (RX2)` -> A9G `TX` (AT UART)
- shared `GND`

Open Serial Monitor at `115200` baud, then try these commands:
- `AT` (basic response check)
- `ATE0` (turn off echo)
- `AT+CSQ` (signal quality)
- `AT+GPS=1` (turn GPS on)
- `AT+GPSRD=1` (start NMEA stream)
- `AT+LOCATION=2` (read parsed location; may say `GPS NOT FIX NOW` until lock)
- `AT+GPSRD=0` (stop NMEA stream)
- `AT+GPS=0` (turn GPS off)

GPS lock tips:
- First lock can take a few minutes.
- Test outdoors with clear sky view.
- Weak power or bad antenna placement can cause unstable/no lock.

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

### Shelter-specific ownership (important)
- Devices are now scoped per shelter account.
- When you click **Add Device**, the system creates a short “pairing claim” for your logged-in shelter user.
- When that ESP32 starts posting data, it is auto-bound to your shelter.
- Result: each shelter only sees/manages its own devices and gets notifications for its own devices.

### Reset / change WiFi (ESP32)
If you want to pair again or you typed the wrong WiFi:
- While the ESP32 is ON and running, press and hold **BOOT** for ~2–3 seconds.
- It will clear its saved WiFi config and restart into setup mode again.

## 4) Dog Tracker demo controls (GPS + battery)

The dashboard has controls you can change for the demo:
- GPS bounding box (min/max lat/lng)
- battery drain

These settings are sent to the ESP32 automatically on the next update (the ESP32 reads the response from `POST /data`).
If a shelter boundary exists in Supabase (`shelter_boundaries.polygon_geojson`), dashboard bbox is auto-derived from that boundary and manual bbox fields are locked.

## 5) Notifications (Firebase Cloud Messaging)

The base station can send:
- **Out of bounds** notification (when the GPS first goes outside the box)
- **Low battery** notification (when `battery < 10%`)
- **Tracker disconnected** notification (after three minutes without a report at the default one-minute reporting interval; longer reporting intervals allow the interval plus one minute). The dashboard uses the same timeout and refreshes every two seconds.
- **Tracker back online** notification (when a disconnected tracker reports again)
- **Dog back in bounds** notification (when a previously out-of-bounds tracker returns to the safe area)

Important note: the “Force OOB” button is only a demo toggle. The base station sends a flag to the ESP32, and the ESP32 is the one that starts reporting GPS outside the bounding box (so the data flow stays realistic).
Extra note: “Force Low Batt” is a server-side demo toggle (no ESP32 code change needed). It makes the dashboard/alerts treat the device as low battery for testing notifications.
When Force Low Batt is enabled, the server also freezes that device's location to the last known GPS point, so the map does not keep moving during the low-power demo.

### Setup (recommended, for your current mobile app)
Your app is using **Expo push via Supabase**, so the easiest working path is:

1. Keep your existing Supabase notification trigger setup.
2. In Flask `.env`, set:
   - `SUPABASE_URL=...`
   - `SUPABASE_KEY=...`
   - `SUPABASE_SERVICE_ROLE_KEY=...`  ← important for server-side inserts
3. Flask inserts critical alerts into Supabase notifications; Supabase trigger sends push via Expo.

This is the current default bridge path for alerts.

### Optional direct FCM path (advanced / parallel)
You can still use direct Firebase send from Flask:
- Set:
  - `FCM_SERVICE_ACCOUNT_FILE=/absolute/path/to/service-account.json`
  - Optional: `FCM_PROJECT_ID=<firebase_project_id>`
- Register native FCM tokens to:
  - `POST /fcm/register`
  - body: `{"token":"<fcm_token>","device_id":"<optional>","platform":"android"}`

### Quick test button
Dashboard has **Test Push**:
- sends test alerts
- shows both channels in result (`FCM x/y`, `Supabase n sent`)
