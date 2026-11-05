/*
  RKS Pro Micro keyboard and mouse emulator, driven by JSON over USB Serial.

  Board: SparkFun Pro Micro / Leonardo / Micro (ATmega32U4 with native USB).
  Libraries: ArduinoJson 7.x and HID-Project 2.8.4 by NicoHood.
  USB HID: keyboard and mouse events are sent to the target PC.

  Serial interfaces
    USB Serial (Serial): JSON input from the target PC at 115200 baud.
      Send one compact JSON object per line, terminated by '\n'.
      Reply for each JSON line: "OK" or "ERR:<reason>".
      Lines beginning with '{' are parsed as JSON. Other lines are forwarded
      to the controller as EVT messages for compatibility with the old sketch.
      This port is the command protocol; do not use Serial Monitor at the same
      time or its output will interfere with the PC-side protocol.
    Hardware UART (Serial1): Nano_KB commands at 38400 baud.
      Pro Micro TX1 (pin 1) -> Nano_KB D10 (SoftwareSerial RX)
      Pro Micro RX1 (pin 0) <- Nano_KB D11 (SoftwareSerial TX)
      Connect the grounds. Existing RKS text commands and ACKs are retained.

  Example USB line (one line, followed by newline):
  {"actions":[{"type":"combo","keys":["WIN","r"]},{"type":"delay","ms":500},{"type":"text","value":"notepad","interval":30},{"type":"key","key":"ENTER"},{"type":"delay","ms":1000},{"type":"text","value":"Hello from Arduino"},{"type":"key","key":"ENTER","repeat":2},{"type":"combo","keys":["CTRL","a"]}]}

  JSON action types
    text           value (string), interval in ms between characters (default 0)
    key            key name or one character, repeat count (default 1)
    combo          keys array pressed together, then released (maximum 6 keys)
    delay          ms (maximum 300000)
    mouse_move     x and y relative movement in pixels; large moves are split
    mouse_click    button (left | right | middle), count (default 1)
    mouse_press    button to hold
    mouse_release  button to release
    mouse_scroll   amount in wheel steps; positive is up

  Limits and safety
    Maximum JSON line: 767 bytes. Maximum actions per line: 16.
    Key repeat and mouse click count are limited to 100.
    ATmega32U4 has 2.5 KB SRAM; keep JSON lines small, especially when using
    long text or many actions. Text uses the keyboard library's character set.
    Connect KILL_PIN (pin 7) to GND to stop actions and release held inputs.
    Leave pin 7 unconnected for normal operation (INPUT_PULLUP).
    Action lists run synchronously. Serial1 commands wait until the current
    USB JSON action list finishes.
*/

#include <ArduinoJson.h>
#include <Keyboard.h>
#include <Mouse.h>
#undef MOUSE_LEFT
#undef MOUSE_RIGHT
#undef MOUSE_MIDDLE
#include <HID-Settings.h>
#include <MultiReport/AbsoluteMouse.h>

#define NANO_BAUD 38400
#define USB_BAUD 115200
#define USB_LINE_CAPACITY 768
#define NANO_LINE_CAPACITY 128
#define MAX_JSON_ACTIONS 16
#define MAX_KEY_REPEAT 100
#define MAX_MOUSE_DELTA 32767L

const uint8_t KILL_PIN = 7;

struct KeyMap { const char *name; uint8_t code; };

static const KeyMap MOD_MAP[] PROGMEM = {
  {"CTRL", KEY_LEFT_CTRL}, {"SHIFT", KEY_LEFT_SHIFT},
  {"ALT", KEY_LEFT_ALT}, {"WIN", KEY_LEFT_GUI}, {"GUI", KEY_LEFT_GUI},
  {nullptr, 0}
};

static const KeyMap KEY_MAP[] PROGMEM = {
  {"ENTER", KEY_RETURN}, {"ESC", KEY_ESC},
  {"BACKSPACE", KEY_BACKSPACE}, {"TAB", KEY_TAB},
  {"DELETE", KEY_DELETE}, {"DEL", KEY_DELETE}, {"INSERT", KEY_INSERT},
  {"HOME", KEY_HOME}, {"END", KEY_END},
  {"PAGEUP", KEY_PAGE_UP}, {"PAGEDOWN", KEY_PAGE_DOWN},
  {"UP", KEY_UP_ARROW}, {"DOWN", KEY_DOWN_ARROW},
  {"LEFT", KEY_LEFT_ARROW}, {"RIGHT", KEY_RIGHT_ARROW},
  {"CAPSLOCK", KEY_CAPS_LOCK}, {"PRINTSCREEN", KEY_PRINT_SCREEN},
  {"SCROLLLOCK", KEY_SCROLL_LOCK}, {"PAUSE", KEY_PAUSE}, {"SPACE", ' '},
  {"F1", KEY_F1}, {"F2", KEY_F2}, {"F3", KEY_F3}, {"F4", KEY_F4},
  {"F5", KEY_F5}, {"F6", KEY_F6}, {"F7", KEY_F7}, {"F8", KEY_F8},
  {"F9", KEY_F9}, {"F10", KEY_F10}, {"F11", KEY_F11}, {"F12", KEY_F12},
  {nullptr, 0}
};

struct LineBuffer {
  char *data;
  size_t capacity;
  size_t length;
  bool overflow;
};

char usbLineData[USB_LINE_CAPACITY];
char nanoLineData[NANO_LINE_CAPACITY];
LineBuffer usbLine = {usbLineData, sizeof(usbLineData), 0, false};
LineBuffer nanoLine = {nanoLineData, sizeof(nanoLineData), 0, false};

static uint8_t findInMap(const KeyMap *map, const char *name) {
  if (!name) return 0;
  for (uint8_t i = 0; ; i++) {
    const char *entry = (const char*)pgm_read_ptr(&map[i].name);
    if (!entry) break;
    if (!strcasecmp(name, entry)) return pgm_read_byte(&map[i].code);
  }
  return 0;
}

static bool resolveKey(const char *name, uint8_t &code) {
  if (!name || !*name) return false;
  code = findInMap(MOD_MAP, name);
  if (code) return true;
  code = findInMap(KEY_MAP, name);
  if (code) return true;
  if (name[1] == '\0') {
    code = (uint8_t)name[0];
    return true;
  }
  return false;
}

static bool killed() {
  return digitalRead(KILL_PIN) == LOW;
}

static void releaseAll() {
  Keyboard.releaseAll();
  Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
}

static bool waitWhileActive(uint32_t milliseconds) {
  while (milliseconds > 0) {
    if (killed()) return false;
    uint16_t slice = milliseconds > 5 ? 5 : (uint16_t)milliseconds;
    delay(slice);
    milliseconds -= slice;
  }
  return !killed();
}

static uint8_t mouseButton(const char *name) {
  if (!name) return 0;
  if (!strcasecmp(name, "left")) return MOUSE_LEFT;
  if (!strcasecmp(name, "right")) return MOUSE_RIGHT;
  if (!strcasecmp(name, "middle")) return MOUSE_MIDDLE;
  return 0;
}

static int32_t boundedDelta(JsonVariantConst value) {
  int32_t delta = value | 0;
  if (delta > MAX_MOUSE_DELTA) return MAX_MOUSE_DELTA;
  if (delta < -MAX_MOUSE_DELTA) return -MAX_MOUSE_DELTA;
  return delta;
}

static bool moveBy(int32_t x, int32_t y, int32_t wheel) {
  while ((x || y || wheel) && !killed()) {
    int8_t dx = (int8_t)(x > 127 ? 127 : (x < -127 ? -127 : x));
    int8_t dy = (int8_t)(y > 127 ? 127 : (y < -127 ? -127 : y));
    int8_t dw = (int8_t)(wheel > 127 ? 127 : (wheel < -127 ? -127 : wheel));
    Mouse.move(dx, dy, dw);
    x -= dx;
    y -= dy;
    wheel -= dw;
    if (!waitWhileActive(2)) return false;
  }
  return !killed();
}

static const char *runAction(JsonObjectConst action) {
  const char *type = action["type"] | "";
  uint8_t code;

  if (strcmp(type, "text") == 0) {
    const char *value = action["value"] | "";
    int interval = action["interval"] | 0;
    if (interval < 0 || interval > 1000) return "invalid text interval";
    for (const char *cursor = value; *cursor; cursor++) {
      if (killed()) return "killed";
      Keyboard.write((uint8_t)*cursor);
      if (interval > 0 && !waitWhileActive((uint32_t)interval)) return "killed";
    }
  } else if (strcmp(type, "key") == 0) {
    if (!resolveKey(action["key"] | "", code)) return "unknown key";
    int repeat = action["repeat"] | 1;
    if (repeat < 1 || repeat > MAX_KEY_REPEAT) return "invalid key repeat";
    for (int i = 0; i < repeat; i++) {
      if (killed()) return "killed";
      Keyboard.press(code);
      if (!waitWhileActive(20)) return "killed";
      Keyboard.release(code);
      if (!waitWhileActive(20)) return "killed";
    }
  } else if (strcmp(type, "combo") == 0) {
    JsonArrayConst keys = action["keys"].as<JsonArrayConst>();
    if (keys.isNull() || keys.size() == 0 || keys.size() > 6) return "invalid combo keys";
    uint8_t codes[6];
    size_t count = 0;
    for (JsonVariantConst key : keys) {
      const char *name = key.as<const char*>();
      if (!resolveKey(name, codes[count])) return "unknown key in combo";
      count++;
    }
    if (killed()) return "killed";
    for (size_t i = 0; i < count; i++) Keyboard.press(codes[i]);
    if (!waitWhileActive(50)) {
      releaseAll();
      return "killed";
    }
    Keyboard.releaseAll();
  } else if (strcmp(type, "delay") == 0) {
    int32_t milliseconds = action["ms"] | 0;
    if (milliseconds < 0 || milliseconds > 300000) return "invalid delay";
    if (!waitWhileActive((uint32_t)milliseconds)) return "killed";
  } else if (strcmp(type, "mouse_move") == 0) {
    if (!moveBy(boundedDelta(action["x"]), boundedDelta(action["y"]), 0)) return "killed";
  } else if (strcmp(type, "mouse_scroll") == 0) {
    if (!moveBy(0, 0, boundedDelta(action["amount"]))) return "killed";
  } else if (strcmp(type, "mouse_click") == 0 ||
             strcmp(type, "mouse_press") == 0 ||
             strcmp(type, "mouse_release") == 0) {
    uint8_t button = mouseButton(action["button"] | "left");
    if (!button) return "unknown mouse button";
    if (strcmp(type, "mouse_press") == 0) {
      if (killed()) return "killed";
      Mouse.press(button);
    } else if (strcmp(type, "mouse_release") == 0) {
      Mouse.release(button);
    } else {
      int count = action["count"] | 1;
      if (count < 1 || count > MAX_KEY_REPEAT) return "invalid mouse click count";
      for (int i = 0; i < count; i++) {
        if (killed()) return "killed";
        Mouse.press(button);
        if (!waitWhileActive(30)) return "killed";
        Mouse.release(button);
        if (!waitWhileActive(30)) return "killed";
      }
    }
  } else {
    return "unknown action type";
  }
  return nullptr;
}

static void handleJsonLine(char *line) {
  JsonDocument document;
  DeserializationError error = deserializeJson(document, line);
  if (error) {
    Serial.print(F("ERR:json "));
    Serial.println(error.c_str());
    return;
  }

  JsonArrayConst actions = document["actions"].as<JsonArrayConst>();
  if (actions.isNull() || actions.size() == 0) {
    Serial.println(F("ERR:missing actions"));
    return;
  }
  if (actions.size() > MAX_JSON_ACTIONS) {
    Serial.println(F("ERR:too many actions"));
    return;
  }

  for (JsonObjectConst action : actions) {
    if (killed()) {
      releaseAll();
      Serial.println(F("ERR:killed"));
      return;
    }
    const char *errorMessage = runAction(action);
    if (errorMessage) {
      releaseAll();
      Serial.print(F("ERR:"));
      Serial.println(errorMessage);
      return;
    }
  }
  Serial.println(F("OK"));
}

static void forwardEventToController(const char *message) {
  Serial1.print(F("EVT:"));
  Serial1.println(message);
}

static bool processCommand(char *raw) {
  size_t length = strlen(raw);
  while (length > 0 && (raw[length - 1] == '\r' || raw[length - 1] == '\n')) {
    raw[--length] = '\0';
  }
  if (!length) return true;

  if (!strcmp(raw, "PING")) {
    Serial1.println(F("OK PONG"));
    return true;
  }
  if (!strcmp(raw, "CAPS")) {
    Serial1.println(F("CAPS ABS_MOUSE=1 ABS_RANGE=0..32767"));
    return true;
  }
  if (!strcmp(raw, "RELEASEALL")) {
    releaseAll();
    return true;
  }
  if (!strncmp(raw, "TYPE:", 5)) {
    Keyboard.print(raw + 5);
    return true;
  }
  if (!strncmp(raw, "KEY:", 4)) {
    char *key = raw + 4;
    uint8_t code = findInMap(KEY_MAP, key);
    if (code) {
      Keyboard.press(code);
      delay(30);
      Keyboard.release(code);
      return true;
    }
    if (strlen(key) == 1) {
      Keyboard.press(key[0]);
      delay(30);
      Keyboard.release(key[0]);
      return true;
    }
    return false;
  }
  if (!strncmp(raw, "DOWN:", 5)) {
    uint8_t code;
    if (resolveKey(raw + 5, code)) { Keyboard.press(code); return true; }
    return false;
  }
  if (!strncmp(raw, "UP:", 3)) {
    uint8_t code;
    if (resolveKey(raw + 3, code)) { Keyboard.release(code); return true; }
    return false;
  }
  if (!strncmp(raw, "COMBO:", 6)) {
    char buffer[64];
    strncpy(buffer, raw + 6, sizeof(buffer) - 1);
    buffer[sizeof(buffer) - 1] = '\0';
    for (char *cursor = buffer; *cursor; cursor++) *cursor = toupper(*cursor);
    uint8_t count = 0;
    char *token = strtok(buffer, "+");
    while (token && count < 6) {
      uint8_t code = findInMap(MOD_MAP, token);
      if (!code) code = findInMap(KEY_MAP, token);
      if (!code && strlen(token) == 1) {
        char character = token[0];
        if (character >= 'A' && character <= 'Z') character += 'a' - 'A';
        code = (uint8_t)character;
      }
      if (code) { Keyboard.press(code); count++; }
      token = strtok(nullptr, "+");
    }
    if (!count) return false;
    delay(50);
    Keyboard.releaseAll();
    return true;
  }
  if (!strncmp(raw, "MOUSE:ABS ", 10)) {
    const char *cursor = raw + 10;
    while (*cursor == ' ' || *cursor == '\t') cursor++;
    if (*cursor < '0' || *cursor > '9') return false;
    char *end;
    long horizontal = strtol(cursor, &end, 10);
    if (end == cursor || horizontal < 0 || horizontal > 32767) return false;
    cursor = end;
    if (*cursor != ' ' && *cursor != '\t') return false;
    while (*cursor == ' ' || *cursor == '\t') cursor++;
    if (*cursor < '0' || *cursor > '9') return false;
    long vertical = strtol(cursor, &end, 10);
    if (end == cursor || vertical < 0 || vertical > 32767) return false;
    while (*end == ' ' || *end == '\t') end++;
    if (*end) return false;
    int16_t axisX = (int16_t)(horizontal * 2 - 32768L);
    int16_t axisY = (int16_t)(vertical * 2 - 32768L);
    AbsoluteMouse.moveTo(axisX, axisY);
    return true;
  }
  if (!strncmp(raw, "MOUSE:MOVE ", 11)) {
    int dx = 0, dy = 0;
    if (sscanf(raw + 11, "%d %d", &dx, &dy) != 2) return false;
    Mouse.move(constrain(dx, -127, 127), constrain(dy, -127, 127), 0);
    return true;
  }
  if (!strncmp(raw, "MOUSE:CLICK ", 12)) {
    Mouse.click(raw[12] == 'R' || raw[12] == 'r' ? MOUSE_RIGHT :
                raw[12] == 'M' || raw[12] == 'm' ? MOUSE_MIDDLE : MOUSE_LEFT);
    return true;
  }
  if (!strncmp(raw, "MOUSE:DBLCLICK ", 15)) {
    uint8_t button = raw[15] == 'R' || raw[15] == 'r' ? MOUSE_RIGHT :
                     raw[15] == 'M' || raw[15] == 'm' ? MOUSE_MIDDLE : MOUSE_LEFT;
    Mouse.click(button);
    delay(80);
    Mouse.click(button);
    return true;
  }
  if (!strncmp(raw, "MOUSE:DOWN ", 11)) {
    Mouse.press(raw[11] == 'R' || raw[11] == 'r' ? MOUSE_RIGHT :
                raw[11] == 'M' || raw[11] == 'm' ? MOUSE_MIDDLE : MOUSE_LEFT);
    return true;
  }
  if (!strncmp(raw, "MOUSE:UP ", 9)) {
    Mouse.release(raw[9] == 'R' || raw[9] == 'r' ? MOUSE_RIGHT :
                  raw[9] == 'M' || raw[9] == 'm' ? MOUSE_MIDDLE : MOUSE_LEFT);
    return true;
  }
  if (!strncmp(raw, "MOUSE:SCROLL ", 13)) {
    int amount = constrain(atoi(raw + 13), -127, 127);
    Mouse.move(0, 0, amount);
    return true;
  }
  return false;
}

static bool readLine(Stream &port, LineBuffer &line) {
  while (port.available() > 0) {
    int value = port.read();
    if (value < 0) break;
    if (value == '\r') continue;
    if (value == '\n') {
      line.data[line.length] = '\0';
      return true;
    }
    if (!line.overflow) {
      if (line.length + 1 < line.capacity) {
        line.data[line.length++] = (char)value;
      } else {
        line.overflow = true;
      }
    }
  }
  return false;
}

static void resetLine(LineBuffer &line) {
  line.length = 0;
  line.overflow = false;
  line.data[0] = '\0';
}

static void handleUsbLine() {
  if (usbLine.overflow) {
    Serial.println(F("ERR:line too long"));
  } else if (!usbLine.length) {
    resetLine(usbLine);
    return;
  } else if (usbLine.data[0] == '{') {
    handleJsonLine(usbLine.data);
  } else if (!strncmp(usbLine.data, "EVT:", 4)) {
    forwardEventToController(usbLine.data + 4);
  } else {
    forwardEventToController(usbLine.data);
  }
  resetLine(usbLine);
}

static void handleNanoLine() {
  if (nanoLine.overflow) {
    Serial1.println(F("ERR"));
  } else {
    bool ok = processCommand(nanoLine.data);
    Serial1.println(ok ? F("OK") : F("ERR"));
  }
  resetLine(nanoLine);
}

void setup() {
  pinMode(KILL_PIN, INPUT_PULLUP);
  Serial1.begin(NANO_BAUD);
  Serial.begin(USB_BAUD);
  Keyboard.begin();
  Mouse.begin();
  Serial1.println(F("OK READY"));
}

void loop() {
  if (readLine(Serial, usbLine)) handleUsbLine();
  if (readLine(Serial1, nanoLine)) handleNanoLine();
  if (killed()) releaseAll();
}