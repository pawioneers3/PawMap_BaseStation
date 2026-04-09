#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#include <ArduinoJson.h>

// ----- User-adjustable (optional) -----
static const int RESET_BUTTON_PIN = 0; // BOOT button on many ESP32 DevKit V1 boards
static const unsigned long POST_INTERVAL_MS = 5000;
static const unsigned long WIFI_RETRY_INTERVAL_MS = 5000;
static const unsigned long WIFI_GIVEUP_MS = 30000;

// ----- NVS keys -----
static const char *PREF_NS = "cfg";
static const char *K_SSID = "ssid";
static const char *K_PASS = "pass";
static const char *K_SERVER = "server";
static const char *K_NAME = "name";

struct Config {
  String ssid;
  String pass;
  String serverIp;
  String name;
};

Preferences prefs;
WebServer web(80);

enum Mode { MODE_SETUP, MODE_NORMAL };
Mode mode = MODE_SETUP;

Config cfg;
String deviceId;

unsigned long lastPostMs = 0;
unsigned long lastWifiAttemptMs = 0;
unsigned long wifiStartedMs = 0;

struct MockConfig {
  float centerLat = 14.5995f;
  float centerLng = 120.9842f;
  float radiusM = 150.0f;
  int batteryDrain = 1;
  bool batteryLoop = true;
};

MockConfig mockCfg;
float gpsLat = 0.0f;
float gpsLng = 0.0f;
bool gpsInited = false;
int batteryPct = 100;
bool batteryInited = false;

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
  prefs.end();
  return out.ssid.length() > 0 && out.serverIp.length() > 0;
}

static void saveConfig(const Config &in) {
  prefs.begin(PREF_NS, false);
  prefs.putString(K_SSID, in.ssid);
  prefs.putString(K_PASS, in.pass);
  prefs.putString(K_SERVER, in.serverIp);
  prefs.putString(K_NAME, in.name);
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
    } else {
      Serial.printf("[SETUP] JSON parse error: %s\n", err.c_str());
    }
  } else {
    ssid = web.arg("ssid");
    pass = web.arg("password");
    name = web.arg("name");
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

static void initMockStateIfNeeded() {
  if (!gpsInited) {
    float angle = rand01() * 6.2831853f;
    float dist = sqrtf(rand01()) * mockCfg.radiusM;
    float latRad = mockCfg.centerLat * 0.0174532925f;
    float metersPerDegLat = 111320.0f;
    float metersPerDegLng = 111320.0f * cosf(latRad);
    float dLat = (cosf(angle) * dist) / metersPerDegLat;
    float dLng = (sinf(angle) * dist) / (metersPerDegLng > 1.0f ? metersPerDegLng : 1.0f);
    gpsLat = mockCfg.centerLat + dLat;
    gpsLng = mockCfg.centerLng + dLng;
    gpsInited = true;
  }

  if (!batteryInited) {
    batteryPct = 100;
    batteryInited = true;
  }
}

static void stepMockGps() {
  initMockStateIfNeeded();

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
  initMockStateIfNeeded();
  batteryPct -= mockCfg.batteryDrain;
  if (batteryPct < 0) {
    batteryPct = mockCfg.batteryLoop ? 100 : 0;
  }
  if (batteryPct > 100) batteryPct = 100;
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
    gpsInited = false; // re-seed around new center
  }
  if (cfgObj.containsKey("gps_radius_m")) {
    float r = cfgObj["gps_radius_m"].as<float>();
    if (r >= 1.0f) mockCfg.radiusM = r;
  }
  if (cfgObj.containsKey("battery_drain")) {
    int d = cfgObj["battery_drain"].as<int>();
    if (d >= 0) mockCfg.batteryDrain = d;
  }
  if (cfgObj.containsKey("battery_loop")) {
    mockCfg.batteryLoop = (bool)cfgObj["battery_loop"];
  }
}

static void maybePostData() {
  if (mode != MODE_NORMAL) return;
  if (WiFi.status() != WL_CONNECTED) return;

  unsigned long now = millis();
  if (now - lastPostMs < POST_INTERVAL_MS) return;
  lastPostMs = now;

  initMockStateIfNeeded();
  stepMockGps();
  stepMockBattery();

  String url = String("http://") + cfg.serverIp + ":5000/data";
  StaticJsonDocument<256> doc;
  doc["device_id"] = deviceId;
  doc["name"] = cfg.name.length() ? cfg.name : defaultNameFor(deviceId);
  doc["status"] = "online";
  JsonObject gps = doc.createNestedObject("gps");
  gps["lat"] = gpsLat;
  gps["lng"] = gpsLng;
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
    Serial.printf("[NORMAL] POST /data -> %d\n", code);
  } else {
    Serial.printf("[NORMAL] POST /data failed: %d\n", code);
  }

  if (respBody.length()) {
    applyConfigFromServer(respBody);
  }
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
  normalMode();
}

void loop() {
  if (mode == MODE_SETUP) {
    web.handleClient();
    delay(2);
    return;
  }

  ensureWifiConnected();
  ensureMdns();
  maybePostData();
  delay(5);
}
