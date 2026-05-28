// ============================================================
//  RKS — Pro Micro (ATmega32U4)
//  USB HID: Keyboard + Mouse
//
//  Serial1 (TX1/RX1) ← Nano_KB SoftwareSerial（指令來源）
//  Serial  (USB)     → debug（電腦1 Serial Monitor 可看）
//
//  接線：
//    Pro Micro TX1 (Pin 1) → Nano_KB D10 (SoftSerial RX)
//    Pro Micro RX1 (Pin 0) ← Nano_KB D11 (SoftSerial TX)
//    Pro Micro GND         — Nano_KB GND
//    Pro Micro USB         → 電腦1（HID 鍵盤+滑鼠）
//
//  鮑率：38400
// ============================================================

#include <Keyboard.h>
#include <Mouse.h>

#define BAUD 38400

struct KeyMap { const char *name; uint8_t code; };

static const KeyMap MOD_MAP[] PROGMEM = {
  {"CTRL",  KEY_LEFT_CTRL},  {"SHIFT", KEY_LEFT_SHIFT},
  {"ALT",   KEY_LEFT_ALT},   {"GUI",   KEY_LEFT_GUI},
  {nullptr, 0}
};
static const KeyMap KEY_MAP[] PROGMEM = {
  {"ENTER",       KEY_RETURN},    {"ESC",         KEY_ESC},
  {"BACKSPACE",   KEY_BACKSPACE}, {"TAB",         KEY_TAB},
  {"DELETE",      KEY_DELETE},    {"INSERT",      KEY_INSERT},
  {"HOME",        KEY_HOME},      {"END",         KEY_END},
  {"PAGEUP",      KEY_PAGE_UP},   {"PAGEDOWN",    KEY_PAGE_DOWN},
  {"UP",          KEY_UP_ARROW},  {"DOWN",        KEY_DOWN_ARROW},
  {"LEFT",        KEY_LEFT_ARROW},{"RIGHT",       KEY_RIGHT_ARROW},
  {"CAPSLOCK",    KEY_CAPS_LOCK}, {"PRINTSCREEN", KEY_PRINT_SCREEN},
  {"SCROLLLOCK",  KEY_SCROLL_LOCK},{"PAUSE",      KEY_PAUSE},
  {"F1", KEY_F1}, {"F2", KEY_F2}, {"F3", KEY_F3},  {"F4",  KEY_F4},
  {"F5", KEY_F5}, {"F6", KEY_F6}, {"F7", KEY_F7},  {"F8",  KEY_F8},
  {"F9", KEY_F9}, {"F10",KEY_F10},{"F11",KEY_F11}, {"F12", KEY_F12},
  {nullptr, 0}
};

static uint8_t findInMap(const KeyMap *map, const char *name) {
  for (uint8_t i = 0; ; i++) {
    const char *n = (const char*)pgm_read_ptr(&map[i].name);
    if (!n) break;
    if (!strcasecmp(name, n)) return pgm_read_byte(&map[i].code);
  }
  return 0;
}
static uint8_t findMod(const char *s) { return findInMap(MOD_MAP, s); }
static uint8_t findKey(const char *s) { return findInMap(KEY_MAP, s); }
static uint8_t mouseBtn(char c) {
  if (c=='R'||c=='r') return MOUSE_RIGHT;
  if (c=='M'||c=='m') return MOUSE_MIDDLE;
  return MOUSE_LEFT;
}

static bool processCommand(char *raw) {
  int len = strlen(raw);
  while (len > 0 && (raw[len-1]=='\r'||raw[len-1]=='\n')) raw[--len] = 0;
  if (!len) return true;

  if (!strcmp(raw, "PING")) {
    Serial1.println(F("OK PONG")); Serial.println(F("[DBG] PING")); return true;
  }
  if (!strcmp(raw, "RELEASEALL")) {
    Keyboard.releaseAll();
    Mouse.release(MOUSE_LEFT|MOUSE_RIGHT|MOUSE_MIDDLE); return true;
  }
  if (!strncmp(raw, "TYPE:", 5))  { Keyboard.print(raw+5); return true; }
  if (!strncmp(raw, "KEY:", 4))   {
    char *k=raw+4;
    uint8_t code=findKey(k);
    if (code)            { Keyboard.press(code); delay(30); Keyboard.release(code); return true; }
    if (strlen(k)==1)    { Keyboard.press(k[0]); delay(30); Keyboard.release(k[0]); return true; }
    return false;
  }
  if (!strncmp(raw, "DOWN:", 5))  { uint8_t c=findMod(raw+5); if(c){Keyboard.press(c);return true;} return false; }
  if (!strncmp(raw, "UP:", 3))    { uint8_t c=findMod(raw+3); if(c){Keyboard.release(c);return true;} return false; }
  if (!strncmp(raw, "COMBO:", 6)) {
    char buf[64]; strncpy(buf, raw+6, 63); buf[63]=0;
    for (char *p=buf;*p;p++) *p=toupper(*p);
    uint8_t pressed[6]; uint8_t cnt=0;
    char *tok=strtok(buf,"+");
    while (tok&&cnt<6) {
      uint8_t code=findMod(tok); if(!code) code=findKey(tok);
      if(!code&&strlen(tok)==1) {
        char ch = tok[0];
        // 單字母組合鍵預設送小寫，避免隱含 SHIFT（例如 WIN+R 被解讀成 WIN+SHIFT+R）
        if (ch >= 'A' && ch <= 'Z') ch = ch - 'A' + 'a';
        code=(uint8_t)ch;
      }
      if(code){pressed[cnt++]=code;Keyboard.press(code);}
      tok=strtok(nullptr,"+");
    }
    delay(50); Keyboard.releaseAll(); return cnt>0;
  }
  if (!strncmp(raw,"MOUSE:MOVE ",11))    { int dx=0,dy=0; sscanf(raw+11,"%d %d",&dx,&dy); Mouse.move(constrain(dx,-127,127),constrain(dy,-127,127),0); return true; }
  if (!strncmp(raw,"MOUSE:CLICK ",12))   { Mouse.click(mouseBtn(raw[12])); return true; }
  if (!strncmp(raw,"MOUSE:DBLCLICK ",15)){ uint8_t b=mouseBtn(raw[15]); Mouse.click(b); delay(80); Mouse.click(b); return true; }
  if (!strncmp(raw,"MOUSE:DOWN ",11))    { Mouse.press(mouseBtn(raw[11])); return true; }
  if (!strncmp(raw,"MOUSE:UP ",9))       { Mouse.release(mouseBtn(raw[9])); return true; }
  if (!strncmp(raw,"MOUSE:SCROLL ",13))  { Mouse.move(0,0,(int8_t)constrain(atoi(raw+13),-127,127)); return true; }
  return false;
}

char rxBuf[128];

void setup() {
  Serial1.begin(BAUD);
  Serial.begin(BAUD);
  Keyboard.begin();
  Mouse.begin();
  Serial1.println(F("OK READY"));
  Serial.println(F("[ProMicro] Ready. Waiting on Serial1 (TX1/RX1) 38400..."));
}

void loop() {
  if (Serial1.available()) {
    int len = Serial1.readBytesUntil('\n', rxBuf, sizeof(rxBuf)-1);
    rxBuf[len] = 0;
    bool ok = processCommand(rxBuf);
    Serial1.println(ok ? F("OK") : F("ERR"));
    Serial.print(ok ? F("[OK]  ") : F("[ERR] "));
    Serial.println(rxBuf);
  }
}
