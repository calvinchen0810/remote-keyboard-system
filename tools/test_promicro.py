"""
tools/test_promicro.py
MVP Step 1 — Pro Micro HID 功能驗證（透過 Nano_KB 橋接）

執行前：
  1. 燒錄 arduino/pro_micro/pro_micro.ino 到 Pro Micro，插電腦1
  2. 燒錄 arduino/nano_keyboard/nano_keyboard.ino 到 Nano_KB，插電腦2
  3. 接好 Nano_KB D10/D11 ↔ Pro Micro TX1/RX1，GND 共地
  4. 在電腦1開啟記事本並給焦點
  5. 修改 PORT 為 Nano_KB 在電腦2的 COM port
"""

import serial, time, sys

PORT     = "COM3"    # ← 修改為 Nano_KB 的 COM port（電腦2上）
BAUDRATE = 38400
TIMEOUT  = 1.0

def send(ser, cmd, label=""):
    ser.reset_input_buffer()
    ser.write(f"{cmd}\n".encode())
    time.sleep(0.15)
    ack = ser.readline().decode("utf-8", errors="ignore").strip()
    ok  = ack in ("OK", "OK PONG") or ack.startswith("OK")
    print(f"  {'✓' if ok else '✗'} [{ack:>10}]  {label or cmd}")
    return ok

def main():
    print(f"\n{'='*52}")
    print(f"  RKS MVP Step 1 — Pro Micro HID (via Nano_KB)")
    print(f"  Port: {PORT}  Baud: {BAUDRATE}")
    print(f"{'='*52}\n")
    print("  ⚠  確認電腦1 記事本已開啟並有焦點")
    input("  按 Enter 開始測試...")

    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=TIMEOUT)
        time.sleep(2); ser.reset_input_buffer()
        print(f"\n  [OK] 連線成功: {PORT}\n")
    except serial.SerialException as e:
        print(f"\n  [ERR] 無法連線: {e}"); sys.exit(1)

    results = []
    print("── Ping ────────────────────────────────────")
    results.append(("PING",         send(ser, "PING",                    "連線測試")))
    print("\n── Keyboard ────────────────────────────────")
    results.append(("TYPE",         send(ser, "TYPE:Hello RKS",          "輸入文字")))
    time.sleep(0.3)
    results.append(("KEY:ENTER",    send(ser, "KEY:ENTER",               "按 Enter")))
    time.sleep(0.3)
    results.append(("COMBO:CTRL+A", send(ser, "COMBO:CTRL+A",            "全選")))
    time.sleep(0.2)
    results.append(("KEY:DELETE",   send(ser, "KEY:DELETE",              "刪除")))
    time.sleep(0.2)
    results.append(("KEY:F5",       send(ser, "KEY:F5",                  "F5")))
    time.sleep(0.2)
    results.append(("RELEASEALL",   send(ser, "RELEASEALL",              "釋放所有鍵")))
    print("\n── Mouse ───────────────────────────────────")
    results.append(("MOVE +50",     send(ser, "MOUSE:MOVE 50 50",        "滑鼠 +50,+50")))
    time.sleep(0.2)
    results.append(("MOVE -50",     send(ser, "MOUSE:MOVE -50 -50",      "滑鼠 -50,-50")))
    time.sleep(0.2)
    results.append(("CLICK L",      send(ser, "MOUSE:CLICK L",           "左鍵單擊")))
    time.sleep(0.2)
    results.append(("DBLCLICK L",   send(ser, "MOUSE:DBLCLICK L",        "左鍵雙擊")))
    time.sleep(0.3)
    results.append(("SCROLL -3",    send(ser, "MOUSE:SCROLL -3",         "向下滾動")))
    ser.close()

    passed = sum(1 for _,ok in results if ok)
    total  = len(results)
    print(f"\n{'='*52}")
    print(f"  結果：{passed}/{total} 通過")
    print(f"  {'✓ MVP Step 1 PASSED' if passed==total else '✗ MVP Step 1 FAILED'}")
    if passed < total:
        for name, ok in results:
            if not ok: print(f"    ✗ {name}")
    print(f"{'='*52}\n")

if __name__ == "__main__":
    main()
