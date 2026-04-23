#include <Arduino.h>

// ESP32 <-> A9G UART test sketch
// Default wiring (change if needed):
// ESP32 GPIO17 (TX2) -> A9G RX
// ESP32 GPIO16 (RX2) -> A9G TX

static const int A9G_RX_PIN = 16;      // ESP32 RX2 pin (reads data from A9G TX)
static const int A9G_TX_PIN = 17;      // ESP32 TX2 pin (sends data to A9G RX)
static const long USB_BAUD = 115200;   // Serial Monitor baud

HardwareSerial A9G(2);

const long BAUD_CANDIDATES[] = {115200, 9600, 57600, 38400, 19200};
const size_t BAUD_COUNT = sizeof(BAUD_CANDIDATES) / sizeof(BAUD_CANDIDATES[0]);

long activeBaud = 0;
unsigned long lastAtPingMs = 0;
unsigned long lastCsqMs = 0;

void sendToA9G(const String &cmd) {
  A9G.print(cmd);
  A9G.print("\r\n");
}

bool waitForOk(unsigned long timeoutMs) {
  unsigned long start = millis();
  String line = "";
  while (millis() - start < timeoutMs) {
    while (A9G.available()) {
      char c = (char)A9G.read();
      if (c == '\r') continue;
      if (c == '\n') {
        if (line.length()) {
          Serial.print("[A9G] ");
          Serial.println(line);
          if (line.indexOf("OK") >= 0) return true;
          if (line.indexOf("ERROR") >= 0) return false;
          line = "";
        }
      } else {
        line += c;
      }
    }
  }
  return false;
}

bool tryBaud(long baud) {
  A9G.end();
  delay(60);
  A9G.begin(baud, SERIAL_8N1, A9G_RX_PIN, A9G_TX_PIN);
  delay(150);

  while (A9G.available()) A9G.read();

  Serial.print("[SCAN] Trying baud ");
  Serial.println(baud);

  for (int i = 0; i < 3; i++) {
    sendToA9G("AT");
    if (waitForOk(700)) {
      activeBaud = baud;
      return true;
    }
    delay(120);
  }

  return false;
}

bool detectA9GBaud() {
  for (size_t i = 0; i < BAUD_COUNT; i++) {
    if (tryBaud(BAUD_CANDIDATES[i])) return true;
  }
  return false;
}

void printHelp() {
  Serial.println();
  Serial.println("=== A9G UART TEST READY ===");
  Serial.print("Detected baud: ");
  Serial.println(activeBaud);
  Serial.println("Type AT commands directly in Serial Monitor.");
  Serial.println("Examples:");
  Serial.println("  AT");
  Serial.println("  ATI");
  Serial.println("  AT+CSQ");
  Serial.println("  AT+CGATT?");
  Serial.println("  AT+CPIN?");
  Serial.println("===========================");
  Serial.println();
}

void setup() {
  Serial.begin(USB_BAUD);
  delay(300);

  Serial.println();
  Serial.println("ESP32 A9G UART connection test");
  Serial.print("Pins -> RX:");
  Serial.print(A9G_RX_PIN);
  Serial.print(" TX:");
  Serial.println(A9G_TX_PIN);

  if (!detectA9GBaud()) {
    Serial.println("[FAIL] Could not detect A9G baud.");
    Serial.println("Check:");
    Serial.println("1) Wiring RX/TX crossed correctly");
    Serial.println("2) Shared GND");
    Serial.println("3) A9G power supply stability");
    Serial.println("4) Try editing BAUD_CANDIDATES[]");
    return;
  }

  sendToA9G("ATE0");
  waitForOk(800);
  printHelp();
}

void loop() {
  if (activeBaud == 0) {
    delay(250);
    return;
  }

  // USB -> A9G passthrough
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.length()) {
      Serial.print("[TX] ");
      Serial.println(cmd);
      sendToA9G(cmd);
    }
  }

  // A9G -> USB passthrough
  while (A9G.available()) {
    char c = (char)A9G.read();
    Serial.write(c);
  }

  // Auto ping so you can see if link drops
  unsigned long now = millis();
  if (now - lastAtPingMs > 5000) {
    sendToA9G("AT");
    lastAtPingMs = now;
  }
  if (now - lastCsqMs > 15000) {
    sendToA9G("AT+CSQ");
    lastCsqMs = now;
  }

  delay(3);
}
