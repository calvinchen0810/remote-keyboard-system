"""
tools/test_nano_servo.py
MVP Step 2 — Nano_SRV Servo 功能驗證

執行前：
  1. 燒錄 arduino/nano_servo/nano_servo.ino 到 Nano_SRV，插電腦2
  2. SG90 接在 D9
  3. 修改 PORT 為 Nano_SRV 的 COM port
"""

import serial, time, sys

PORT     = "COM4"    # ← 修改為 Nano_SRV 的 COM port
BAUDRATE = 115200
TIMEOUT  = 3.0

def send(ser, cmd, wait_prefix=None, timeout=3.0):
    ser.write(f"{cmd}\n".encode())
    print(f"  >> {cmd}")
    if not wait_prefix:
        time.sleep(0.1); return True, ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()
            if raw:
                ok = raw.startswith(wait_prefix)
                print(f"  {'✓' if ok else '?'} << {raw}")
                return ok, raw
    print(f"  ✗ TIMEOUT waiting for: {wait_prefix}")
    return False, "TIMEOUT"

def main():
    print(f"\n{'='*52}")
    print(f"  RKS MVP Step 2 — Nano_SRV Servo Test")
    print(f"  Port: {PORT}  Baud: {BAUDRATE}")
    print(f"{'='*52}\n")

    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=TIMEOUT)
        time.sleep(2); ser.reset_input_buffer()
        deadline = time.time() + 3
        while time.time() < deadline:
            if ser.in_waiting:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()
                if raw: print(f"  << {raw}"); break
        print(f"  [OK] 連線成功: {PORT}\n")
    except serial.SerialException as e:
        print(f"\n  [ERR] 無法連線: {e}"); sys.exit(1)

    results = []

    print("── PING ────────────────────────────────────")
    ok, _ = send(ser, "PING", "OK PONG")
    results.append(("PING", ok))

    print("\n── ATTACH S1 D9 ────────────────────────────")
    ok, _ = send(ser, "ATTACH 1 9", "OK ATTACH", 4.0)
    results.append(("ATTACH 1 9", ok))
    time.sleep(0.5)

    print("\n── STATUS ──────────────────────────────────")
    ok, _ = send(ser, "STATUS", "OK IDLE")
    results.append(("STATUS", ok))

    print("\n── Single Step 90° ─────────────────────────")
    send(ser, "BEGIN 1")
    time.sleep(0.1)
    send(ser, "STEP 0 1 90 60 300 1")
    time.sleep(0.1)
    ser.write(b"END\n")
    print("  Waiting for OK DONE...")
    deadline = time.time() + 10; done = False
    while time.time() < deadline:
        if ser.in_waiting:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()
            if raw:
                sym = '✓' if raw.startswith(("OK","OK DONE")) else '?'
                print(f"  {sym} << {raw}")
                if raw == "OK DONE": done = True; break
    results.append(("STEP 90° → DONE", done))

    print("\n── DETACH ──────────────────────────────────")
    ok, _ = send(ser, "DETACH 1", "OK DETACH")
    results.append(("DETACH 1", ok))
    ser.close()

    passed = sum(1 for _,ok in results if ok)
    total  = len(results)
    print(f"\n{'='*52}")
    print(f"  結果：{passed}/{total} 通過")
    print(f"  {'✓ MVP Step 2 PASSED' if passed==total else '✗ MVP Step 2 FAILED'}")
    if passed < total:
        for name, ok in results:
            if not ok: print(f"    ✗ {name}")
    print(f"{'='*52}\n")

if __name__ == "__main__":
    main()
