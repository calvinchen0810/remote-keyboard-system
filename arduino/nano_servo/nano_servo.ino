// ============================================================
//  RKS — Nano_SRV（Servo 控制端）
//  移植自 auto-clicker servo_controller.ino
//
//  USB Serial（電腦2）← FastAPI ServoSerial 115200
//  PWM D3/D5/D6/D9/D10/D11 → Servo × 6
//
//  指令協議（\n 結尾）：
//    ATTACH sid pin    → OK ATTACH sid pin
//    DETACH sid        → OK DETACH sid
//    LOOP 0/1
//    BEGIN n           → OK RECEIVING
//    STEP d s a sp h ho → OK STEP n
//    END               → 開始執行 → OK RUNNING n/total ... OK DONE
//    STOP              → OK STOPPED
//    STATUS            → OK IDLE ATTACHED=1,2,...
//    PING              → OK PONG
// ============================================================

#include <Servo.h>

#define MAX_SERVOS  6
#define MAX_STEPS   48
#define BAUD        115200

struct ServoInfo {
  Servo  obj;
  int    pin;
  int    angle;
  bool   attached;
};

struct Step {
  uint16_t delay_ms;
  uint8_t  sid;
  uint8_t  angle;
  uint8_t  speed;      // degrees per delay unit
  uint16_t duration_ms;
  uint8_t  home;
};

ServoInfo servos[MAX_SERVOS + 1];  // index 1..6
Step      script[MAX_STEPS];
uint8_t   scriptLen  = 0;
bool      loopMode   = false;
bool      running    = false;
uint8_t   stepCount  = 0;   // receiving counter

// ── helpers ──────────────────────────────────────────────────
bool validSid(int s) { return s >= 1 && s <= MAX_SERVOS; }
bool validPin(int p) { return p >= 2 && p <= 13; }

void moveServo(ServoInfo &s, int target, int speedDpD) {
  if (!s.attached) return;
  int dir  = (target > s.angle) ? 1 : -1;
  int dpd  = max(1, speedDpD);
  while (s.angle != target) {
    s.angle += dir;
    s.obj.write(s.angle);
    delay(dpd);
  }
}

void execStep(const Step &st) {
  if (st.delay_ms > 0) delay(st.delay_ms);
  if (!validSid(st.sid)) return;
  ServoInfo &s = servos[st.sid];
  if (!s.attached) return;

  int dpd = (st.speed == 0) ? 1 : max(1, 100 / st.speed);
  moveServo(s, st.angle, dpd);

  if (st.duration_ms > 0) delay(st.duration_ms);
  if (st.home) moveServo(s, 0, dpd);
}

// ── command parser ────────────────────────────────────────────
void handleCommand(const char *raw) {
  // PING
  if (!strcmp(raw, "PING"))   { Serial.println(F("OK PONG")); return; }
  // STATUS
  if (!strcmp(raw, "STATUS")) {
    Serial.print(F("OK IDLE ATTACHED="));
    bool first = true;
    for (int i = 1; i <= MAX_SERVOS; i++) {
      if (servos[i].attached) {
        if (!first) Serial.print(',');
        Serial.print(i); first = false;
      }
    }
    Serial.println();
    return;
  }
  // STOP
  if (!strcmp(raw, "STOP")) {
    running = false;
    for (int i = 1; i <= MAX_SERVOS; i++)
      if (servos[i].attached) { servos[i].obj.write(0); servos[i].angle = 0; }
    Serial.println(F("OK STOPPED"));
    return;
  }
  // LOOP 0/1
  if (!strncmp(raw, "LOOP ", 5)) {
    loopMode = (raw[5] == '1');
    return;
  }
  // ATTACH sid pin
  if (!strncmp(raw, "ATTACH ", 7)) {
    int sid, pin;
    if (sscanf(raw + 7, "%d %d", &sid, &pin) == 2 && validSid(sid) && validPin(pin)) {
      if (!servos[sid].attached) {
        servos[sid].obj.attach(pin);
        servos[sid].pin      = pin;
        servos[sid].angle    = 0;
        servos[sid].attached = true;
        servos[sid].obj.write(0);
      }
      Serial.print(F("OK ATTACH ")); Serial.print(sid);
      Serial.print(' ');             Serial.println(pin);
    } else { Serial.println(F("ERR ATTACH")); }
    return;
  }
  // DETACH sid
  if (!strncmp(raw, "DETACH ", 7)) {
    int sid;
    if (sscanf(raw + 7, "%d", &sid) == 1 && validSid(sid)) {
      servos[sid].obj.detach();
      servos[sid].attached = false;
      Serial.print(F("OK DETACH ")); Serial.println(sid);
    } else { Serial.println(F("ERR DETACH")); }
    return;
  }
  // BEGIN n
  if (!strncmp(raw, "BEGIN ", 6)) {
    int n;
    if (sscanf(raw + 6, "%d", &n) == 1) {
      scriptLen = 0; stepCount = 0;
      Serial.println(F("OK RECEIVING"));
    } else { Serial.println(F("ERR BEGIN")); }
    return;
  }
  // STEP d s a sp h ho
  if (!strncmp(raw, "STEP ", 5)) {
    if (scriptLen < MAX_STEPS) {
      Step &st = script[scriptLen];
      int d, s, a, sp, h, ho;
      if (sscanf(raw + 5, "%d %d %d %d %d %d", &d, &s, &a, &sp, &h, &ho) == 6) {
        st.delay_ms   = (uint16_t)d;
        st.sid        = (uint8_t)s;
        st.angle      = (uint8_t)constrain(a, 0, 180);
        st.speed      = (uint8_t)constrain(sp, 1, 100);
        st.duration_ms= (uint16_t)h;
        st.home       = (uint8_t)(ho ? 1 : 0);
        scriptLen++;
        Serial.print(F("OK STEP ")); Serial.println(scriptLen);
      } else { Serial.println(F("ERR STEP")); }
    } else { Serial.println(F("ERR STEP FULL")); }
    return;
  }
  // END → execute
  if (!strcmp(raw, "END")) {
    if (scriptLen == 0) { Serial.println(F("ERR NO STEPS")); return; }
    running = true;
    do {
      for (uint8_t i = 0; i < scriptLen && running; i++) {
        Serial.print(F("OK RUNNING "));
        Serial.print(i + 1); Serial.print('/'); Serial.println(scriptLen);
        execStep(script[i]);
        // drain incoming STOP commands
        while (Serial.available()) {
          String s = Serial.readStringUntil('\n'); s.trim();
          if (s == "STOP") { running = false; break; }
        }
      }
    } while (loopMode && running);
    if (running) {
      Serial.println(F("OK DONE"));
      running = false;
    } else {
      Serial.println(F("OK STOPPED"));
    }
    return;
  }
  Serial.println(F("ERR UNKNOWN"));
}

// ── setup / loop ─────────────────────────────────────────────
char rxBuf[64];

void setup() {
  Serial.begin(BAUD);
  for (int i = 1; i <= MAX_SERVOS; i++) {
    servos[i].attached = false;
    servos[i].angle    = 0;
    servos[i].pin      = 0;
  }
  Serial.println(F("OK READY"));
}

void loop() {
  if (Serial.available()) {
    int len = Serial.readBytesUntil('\n', rxBuf, sizeof(rxBuf) - 1);
    rxBuf[len] = 0;
    // trim CR
    while (len > 0 && (rxBuf[len-1] == '\r' || rxBuf[len-1] == '\n')) rxBuf[--len] = 0;
    if (len > 0) handleCommand(rxBuf);
  }
}
