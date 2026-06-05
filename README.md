# RKS - Remote Keyboard System

RKS is a FastAPI based remote input and visual-trigger automation system.
It controls keyboard/mouse (KBM) and servo actions from a web UI or from curl script JSON.

- KBM path: Computer 2 -> Nano_KB -> Pro Micro -> USB HID to Computer 1
- SRV path: Computer 2 -> Nano_SRV -> up to 6 servos
- Vision path: Webcam on Computer 2 -> snapshot/condition matching -> IMG.MATCH events

---

## Architecture

```text
Computer 2 (Controller)
  FastAPI :8000
    |- KeyboardSerial 38400 -> Nano_KB -> SoftSerial -> Pro Micro Serial1 38400
    |- ServoSerial 115200   -> Nano_SRV -> PWM -> Servo x6
    |- Webcam -> frame buffer -> visual monitor

Computer 1 (Target)
  Pro Micro USB HID keyboard + mouse receiver
```

---

## Hardware

- Arduino Pro Micro (ATmega32U4) x1
- Arduino Nano (Nano_KB) x1
- Arduino Nano (Nano_SRV) x1
- SG90 servo x1~6
- Webcam x1
- USB cables x3
- Dupont wires for Nano_KB <-> Pro Micro (TX/RX/GND)

---

## Wiring

```text
Nano_KB D11 (TX) -> Pro Micro RX1 (Pin 0)
Nano_KB D10 (RX) <- Pro Micro TX1 (Pin 1)
Nano_KB GND      -- Pro Micro GND
Nano_KB USB      -> Computer 2

Pro Micro USB    -> Computer 1 (HID)

Nano_SRV D9  -> S1
Nano_SRV D10 -> S2
Nano_SRV D11 -> S3
Nano_SRV D6  -> S4
Nano_SRV D5  -> S5
Nano_SRV D3  -> S6
Nano_SRV USB -> Computer 2
```

---

## Baud Rates

- PC2 <-> Nano_KB USB: 38400
- Nano_KB SoftSerial <-> Pro Micro Serial1: 38400
- Pro Micro USB debug serial: 38400
- PC2 <-> Nano_SRV USB: 115200

---

## Project Structure

```text
remote-keyboard-system/
├─ README.md
├─ arduino/
│  ├─ nano_keyboard/nano_keyboard.ino
│  ├─ pro_micro/pro_micro.ino
│  └─ nano_servo/nano_servo.ino
├─ server/
│  ├─ main.py
│  ├─ keyboard_serial.py
│  ├─ servo_serial.py
│  ├─ servo_router.py
│  ├─ requirements.txt
│  ├─ build-exe.bat
│  ├─ rks-server.spec
│  └─ static/
│     ├─ index.html
│     ├─ arch.html
│     └─ sw.js
└─ tools/
   ├─ test_promicro.py
   ├─ test_nano_servo.py
   ├─ test_evt_flow.py
   ├─ send_event.ps1
   └─ SerialMonitor.ps1
```

---

## Run

```bash
cd server
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open:

- UI: http://127.0.0.1:8000
- Architecture page: http://127.0.0.1:8000/arch

### Optional Environment Variables

- `HOST` (default `0.0.0.0`)
- `PORT` (default `8000`)
- `AUTO_CONNECT_SERIAL` (default `1`)
- `CAMERA_INDEX` (default `0`)
- `CAMERA_WIDTH` (default `1280`)
- `CAMERA_HEIGHT` (default `720`)
- `JPEG_QUALITY` (default `75`)
- `DEFAULT_EVT_TIMEOUT_MS` (default `30000`)

---

## Startup Flow (Current)

1. FastAPI starts
2. Camera auto open by `CAMERA_INDEX`
3. Background serial auto-probe for SRV and KBM
4. Visual monitor task starts
5. When camera + SRV + KBM are all ready, console logs:

```text
service ready for script input
```

### Typical Logs

```text
[INFO] main: Camera auto-started: index=0, Camera ready
[INFO] servo_serial: SRV connected: COM18 @ 115200
[INFO] keyboard_serial: KBM connected: COM10 @ 38400
[INFO] main: service ready for script input
```

---

## Auto Detect Details

- SRV probing: 115200 `PING` expecting `OK PONG` / `OK READY` style responses
- KBM probing: 38400, prefer Nano_KB startup banner detection (`[Nano_KB] Bridge ready...`), then fallback to `PING` roundtrip
- If auto connect fails, UI manual connect is still available

---

## Core Runtime Concepts

### 1) Mixed Script Runner

`/script/run` accepts mixed steps:

- `srv`: servo action
- `kbd`: keyboard action
- `mse`: mouse action
- `evt`: wait for event gate

### 2) Event Bus + Pending Pool

- If script starts with an event gate that is not available yet, script is held in pending pool bucket by event name
- When event is emitted (from KBM `EVT:<name>` or visual trigger), pending scripts are released to queue

### 3) Visual Monitor

- Condition evaluates at about 3 fps
- On match edge, emits `IMG.MATCH.<condition_id>`
- Broadcasts visual events by websocket

### 4) Auto Attach Servo in Script Mode

- Before queueing/running scripts, required servo IDs are auto-attached if pin mapping exists
- This also applies to scripts released later from pending pool

---

## Script JSON Formats

### A. Basic Mixed Script

```json
{
  "loop": false,
  "servos": { "1": 9, "2": 10 },
  "attach_cmds": ["ATTACH 1 9", "ATTACH 2 10"],
  "steps": [
    {
      "type": "srv",
      "delay_ms": 500,
      "servo_id": 1,
      "angle": 90,
      "speed": 60,
      "duration_ms": 300,
      "home": 1
    },
    {
      "type": "kbd",
      "delay_ms": 100,
      "cmd_type": "KEY",
      "key": "F12"
    },
    {
      "type": "evt",
      "delay_ms": 0,
      "evt": "HELLO"
    },
    {
      "type": "mse",
      "delay_ms": 50,
      "action": "MOVE",
      "x": 10,
      "y": 0
    }
  ]
}
```

### B. Script Bundle (IMG Embedded)

`/script/run` and `/script/import_bundle` both support this bundle format.

```json
{
  "schema_version": 2,
  "loop": false,
  "servos": { "1": 9 },
  "attach_cmds": ["ATTACH 1 9"],
  "steps": [
    { "type": "evt", "delay_ms": 0, "evt": "IMG.MATCH.old-cond-id" },
    { "type": "kbd", "delay_ms": 100, "cmd_type": "TYPE", "text": "done" }
  ],
  "img_bundle": {
    "snapshots": [
      {
        "snapshot_id": "snap-old-1",
        "jpeg_b64": "...base64 jpeg..."
      }
    ],
    "conditions": [
      {
        "condition_id": "old-cond-id",
        "name": "dialog-ready",
        "snapshot_id": "snap-old-1",
        "roi": [0.2, 0.3, 0.4, 0.2],
        "threshold": 0.92,
        "min_hits": 3,
        "cooldown_ms": 3000
      }
    ]
  }
}
```

Bundle import behavior:

- snapshot base64 is stored into snapshot pool
- conditions are created and auto-armed
- `IMG.MATCH.<old_id>` in steps is remapped to new imported IDs
- websocket emits visual sync event
- if camera is not running, server auto starts camera for IMG bundle import

---

## Curl Examples

### Run script (basic or bundle)

```bash
curl.exe -X POST http://127.0.0.1:8000/script/run ^
  -H "Content-Type: application/json" ^
  --data-binary "@your_script.json"
```

### Stop script

```bash
curl.exe -X POST http://127.0.0.1:8000/script/stop
```

### Check script status

```bash
curl.exe http://127.0.0.1:8000/script/status
```

---

## API Summary

### General

- `GET /`
- `GET /arch`
- `GET /health`
- `GET /stream`

### Camera

- `GET /camera/devices`
- `POST /camera/start`
- `POST /camera/stop`
- `GET /camera/status`

### KBM

- `GET /kbm/api/ports`
- `GET /kbm/api/status`
- `POST /kbm/api/connect`
- `POST /kbm/api/disconnect`
- `POST /kbm/api/send`
- `WS /ws/kbm`

### SRV

- `GET /srv/api/ports`
- `GET /srv/api/status`
- `POST /srv/api/connect`
- `POST /srv/api/disconnect`
- `POST /srv/api/attach`
- `POST /srv/api/detach`
- `POST /srv/api/attach_all`
- `POST /srv/api/detach_all`
- `POST /srv/api/run`
- `POST /srv/api/stop`
- `POST /srv/api/command`
- `POST /srv/api/send`
- `WS /ws/srv`

### Mixed Script + Pool

- `POST /script/run`
- `POST /script/stop`
- `POST /script/import_bundle`
- `GET /script/status`
- `GET /script/pool/{evt}`
- `DELETE /script/pool/{evt}`
- `POST /script/pool/{evt}/queue_now`

### Visual

- `POST /visual/snapshot`
- `GET /visual/snapshots`
- `DELETE /visual/snapshots/{snap_id}`
- `POST /visual/conditions`
- `GET /visual/conditions`
- `PUT /visual/conditions/{cid}`
- `DELETE /visual/conditions/{cid}`
- `POST /visual/conditions/{cid}/arm`
- `POST /visual/conditions/{cid}/disarm`
- `POST /visual/conditions/{cid}/test`

---

## KBM Protocol (38400)

- `TYPE:<text>`
- `KEY:<name>` (ENTER ESC BACKSPACE TAB DELETE F1~F12 ...)
- `DOWN:<mod>` / `UP:<mod>` where mod in `CTRL SHIFT ALT GUI`
- `COMBO:<m>+<k>`
- `RELEASEALL`
- `MOUSE:MOVE dx dy`
- `MOUSE:CLICK L|R|M`
- `MOUSE:DBLCLICK L|R|M`
- `MOUSE:DOWN L|R|M`
- `MOUSE:UP L|R|M`
- `MOUSE:SCROLL n`
- `PING`
- `EVT:<name>`

---

## Troubleshooting

### Camera not available

- Close Teams/Zoom/Camera app and retry
- Check Windows camera privacy permissions
- Try different `CAMERA_INDEX`
- Use `GET /camera/devices` to inspect available indices

### SRV/KBM no matched port

- Ensure no other app is holding COM ports (Arduino Serial Monitor, etc.)
- Replug device and confirm COM number in Device Manager
- Use manual connect from UI if auto detect fails

### Script stuck in pending

- Check first `evt` step value
- Emit corresponding `EVT:<name>` from target side
- Inspect `GET /script/status` and `GET /script/pool/{evt}`

---

## Validation Tools

- `python tools/test_promicro.py`
- `python tools/test_nano_servo.py`
- `python tools/test_evt_flow.py`
- `powershell -ExecutionPolicy Bypass -File .\tools\send_event.ps1`
