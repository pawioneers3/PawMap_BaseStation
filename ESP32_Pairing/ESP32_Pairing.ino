#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#include <ArduinoJson.h>
#include <HardwareSerial.h>

// ----- User-adjustable (optional) -----
static const int RESET_BUTTON_PIN = 0; // BOOT button on many ESP32 DevKit V1 boards
static const unsigned long DEFAULT_POST_INTERVAL_MS = 5000;
static const unsigned long WIFI_RETRY_INTERVAL_MS = 5000;
static const unsigned long WIFI_GIVEUP_MS = 30000;
static const unsigned long RESET_HOLD_MS = 2500;
static const int A9G_RX_PIN = 16; // ESP32 RX2 pin (reads from A9G TX)
static const int A9G_TX_PIN = 17; // ESP32 TX2 pin (writes to A9G RX)
static const unsigned long GPS_BOOT_LOCK_TIMEOUT_MS = 12000;
static const unsigned long GPS_BOOT_POLL_INTERVAL_MS = 1500;
static const unsigned long GPS_CMD_TIMEOUT_MS = 1500;
static const unsigned long GPS_LOCK_RETRY_INTERVAL_MS = 15000;
static const unsigned long GPS_WAIT_STATUS_INTERVAL_MS = 10000;

const long A9G_BAUD_CANDIDATES[] = {115200, 9600, 57600, 38400, 19200};
const size_t A9G_BAUD_COUNT = sizeof(A9G_BAUD_CANDIDATES) / sizeof(A9G_BAUD_CANDIDATES[0]);

// ----- NVS keys -----
static const char *PREF_NS = "cfg";
static const char *K_SSID = "ssid";
static const char *K_PASS = "pass";
static const char *K_SERVER = "server";
static const char *K_NAME = "name";
static const char *K_CLAIM = "claim";

struct Config {
  String ssid;
  String pass;
  String serverIp;
  String name;
  String claimToken;
};

Preferences prefs;
WebServer web(80);
HardwareSerial A9G(2);

enum Mode { MODE_SETUP, MODE_NORMAL };
Mode mode = MODE_SETUP;

Config cfg;
String deviceId;

unsigned long lastPostMs = 0;
unsigned long lastWifiAttemptMs = 0;
unsigned long wifiStartedMs = 0;
unsigned long postIntervalMs = DEFAULT_POST_INTERVAL_MS;
int gpsCheckEveryNPosts = 1;
unsigned long postCounter = 0;

struct MockConfig {
  float centerLat = 14.5995f;
  float centerLng = 120.9842f;
  float radiusM = 150.0f;
  bool useBbox = false;
  float minLat = 0.0f;
  float maxLat = 0.0f;
  float minLng = 0.0f;
  float maxLng = 0.0f;
  bool forceOob = false;
  int batteryDrain = 1;
  bool batteryLoop = true;
};

MockConfig mockCfg;
float gpsLat = 0.0f;
float gpsLng = 0.0f;
bool gpsInited = false;
int batteryPct = 100;
bool batteryInited = false;
bool useRealGps = false;
long a9gActiveBaud = 0;
bool forceWaitForGpsLock = false;
unsigned long lastGpsProbeMs = 0;
unsigned long lastGpsStatusPostMs = 0;
unsigned long gpsWaitAttempts = 0;
String gpsWaitReason = "";

unsigned long resetHoldStartMs = 0;

static void sendCorsHeaders() {
  web.sendHeader("Access-Control-Allow-Origin", "*");
  web.sendHeader("Access-Control-Allow-Methods", "POST, OPTIONS, GET");
  web.sendHeader("Access-Control-Allow-Headers", "Content-Type");
}

static String getDeviceId() {
  // Requirement: String((uint32_t)ESP.getEfuseMac(), HEX);
  uint32_t id32 = (uint32_t)ESP.getEfuseMac();
  String id = String(id32, HEX);
  id.toLowerCase();
  return id;
}

static String apNameFor(const String &id) {
  String suffix = id;
  if (suffix.length() > 4) suffix = suffix.substring(suffix.length() - 4);
  return String("ESP32-") + suffix;
}

static String defaultNameFor(const String &id) {
  String suffix = id;
  if (suffix.length() > 4) suffix = suffix.substring(suffix.length() - 4);
  return String("Dog-") + suffix;
}

static bool loadConfig(Config &out) {
  prefs.begin(PREF_NS, true);
  out.ssid = prefs.getString(K_SSID, "");
  out.pass = prefs.getString(K_PASS, "");
  out.serverIp = prefs.getString(K_SERVER, "");
  out.name = prefs.getString(K_NAME, "");
  out.claimToken = prefs.getString(K_CLAIM, "");
  prefs.end();
  return out.ssid.length() > 0 && out.serverIp.length() > 0;
}

static void saveConfig(const Config &in) {
  prefs.begin(PREF_NS, false);
  prefs.putString(K_SSID, in.ssid);
  prefs.putString(K_PASS, in.pass);
  prefs.putString(K_SERVER, in.serverIp);
  prefs.putString(K_NAME, in.name);
  prefs.putString(K_CLAIM, in.claimToken);
  prefs.end();
}

static void clearConfig() {
  prefs.begin(PREF_NS, false);
  prefs.clear();
  prefs.end();
}

static void handleRoot() {
  sendCorsHeaders();
  String html =
      "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'/>"
      "<title>ESP32 Setup</title></head><body style='font-family:Arial;padding:16px;'>"
      "<h2>ESP32 WiFi Provisioning</h2>"
      "<form method='POST' action='/setup'>"
      "SSID:<br/><input name='ssid' style='width:100%;padding:10px'/><br/><br/>"
      "Password:<br/><input name='password' type='password' style='width:100%;padding:10px'/><br/><br/>"
      "Device Name (optional):<br/><input name='name' style='width:100%;padding:10px' placeholder='Dog name'/><br/><br/>"
      "Server IP:<br/><input name='server_ip' style='width:100%;padding:10px' placeholder='192.168.1.10'/><br/><br/>"
      "<button type='submit' style='padding:10px 12px'>Save</button>"
      "</form>"
      "</body></html>";
  web.send(200, "text/html", html);
}

static void handleOptions() {
  sendCorsHeaders();
  web.send(204);
}

static void handleSetup() {
  sendCorsHeaders();

  String ssid = "";
  String pass = "";
  String serverIp = "";
  String name = "";
  String claimToken = "";

  String contentType = web.header("Content-Type");
  contentType.toLowerCase();
  Serial.printf("[SETUP] /setup Content-Type: %s\n", contentType.c_str());

  if (contentType.indexOf("application/json") >= 0) {
    String body = web.arg("plain");
    Serial.printf("[SETUP] /setup JSON body bytes: %d\n", body.length());
    StaticJsonDocument<512> doc;
    DeserializationError err = deserializeJson(doc, body);
    if (!err) {
      ssid = (const char *)(doc["ssid"] | "");
      pass = (const char *)(doc["password"] | "");
      serverIp = (const char *)(doc["server_ip"] | "");
      if (serverIp.length() == 0) {
        // Accept camelCase too (common in JS).
        serverIp = (const char *)(doc["serverIp"] | "");
      }
      name = (const char *)(doc["name"] | "");
      claimToken = (const char *)(doc["claim_token"] | "");
    } else {
      Serial.printf("[SETUP] JSON parse error: %s\n", err.c_str());
    }
  } else {
    ssid = web.arg("ssid");
    pass = web.arg("password");
    name = web.arg("name");
    claimToken = web.arg("claim_token");
    serverIp = web.arg("server_ip");

    // Some clients forget to set Content-Type; try JSON anyway.
    if (ssid.length() == 0 && web.hasArg("plain")) {
      String body = web.arg("plain");
      body.trim();
      if (body.startsWith("{")) {
        Serial.printf("[SETUP] /setup fallback JSON body bytes: %d\n", body.length());
        StaticJsonDocument<512> doc;
        DeserializationError err = deserializeJson(doc, body);
        if (!err) {
          ssid = (const char *)(doc["ssid"] | "");
          pass = (const char *)(doc["password"] | "");
          serverIp = (const char *)(doc["server_ip"] | "");
          if (serverIp.length() == 0) serverIp = (const char *)(doc["serverIp"] | "");
          name = (const char *)(doc["name"] | "");
          claimToken = (const char *)(doc["claim_token"] | "");
        } else {
          Serial.printf("[SETUP] fallback JSON parse error: %s\n", err.c_str());
        }
      }
    }
  }

  ssid.trim();
  serverIp.trim();
  name.trim();

  Serial.printf("[SETUP] Parsed ssid='%s' server_ip='%s' name='%s'\n", ssid.c_str(), serverIp.c_str(), name.c_str());

  if (ssid.length() == 0 || serverIp.length() == 0) {
    web.send(400, "application/json", "{\"status\":\"error\",\"error\":\"missing_fields\"}");
    return;
  }

  Config newCfg;
  newCfg.ssid = ssid;
  newCfg.pass = pass;
  newCfg.serverIp = serverIp;
  newCfg.name = name.length() ? name : defaultNameFor(deviceId);
  newCfg.claimToken = claimToken;
  saveConfig(newCfg);

  web.send(200, "application/json", "{\"status\":\"ok\"}");
  delay(800);
  ESP.restart();
}

void setupMode() {
  mode = MODE_SETUP;

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_AP);

  String apName = apNameFor(deviceId);
  WiFi.softAP(apName.c_str());

  web.stop();
  web.on("/", HTTP_GET, handleRoot);
  web.on("/setup", HTTP_POST, handleSetup);
  web.on("/setup", HTTP_OPTIONS, handleOptions);
  web.onNotFound([]() {
    sendCorsHeaders();
    web.send(404, "application/json", "{\"status\":\"error\",\"error\":\"not_found\"}");
  });
  web.begin();

  Serial.printf("[SETUP] AP started: %s (http://192.168.4.1)\n", apName.c_str());
}

static void beginNormalMode() {
  mode = MODE_NORMAL;

  web.stop();
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);

  wifiStartedMs = millis();
  lastWifiAttemptMs = 0;
  lastPostMs = 0;
  postCounter = 0;

  Serial.printf("[NORMAL] Connecting to WiFi SSID: %s\n", cfg.ssid.c_str());
}

void normalMode() {
  beginNormalMode();
}

static void ensureWifiConnected() {
  if (WiFi.status() == WL_CONNECTED) return;

  unsigned long now = millis();

  if (wifiStartedMs && (now - wifiStartedMs > WIFI_GIVEUP_MS)) {
    Serial.println("[NORMAL] WiFi connect timed out. Falling back to setup mode.");
    setupMode();
    return;
  }

  if (now - lastWifiAttemptMs < WIFI_RETRY_INTERVAL_MS) return;
  lastWifiAttemptMs = now;

  Serial.println("[NORMAL] WiFi not connected. Retrying...");
  WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
}

static void ensureMdns() {
  if (WiFi.status() != WL_CONNECTED) return;
  static bool mdnsStarted = false;
  if (mdnsStarted) return;

  String host = "esp32-" + apNameFor(deviceId).substring(String("ESP32-").length());
  host.toLowerCase();
  if (MDNS.begin(host.c_str())) {
    mdnsStarted = true;
    Serial.printf("[NORMAL] mDNS started: %s.local\n", host.c_str());
  }
}

static float rand01() {
  return (float)esp_random() / (float)UINT32_MAX;
}

static void initMockGpsIfNeeded() {
  if (!gpsInited) {
    if (mockCfg.useBbox && mockCfg.maxLat > mockCfg.minLat && mockCfg.maxLng > mockCfg.minLng) {
      float latSpan = mockCfg.maxLat - mockCfg.minLat;
      float lngSpan = mockCfg.maxLng - mockCfg.minLng;

      float lat = mockCfg.minLat + rand01() * latSpan;
      float lng = mockCfg.minLng + rand01() * lngSpan;

      if (mockCfg.forceOob) {
        // Push to a deterministic outside strip for demo (north or south based on deviceId hash).
        bool north = ((uint8_t)deviceId[deviceId.length() - 1]) & 1;
        float marginLat = latSpan * 0.20f;
        lat = north ? (mockCfg.maxLat + marginLat) : (mockCfg.minLat - marginLat);
        lng = mockCfg.minLng + rand01() * lngSpan;
      }

      gpsLat = lat;
      gpsLng = lng;
    } else {
      float angle = rand01() * 6.2831853f;
      float dist = sqrtf(rand01()) * mockCfg.radiusM;
      float latRad = mockCfg.centerLat * 0.0174532925f;
      float metersPerDegLat = 111320.0f;
      float metersPerDegLng = 111320.0f * cosf(latRad);
      float dLat = (cosf(angle) * dist) / metersPerDegLat;
      float dLng = (sinf(angle) * dist) / (metersPerDegLng > 1.0f ? metersPerDegLng : 1.0f);
      gpsLat = mockCfg.centerLat + dLat;
      gpsLng = mockCfg.centerLng + dLng;
    }
    gpsInited = true;
  }
}

static void initBatteryIfNeeded() {
  if (!batteryInited) {
    batteryPct = 100;
    batteryInited = true;
  }
}

static void stepMockGps() {
  initMockGpsIfNeeded();

  if (mockCfg.useBbox && mockCfg.maxLat > mockCfg.minLat && mockCfg.maxLng > mockCfg.minLng) {
    float latSpan = mockCfg.maxLat - mockCfg.minLat;
    float lngSpan = mockCfg.maxLng - mockCfg.minLng;

    // Step size as a fraction of the box size (keeps motion visible but bounded).
    float stepLat = (rand01() - 0.5f) * latSpan * 0.06f;
    float stepLng = (rand01() - 0.5f) * lngSpan * 0.06f;
    // Ensure it doesn't get too tiny for small spans.
    if (fabsf(stepLat) < latSpan * 0.004f) stepLat = (rand01() < 0.5f ? -1.0f : 1.0f) * latSpan * 0.004f;
    if (fabsf(stepLng) < lngSpan * 0.004f) stepLng = (rand01() < 0.5f ? -1.0f : 1.0f) * lngSpan * 0.004f;

    gpsLat += stepLat;
    gpsLng += stepLng;

    if (mockCfg.forceOob) {
      // Keep outside the bbox but still moving (stick to outside strip beyond north/south edge).
      bool north = ((uint8_t)deviceId[deviceId.length() - 1]) & 1;
      float marginLat = latSpan * 0.20f;
      float oobMinLat = north ? (mockCfg.maxLat + marginLat * 0.30f) : (mockCfg.minLat - marginLat);
      float oobMaxLat = north ? (mockCfg.maxLat + marginLat) : (mockCfg.minLat - marginLat * 0.30f);

      // Clamp to strip.
      if (gpsLat < oobMinLat) gpsLat = oobMinLat;
      if (gpsLat > oobMaxLat) gpsLat = oobMaxLat;

      // Lng still walks but stays within bbox range.
      if (gpsLng < mockCfg.minLng) gpsLng = mockCfg.minLng + (mockCfg.minLng - gpsLng);
      if (gpsLng > mockCfg.maxLng) gpsLng = mockCfg.maxLng - (gpsLng - mockCfg.maxLng);
      if (gpsLng < mockCfg.minLng) gpsLng = mockCfg.minLng;
      if (gpsLng > mockCfg.maxLng) gpsLng = mockCfg.maxLng;
      return;
    }

    // Normal in-bounds behavior: reflect off edges.
    if (gpsLat < mockCfg.minLat) gpsLat = mockCfg.minLat + (mockCfg.minLat - gpsLat);
    if (gpsLat > mockCfg.maxLat) gpsLat = mockCfg.maxLat - (gpsLat - mockCfg.maxLat);
    if (gpsLng < mockCfg.minLng) gpsLng = mockCfg.minLng + (mockCfg.minLng - gpsLng);
    if (gpsLng > mockCfg.maxLng) gpsLng = mockCfg.maxLng - (gpsLng - mockCfg.maxLng);

    // Final clamp (if reflection overshot).
    if (gpsLat < mockCfg.minLat) gpsLat = mockCfg.minLat;
    if (gpsLat > mockCfg.maxLat) gpsLat = mockCfg.maxLat;
    if (gpsLng < mockCfg.minLng) gpsLng = mockCfg.minLng;
    if (gpsLng > mockCfg.maxLng) gpsLng = mockCfg.maxLng;
    return;
  }

  float stepM = mockCfg.radiusM * 0.08f;
  if (stepM < 2.0f) stepM = 2.0f;
  if (stepM > 12.0f) stepM = 12.0f;

  float angle = rand01() * 6.2831853f;
  float latRad = mockCfg.centerLat * 0.0174532925f;
  float metersPerDegLat = 111320.0f;
  float metersPerDegLng = 111320.0f * cosf(latRad);

  gpsLat += (cosf(angle) * stepM) / metersPerDegLat;
  gpsLng += (sinf(angle) * stepM) / (metersPerDegLng > 1.0f ? metersPerDegLng : 1.0f);

  // Clamp within radius.
  float dLatM = (gpsLat - mockCfg.centerLat) * metersPerDegLat;
  float dLngM = (gpsLng - mockCfg.centerLng) * metersPerDegLng;
  float dist = sqrtf(dLatM * dLatM + dLngM * dLngM);
  if (dist > mockCfg.radiusM && dist > 0.1f) {
    float scale = mockCfg.radiusM / dist;
    dLatM *= scale;
    dLngM *= scale;
    gpsLat = mockCfg.centerLat + (dLatM / metersPerDegLat);
    gpsLng = mockCfg.centerLng + (dLngM / (metersPerDegLng > 1.0f ? metersPerDegLng : 1.0f));
  }
}

static void stepMockBattery() {
  initBatteryIfNeeded();
  batteryPct -= mockCfg.batteryDrain;
  if (batteryPct < 0) {
    batteryPct = mockCfg.batteryLoop ? 100 : 0;
  }
  if (batteryPct > 100) batteryPct = 100;
}

static String a9gSendCommand(const String &cmd, unsigned long timeoutMs) {
  while (A9G.available()) A9G.read();
  A9G.print(cmd);
  A9G.print("\r\n");

  String response = "";
  unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    while (A9G.available()) {
      char c = (char)A9G.read();
      response += c;
    }
    delay(2);
  }
  return response;
}

static bool detectA9GBaud() {
  for (size_t i = 0; i < A9G_BAUD_COUNT; i++) {
    long baud = A9G_BAUD_CANDIDATES[i];
    A9G.end();
    delay(60);
    A9G.begin(baud, SERIAL_8N1, A9G_RX_PIN, A9G_TX_PIN);
    delay(120);

    String resp = a9gSendCommand("AT", 700);
    if (resp.indexOf("OK") >= 0) {
      a9gActiveBaud = baud;
      return true;
    }
  }
  a9gActiveBaud = 0;
  return false;
}

static bool parseA9GLocation(const String &resp, float &lat, float &lng) {
  if (resp.indexOf("GPS NOT FIX NOW") >= 0) return false;
  if (resp.indexOf("+LOCATION:") < 0 && resp.indexOf("LOCATION:") < 0) return false;

  int marker = resp.indexOf("+LOCATION:");
  if (marker < 0) marker = resp.indexOf("LOCATION:");
  if (marker < 0) return false;

  String line = resp.substring(marker);
  int lineEnd = line.indexOf('\n');
  if (lineEnd >= 0) line = line.substring(0, lineEnd);
  line.trim();

  int colon = line.indexOf(':');
  if (colon < 0) return false;
  String values = line.substring(colon + 1);
  values.trim();

  int comma = values.indexOf(',');
  if (comma < 0) return false;

  String latStr = values.substring(0, comma);
  String lngStr = values.substring(comma + 1);
  int comma2 = lngStr.indexOf(',');
  if (comma2 >= 0) lngStr = lngStr.substring(0, comma2);
  latStr.trim();
  lngStr.trim();

  lat = latStr.toFloat();
  lng = lngStr.toFloat();
  return !(isnan(lat) || isnan(lng));
}

static bool fetchA9GGps(float &lat, float &lng) {
  String resp = a9gSendCommand("AT+LOCATION=2", GPS_CMD_TIMEOUT_MS);
  return parseA9GLocation(resp, lat, lng);
}

static bool shouldCheckGpsThisPost() {
  int n = gpsCheckEveryNPosts;
  if (n != 1 && n != 2 && n != 5) n = 1;
  return (postCounter % (unsigned long)n) == 0;
}

static void initGpsSourceOnBoot() {
  useRealGps = false;
  Serial.println("[GPS] Boot check: probing A9G + GPS lock...");

  if (!detectA9GBaud()) {
    Serial.println("[GPS] A9G not detected. Using mock GPS.");
    return;
  }

  Serial.printf("[GPS] A9G detected at baud %ld\n", a9gActiveBaud);
  a9gSendCommand("ATE0", 600);
  a9gSendCommand("AT+GPS=1", 1200);

  unsigned long start = millis();
  while (millis() - start < GPS_BOOT_LOCK_TIMEOUT_MS) {
    float lat = 0.0f, lng = 0.0f;
    if (fetchA9GGps(lat, lng)) {
      useRealGps = true;
      gpsLat = lat;
      gpsLng = lng;
      gpsInited = true;
      Serial.printf("[GPS] GPS lock OK: %.6f, %.6f (using real GPS)\n", gpsLat, gpsLng);
      return;
    }
    delay(GPS_BOOT_POLL_INTERVAL_MS);
  }

  Serial.println("[GPS] No GPS lock at boot timeout. Using mock GPS.");
}

static void applyConfigFromServer(const String &responseBody) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, responseBody);
  if (err) return;

  JsonVariant cfgVar = doc["config"];
  if (!cfgVar.is<JsonObject>()) return;
  JsonObject cfgObj = cfgVar.as<JsonObject>();

  if (cfgObj.containsKey("gps_center") && cfgObj["gps_center"].is<JsonObject>()) {
    JsonObject c = cfgObj["gps_center"].as<JsonObject>();
    if (!c["lat"].isNull()) mockCfg.centerLat = c["lat"].as<float>();
    if (!c["lng"].isNull()) mockCfg.centerLng = c["lng"].as<float>();
  }
  if (cfgObj.containsKey("gps_radius_m")) {
    float r = cfgObj["gps_radius_m"].as<float>();
    if (r >= 1.0f) mockCfg.radiusM = r;
  }
  if (cfgObj.containsKey("gps_bbox") && cfgObj["gps_bbox"].is<JsonObject>()) {
    JsonObject b = cfgObj["gps_bbox"].as<JsonObject>();
    float minLat = b["min_lat"].as<float>();
    float maxLat = b["max_lat"].as<float>();
    float minLng = b["min_lng"].as<float>();
    float maxLng = b["max_lng"].as<float>();
    if (minLat > maxLat) {
      float t = minLat;
      minLat = maxLat;
      maxLat = t;
    }
    if (minLng > maxLng) {
      float t = minLng;
      minLng = maxLng;
      maxLng = t;
    }
    if ((maxLat - minLat) > 0.0f && (maxLng - minLng) > 0.0f) {
      mockCfg.minLat = minLat;
      mockCfg.maxLat = maxLat;
      mockCfg.minLng = minLng;
      mockCfg.maxLng = maxLng;
    }
  }
  if (cfgObj.containsKey("gps_mode")) {
    const char *mode = cfgObj["gps_mode"] | "";
    String m = String(mode);
    m.toLowerCase();
    mockCfg.useBbox = (m == "bbox");
  }
  if (cfgObj.containsKey("force_oob")) {
    mockCfg.forceOob = (bool)cfgObj["force_oob"];
  }
  if (cfgObj.containsKey("battery_drain")) {
    int d = cfgObj["battery_drain"].as<int>();
    if (d >= 0) mockCfg.batteryDrain = d;
  }
  if (cfgObj.containsKey("battery_loop")) {
    mockCfg.batteryLoop = (bool)cfgObj["battery_loop"];
  }
  if (cfgObj.containsKey("post_interval_min")) {
    int minutes = cfgObj["post_interval_min"].as<int>();
    if (minutes == 1 || minutes == 5 || minutes == 15 || minutes == 30) {
      postIntervalMs = (unsigned long)minutes * 60UL * 1000UL;
    }
  }
  if (cfgObj.containsKey("gps_check_every_n_posts")) {
    int n = cfgObj["gps_check_every_n_posts"].as<int>();
    if (n == 1 || n == 2 || n == 5) {
      gpsCheckEveryNPosts = n;
    }
  }
  if (cfgObj.containsKey("force_wait_for_gps_lock")) {
    bool newForceWait = (bool)cfgObj["force_wait_for_gps_lock"];
    if (newForceWait != forceWaitForGpsLock) {
      gpsWaitAttempts = 0;
      lastGpsProbeMs = 0;
      lastGpsStatusPostMs = 0;
      gpsWaitReason = "";
    }
    forceWaitForGpsLock = newForceWait;
  }

  gpsInited = false; // re-seed after any GPS config change
  Serial.printf("[CFG] post_interval_min=%lu gps_check_every_n_posts=%d force_wait_for_gps_lock=%s\n",
                postIntervalMs / 60000UL, gpsCheckEveryNPosts, forceWaitForGpsLock ? "true" : "false");
}

static void postGpsWaitStatus(bool waiting, const String &reason) {
  String url = String("http://") + cfg.serverIp + ":5000/gps_status";
  StaticJsonDocument<320> doc;
  doc["device_id"] = deviceId;
  doc["name"] = cfg.name.length() ? cfg.name : defaultNameFor(deviceId);
  doc["gps_waiting"] = waiting;
  doc["gps_wait_reason"] = reason;
  doc["gps_wait_attempts"] = (unsigned long)gpsWaitAttempts;
  doc["gps_source"] = useRealGps ? "a9g" : "mock";

  String payload;
  serializeJson(doc, payload);

  HTTPClient http;
  http.setTimeout(2500);
  if (!http.begin(url)) {
    Serial.println("[GPS] /gps_status begin failed");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)payload.c_str(), payload.length());
  String respBody = "";
  if (code > 0) respBody = http.getString();
  http.end();
  if (code > 0 && respBody.length()) applyConfigFromServer(respBody);
  Serial.printf("[GPS] POST /gps_status -> %d waiting=%s reason=%s attempts=%lu\n",
                code, waiting ? "true" : "false", reason.c_str(), gpsWaitAttempts);
}

static bool maybeHandleForcedGpsLockWait() {
  if (mode != MODE_NORMAL) return false;
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!forceWaitForGpsLock || useRealGps) return false;

  unsigned long now = millis();
  bool shouldProbe = (lastGpsProbeMs == 0 || (now - lastGpsProbeMs >= GPS_LOCK_RETRY_INTERVAL_MS));
  if (shouldProbe) {
    lastGpsProbeMs = now;
    gpsWaitAttempts++;

    bool a9gReady = (a9gActiveBaud > 0);
    if (!a9gReady) {
      a9gReady = detectA9GBaud();
      if (a9gReady) {
        Serial.printf("[GPS] A9G detected at baud %ld during runtime wait\n", a9gActiveBaud);
        a9gSendCommand("ATE0", 600);
        a9gSendCommand("AT+GPS=1", 1200);
      }
    }

    if (!a9gReady) {
      gpsWaitReason = "a9g_not_detected";
    } else {
      float lat = 0.0f, lng = 0.0f;
      if (fetchA9GGps(lat, lng)) {
        useRealGps = true;
        gpsLat = lat;
        gpsLng = lng;
        gpsInited = true;
        gpsWaitReason = "";
        Serial.printf("[GPS] Lock acquired in forced-wait mode: %.6f, %.6f\n", gpsLat, gpsLng);
        postGpsWaitStatus(false, "locked");
        lastPostMs = 0;
        return false;
      }
      gpsWaitReason = "gps_not_fixed";
    }
    Serial.printf("[GPS] Waiting for lock (%s), attempts=%lu\n", gpsWaitReason.c_str(), gpsWaitAttempts);
  }

  if (lastGpsStatusPostMs == 0 || (now - lastGpsStatusPostMs >= GPS_WAIT_STATUS_INTERVAL_MS)) {
    lastGpsStatusPostMs = now;
    postGpsWaitStatus(true, gpsWaitReason.length() ? gpsWaitReason : "waiting_for_lock");
  }
  return true;
}

static void maybePostData() {
  if (mode != MODE_NORMAL) return;
  if (WiFi.status() != WL_CONNECTED) return;

  unsigned long now = millis();
  if (now - lastPostMs < postIntervalMs) return;
  lastPostMs = now;
  postCounter++;

  bool checkGpsNow = shouldCheckGpsThisPost();

  if (useRealGps) {
    if (checkGpsNow) {
      float lat = 0.0f, lng = 0.0f;
      if (fetchA9GGps(lat, lng)) {
        gpsLat = lat;
        gpsLng = lng;
      } else {
        Serial.println("[GPS] Real GPS read failed. Keeping last known coordinate.");
      }
    }
  } else {
    bool promotedToReal = false;
    if (checkGpsNow) {
      float lat = 0.0f, lng = 0.0f;
      if (fetchA9GGps(lat, lng)) {
        useRealGps = true;
        gpsLat = lat;
        gpsLng = lng;
        gpsInited = true;
        promotedToReal = true;
        Serial.printf("[GPS] Runtime lock acquired: %.6f, %.6f (switching to real GPS)\n", gpsLat, gpsLng);
      }
    }
    if (!promotedToReal) {
      initMockGpsIfNeeded();
      stepMockGps();
    }
  }
  stepMockBattery();

  String url = String("http://") + cfg.serverIp + ":5000/data";
  StaticJsonDocument<384> doc;
  doc["device_id"] = deviceId;
  doc["name"] = cfg.name.length() ? cfg.name : defaultNameFor(deviceId);
  if (cfg.claimToken.length()) doc["claim_token"] = cfg.claimToken;
  doc["status"] = "online";
  JsonObject gps = doc.createNestedObject("gps");
  gps["lat"] = gpsLat;
  gps["lng"] = gpsLng;
  doc["gps_source"] = useRealGps ? "a9g" : "mock";
  doc["battery"] = batteryPct;

  String payload;
  serializeJson(doc, payload);

  HTTPClient http;
  http.setTimeout(2500);
  if (!http.begin(url)) {
    Serial.println("[NORMAL] HTTP begin failed");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)payload.c_str(), payload.length());

  String respBody = "";
  if (code > 0) {
    respBody = http.getString();
  }
  http.end();

  if (code > 0) {
    Serial.printf("[NORMAL] POST /data -> %d (interval=%lums gps_check_every=%d)\n",
                  code, postIntervalMs, gpsCheckEveryNPosts);
    gpsWaitReason = "";
  } else {
    Serial.printf("[NORMAL] POST /data failed: %d\n", code);
  }

  if (respBody.length()) {
    applyConfigFromServer(respBody);
  }

  Serial.printf("[GPS] source=%s lat=%.6f lng=%.6f battery=%d%% post=%lu\n",
                useRealGps ? "a9g" : "mock",
                gpsLat,
                gpsLng,
                batteryPct,
                postCounter);
}

static void maybeClearConfigOnBoot() {
  pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);

  unsigned long started = millis();
  while (millis() - started < 800) {
    if (digitalRead(RESET_BUTTON_PIN) == LOW) {
      // Held during boot window -> clear config.
      Serial.println("[BOOT] Reset button held. Clearing config...");
      clearConfig();
      delay(200);
      ESP.restart();
    }
    delay(10);
  }
}

static void checkResetButtonLongPress() {
  // Using BOOT (GPIO0) as "held-on-boot" is unreliable because holding it during reset
  // enters the ROM serial bootloader ("waiting for download"). Instead, support a
  // runtime long-press that works in normal execution.
  pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);
  bool pressed = (digitalRead(RESET_BUTTON_PIN) == LOW);
  unsigned long now = millis();

  if (pressed) {
    if (resetHoldStartMs == 0) resetHoldStartMs = now;
    if (now - resetHoldStartMs >= RESET_HOLD_MS) {
      Serial.println("[RESET] BOOT long-press detected. Clearing config...");
      clearConfig();
      delay(200);
      ESP.restart();
    }
  } else {
    resetHoldStartMs = 0;
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);

  deviceId = getDeviceId();
  Serial.printf("[BOOT] Device ID: %s\n", deviceId.c_str());

  maybeClearConfigOnBoot();

  if (!loadConfig(cfg)) {
    Serial.println("[BOOT] No config found -> setup mode.");
    setupMode();
    return;
  }

  Serial.printf("[BOOT] Config found. SSID=%s server_ip=%s\n", cfg.ssid.c_str(), cfg.serverIp.c_str());
  initGpsSourceOnBoot();
  normalMode();
}

void loop() {
  checkResetButtonLongPress();

  if (mode == MODE_SETUP) {
    web.handleClient();
    delay(2);
    return;
  }

  ensureWifiConnected();
  ensureMdns();
  if (maybeHandleForcedGpsLockWait()) {
    delay(5);
    return;
  }
  maybePostData();
  delay(5);
}
