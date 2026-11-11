// Switch 2 controller bridge for ESP32-S3.
//
//   Native USB (OTG)  -> Nintendo Switch 2. What it enumerates as depends on the mode:
//                          0 pad       HORIPAD-compatible wired gamepad (VID 0F0D / PID 0092)
//                          1 kbm       keyboard (report ID 1) + mouse (report ID 2)
//                          2 keyboard  boot-style keyboard only (no report IDs)
//                          3 mouse     mouse only (no report IDs)
//                        The HORIPAD report has no report ID, so it can't share the single HID
//                        interface with anything else; switching modes restarts the chip.
//   UART0 (CH343 etc) -> host PC, line-based text protocol (921600 baud):
//
//     S <buttons:hex> <hat:0-8> <lx> <ly> <rx> <ry>   pad: set full state (axes 0-255, 128 = center)
//     K <mods:hex> [k1..k6:hex]                     keyboard: set modifiers + up to 6 held usages
//     R <buttons:hex> <dx> <dy> [wheel]             mouse: set buttons, move by dx/dy (-127..127)
//     N                                             neutral (release everything)
//     M <mode:0-3>                                  answer OK, then restart into that mode
//     ?                                             status
//
//   Every command is answered with one line: "OK", "ERR <reason>" or a status line.
//   In pad mode the current state is re-sent to the console every 8 ms while mounted.
//   If the console stops talking to the device (not mounted, or reports stuck) for 5 s while the
//   bus isn't suspended, the bridge detaches and re-attaches USB as if the cable were replugged.

#include <Arduino.h>
#include "USB.h"
#include "USBHID.h"
#include "tusb.h"

static const uint8_t kReportDescriptor[] = {
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x05,        // Usage (Game Pad)
    0xA1, 0x01,        // Collection (Application)
    0x15, 0x00, 0x25, 0x01, 0x35, 0x00, 0x45, 0x01,
    0x75, 0x01, 0x95, 0x10,             // 16 x 1 bit
    0x05, 0x09, 0x19, 0x01, 0x29, 0x10, // Buttons 1..16
    0x81, 0x02,
    0x05, 0x01, 0x25, 0x07, 0x46, 0x3B, 0x01,
    0x75, 0x04, 0x95, 0x01, 0x65, 0x14,
    0x09, 0x39, 0x81, 0x42,             // Hat switch (null state)
    0x65, 0x00, 0x95, 0x01, 0x81, 0x01, // 4 bit padding
    0x26, 0xFF, 0x00, 0x46, 0xFF, 0x00,
    0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35, // X Y Z Rz
    0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
    0x06, 0x00, 0xFF, 0x09, 0x20, 0x95, 0x01, 0x81, 0x02, // vendor byte
    0x0A, 0x21, 0x26, 0x95, 0x08, 0x91, 0x02,             // 8 byte output
    0xC0,
};

// Keyboard / mouse descriptors, with report IDs for the combined mode and without for the others.
enum : uint8_t { kIdKeyboard = 1, kIdMouse = 2 };
static const uint8_t kKeyboardDescId[] = {TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(kIdKeyboard))};
static const uint8_t kMouseDescId[] = {TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(kIdMouse))};
static const uint8_t kKeyboardDesc[] = {TUD_HID_REPORT_DESC_KEYBOARD()};
static const uint8_t kMouseDesc[] = {TUD_HID_REPORT_DESC_MOUSE()};

enum Mode : uint32_t { kModePad = 0, kModeKbm = 1, kModeKeyboard = 2, kModeMouse = 3 };
static const char *const kModeNames[] = {"pad", "kbm", "keyboard", "mouse"};

// Survives ESP.restart() but not a power cycle, so a cold boot always comes up as a gamepad.
static const uint32_t kModeMagic = 0x5332484D;  // "S2HM"
RTC_NOINIT_ATTR static uint32_t g_modeMagic;
RTC_NOINIT_ATTR static uint32_t g_modeNext;
static Mode g_mode = kModePad;

struct __attribute__((packed)) Report {
  uint16_t buttons;
  uint8_t hat;
  uint8_t lx, ly, rx, ry;
  uint8_t vendor;
};

static Report g_report;
static portMUX_TYPE g_lock = portMUX_INITIALIZER_UNLOCKED;

static void setNeutral(Report &r) {
  r.buttons = 0;
  r.hat = 8;
  r.lx = r.ly = r.rx = r.ry = 128;
  r.vendor = 0;
}

class HoriPad : public USBHIDDevice {
 public:
  HoriPad(USBHID &hid) : hid_(hid) {}
  void begin() { hid_.addDevice(this, sizeof(kReportDescriptor)); }
  uint16_t _onGetDescriptor(uint8_t *buffer) override {
    memcpy(buffer, kReportDescriptor, sizeof(kReportDescriptor));
    return sizeof(kReportDescriptor);
  }
  bool send(const Report &r) { return hid_.SendReport(0, &r, sizeof(r), 10); }

 private:
  USBHID &hid_;
};

// A fixed report descriptor, sent with `id` (0 = no report ID).
class RawHidDevice : public USBHIDDevice {
 public:
  RawHidDevice(USBHID &hid) : hid_(hid) {}
  void begin(const uint8_t *desc, uint16_t len, uint8_t id) {
    desc_ = desc;
    len_ = len;
    id_ = id;
    hid_.addDevice(this, len);
  }
  uint16_t _onGetDescriptor(uint8_t *buffer) override {
    memcpy(buffer, desc_, len_);
    return len_;
  }
  bool send(const void *r, size_t n) { return hid_.SendReport(id_, r, n, 20); }

 private:
  USBHID &hid_;
  const uint8_t *desc_ = nullptr;
  uint16_t len_ = 0;
  uint8_t id_ = 0;
};

static USBHID HID;
static HoriPad pad(HID);
static RawHidDevice keyboard(HID);
static RawHidDevice mouse(HID);
static hid_keyboard_report_t g_keys;
static uint8_t g_mouseButtons = 0;

static bool hasKeyboard() { return g_mode == kModeKbm || g_mode == kModeKeyboard; }
static bool hasMouse() { return g_mode == kModeKbm || g_mode == kModeMouse; }

static void releaseKbm() {
  if (hasKeyboard()) {
    memset(&g_keys, 0, sizeof(g_keys));
    keyboard.send(&g_keys, sizeof(g_keys));
  }
  if (hasMouse()) {
    g_mouseButtons = 0;
    hid_mouse_report_t m = {};
    mouse.send(&m, sizeof(m));
  }
}
static bool g_mounted = false;
static uint32_t g_sent = 0;
static uint32_t g_replugs = 0;
static const uint32_t kStuckMs = 5000;

// Detach from the bus (D+ pull-up off) so the console sees an unplug, not a silent restart.
static void detachUsb() {
  tud_disconnect();
  delay(300);
}

static void onUsbEvent(void *, esp_event_base_t base, int32_t id, void *) {
  if (base != ARDUINO_USB_EVENTS) return;
  if (id == ARDUINO_USB_STARTED_EVENT || id == ARDUINO_USB_RESUME_EVENT) g_mounted = true;
  if (id == ARDUINO_USB_STOPPED_EVENT || id == ARDUINO_USB_SUSPEND_EVENT) g_mounted = false;
}

static void handleLine(char *line) {
  char *p = line;
  while (*p == ' ') p++;
  if (*p == 'S') {
    if (g_mode != kModePad) {
      Serial.println("ERR not in pad mode");
      return;
    }
    unsigned btn, hat, lx, ly, rx, ry;
    if (sscanf(p + 1, "%x %u %u %u %u %u", &btn, &hat, &lx, &ly, &rx, &ry) != 6 || hat > 8 ||
        lx > 255 || ly > 255 || rx > 255 || ry > 255) {
      Serial.println("ERR bad S");
      return;
    }
    Report r;
    r.buttons = btn & 0x3FFF;
    r.hat = hat;
    r.lx = lx; r.ly = ly; r.rx = rx; r.ry = ry;
    r.vendor = 0;
    portENTER_CRITICAL(&g_lock);
    g_report = r;
    portEXIT_CRITICAL(&g_lock);
    pad.send(r);  // push immediately; the loop keeps refreshing it
    Serial.println("OK");
  } else if (*p == 'K') {
    if (!hasKeyboard()) {
      Serial.println("ERR not in keyboard mode");
      return;
    }
    unsigned v[7] = {0};
    int n = sscanf(p + 1, "%x %x %x %x %x %x %x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6]);
    if (n < 1) {
      Serial.println("ERR bad K");
      return;
    }
    g_keys.modifier = v[0];
    g_keys.reserved = 0;
    for (int i = 0; i < 6; i++) g_keys.keycode[i] = v[i + 1];
    Serial.println(keyboard.send(&g_keys, sizeof(g_keys)) ? "OK" : "ERR send failed");
  } else if (*p == 'R') {
    if (!hasMouse()) {
      Serial.println("ERR not in mouse mode");
      return;
    }
    unsigned btn;
    int dx, dy, wheel = 0;
    if (sscanf(p + 1, "%x %d %d %d", &btn, &dx, &dy, &wheel) < 3 || dx < -127 || dx > 127 ||
        dy < -127 || dy > 127 || wheel < -127 || wheel > 127) {
      Serial.println("ERR bad R");
      return;
    }
    g_mouseButtons = btn & 0x1F;
    hid_mouse_report_t m = {g_mouseButtons, (int8_t)dx, (int8_t)dy, (int8_t)wheel, 0};
    Serial.println(mouse.send(&m, sizeof(m)) ? "OK" : "ERR send failed");
  } else if (*p == 'N') {
    if (g_mode == kModePad) {
      Report r;
      setNeutral(r);
      portENTER_CRITICAL(&g_lock);
      g_report = r;
      portEXIT_CRITICAL(&g_lock);
      pad.send(r);
    } else {
      releaseKbm();
    }
    Serial.println("OK");
  } else if (*p == 'M') {
    unsigned m;
    if (sscanf(p + 1, "%u", &m) != 1 || m > kModeMouse) {
      Serial.println("ERR bad M");
      return;
    }
    if (m == g_mode) {
      Serial.println("OK");
      return;
    }
    g_modeMagic = kModeMagic;
    g_modeNext = m;
    Serial.println("OK");
    Serial.flush();
    detachUsb();
    ESP.restart();
  } else if (*p == '?') {
    Serial.printf("STATUS mounted=%d ready=%d sent=%lu uptime=%lu mode=%lu replugs=%lu\n", g_mounted,
                  HID.ready(), (unsigned long)g_sent, (unsigned long)millis(), (unsigned long)g_mode,
                  (unsigned long)g_replugs);
  } else if (*p == '\0') {
    // ignore blank lines
  } else {
    Serial.println("ERR unknown");
  }
}

void setup() {
  setNeutral(g_report);

  Serial.setRxBufferSize(1024);
  Serial.begin(921600);

  if (g_modeMagic == kModeMagic && g_modeNext <= kModeMouse) g_mode = (Mode)g_modeNext;
  g_modeMagic = 0;  // one-shot: a later crash/reset falls back to pad mode

  USB.onEvent(onUsbEvent);
  USB.usbClass(0);
  USB.usbSubClass(0);
  USB.usbProtocol(0);
  USB.firmwareVersion(0x0100);
  USB.usbPower(500);
  if (g_mode == kModePad) {
    USB.VID(0x0F0D);
    USB.PID(0x0092);
    USB.manufacturerName("HORI CO.,LTD.");
    USB.productName("POKKEN CONTROLLER");
    pad.begin();
  } else {
    USB.VID(0x303A);  // Espressif
    USB.PID(0x8100 + g_mode);
    USB.manufacturerName("switch2-harness");
    USB.productName(g_mode == kModeKbm ? "S2H Keyboard Mouse"
                    : g_mode == kModeKeyboard ? "S2H Keyboard" : "S2H Mouse");
    if (g_mode == kModeKbm) {
      keyboard.begin(kKeyboardDescId, sizeof(kKeyboardDescId), kIdKeyboard);
      mouse.begin(kMouseDescId, sizeof(kMouseDescId), kIdMouse);
    } else if (g_mode == kModeKeyboard) {
      keyboard.begin(kKeyboardDesc, sizeof(kKeyboardDesc), 0);
    } else {
      mouse.begin(kMouseDesc, sizeof(kMouseDesc), 0);
    }
  }
  HID.begin();
  USB.begin();

  Serial.printf("READY switch2-bridge mode=%s\n", kModeNames[g_mode]);
}

void loop() {
  static char buf[96];
  static size_t len = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      buf[len] = '\0';
      if (len) handleLine(buf);
      len = 0;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }

  static uint32_t last = 0;
  uint32_t now = millis();
  if (now - last >= 8) {
    last = now;
    if (g_mode == kModePad && HID.ready()) {
      Report r;
      portENTER_CRITICAL(&g_lock);
      r = g_report;
      portEXIT_CRITICAL(&g_lock);
      if (pad.send(r)) g_sent++;
    }
  }

  // Re-plug watchdog. A suspended bus (console asleep) is left alone.
  static uint32_t okSince = 0;
  if (tud_suspended() || (tud_mounted() && HID.ready())) {
    okSince = now;
  } else if (now - okSince >= kStuckMs) {
    detachUsb();
    tud_connect();
    g_replugs++;
    okSince = millis();
  }
}
