// ============================================================
//  RKS — Nano_KB（鍵盤橋接端）
//  USB Serial（電腦2）↔ SoftwareSerial TX/RX（Pro Micro）
//
//  接線：
//    Nano D10 (SoftSerial RX) ← Pro Micro TX1 (Pin 1)
//    Nano D11 (SoftSerial TX) → Pro Micro RX1 (Pin 0)
//    Nano GND                 — Pro Micro GND
//    Nano USB                 → 電腦2
//
//  鮑率：38400（兩端一致）
// ============================================================

#include <SoftwareSerial.h>

SoftwareSerial proMicro(10, 11);  // RX=D10, TX=D11

void setup() {
    Serial.begin(38400);
    proMicro.begin(38400);
    Serial.println("[Nano_KB] Bridge ready. 38400 baud.");
}

void loop() {
    if (Serial.available()) {
        String msg = Serial.readStringUntil('\n');
        msg.trim();
        if (msg.length() > 0) proMicro.println(msg);
    }
    if (proMicro.available()) {
        String ack = proMicro.readStringUntil('\n');
        ack.trim();
        if (ack.length() > 0) Serial.println(ack);
    }
}
