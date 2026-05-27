# RKS — Remote Keyboard System

透過網頁虛擬鍵盤遠端操控另一台電腦的鍵盤與滑鼠，並透過 Webcam 即時監看畫面，同時支援 Servo 實體按壓鍵盤。支援 curl 直接 POST 腳本 JSON 執行自動化。

---

## 系統架構

```
┌──────────────────────────────────────────────────────────────────┐
│ 電腦 2（控制端）                                                  │
│  FastAPI :8000                                                   │
│    ├── KeyboardSerial (38400) ──USB──► Nano_KB                   │
│    │                                     └──SoftSerial──► Pro Micro Serial1 (38400)
│    ├── ServoSerial (115200)  ──USB──► Nano_SRV ──PWM──► Servo×6 │
│    └── GET /stream ──────────────────────── Webcam               │
└──────────────────────────────────────────────────────────────────┘
                                               │ USB HID
                                        ┌──────▼──────────┐
                                        │  電腦 1（被控端）│
                                        │  接收 HID 鍵盤+鼠│
                                        └─────────────────┘
```

---

## 硬體清單

| 元件 | 數量 | 說明 |
|------|------|------|
| Arduino Pro Micro (ATmega32U4) | 1 | 插電腦1，USB HID 鍵盤+滑鼠 |
| Arduino Nano（Nano_KB）| 1 | 插電腦2，橋接 KBM 指令 |
| Arduino Nano（Nano_SRV）| 1 | 插電腦2，控制 Servo |
| SG90 Servo | 最多 6 | 實體按壓電腦1鍵盤（≤30cm） |
| USB 線 × 3 | 3 | 三個 Arduino 各一條 |
| 杜邦線 × 3 | 3 | TX、RX、GND（Nano_KB ↔ Pro Micro）|
| Webcam | 1 | 插電腦2，拍攝電腦1螢幕 |

---

## 接線

```
Nano_KB  D11 (TX) ──► Pro Micro RX1 (Pin 0)
Nano_KB  D10 (RX) ◄── Pro Micro TX1 (Pin 1)
Nano_KB  GND      ─── Pro Micro GND
Nano_KB  USB      ──► 電腦2

Pro Micro USB     ──► 電腦1（HID）

Nano_SRV D9  → S1,  D10 → S2,  D11 → S3
         D6  → S4,  D5  → S5,  D3  → S6
Nano_SRV USB ──► 電腦2
```

---

## 鮑率

| 連線段 | 鮑率 |
|--------|------|
| 電腦2 → Nano_KB USB | 38400 |
| Nano_KB SoftSerial → Pro Micro Serial1 | 38400 |
| Pro Micro Serial USB debug | 38400 |
| 電腦2 → Nano_SRV USB | 115200 |

---

## 專案結構

```
rks/
├── README.md
├── arduino/
│   ├── nano_keyboard/nano_keyboard.ino   ← Nano_KB 橋接
│   ├── pro_micro/pro_micro.ino           ← HID 鍵盤+滑鼠
│   └── nano_servo/nano_servo.ino         ← Servo 控制
├── server/
│   ├── main.py                           ← FastAPI 主程式
│   ├── keyboard_serial.py                ← KBM 串口 38400
│   ├── servo_serial.py                   ← SRV 串口 115200
│   ├── servo_router.py                   ← /srv/* 路由
│   ├── requirements.txt
│   └── static/
│       ├── index.html                    ← 整合版 UI
│       └── arch.html                     ← 架構圖 & MVP 計畫
└── tools/
    ├── test_promicro.py                  ← MVP Step 1
    └── test_nano_servo.py                ← MVP Step 2
```

---

## 安裝啟動

```bash
cd rks/server
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

瀏覽器：`http://電腦2:8000`
架構圖：`http://電腦2:8000/arch`

---

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/` | 主介面 |
| GET | `/arch` | 架構圖 |
| GET | `/stream` | MJPEG 串流 |
| GET | `/health` | 系統狀態（含 script.running） |
| GET | `/kbm/api/ports` | KBM 可用串口 |
| POST | `/kbm/api/connect` | `{"port":"COM3"}` |
| POST | `/kbm/api/disconnect` | — |
| POST | `/kbm/api/send` | `{"cmd":"TYPE:hello"}` |
| WS | `/ws/kbm` | KBM 狀態 + 腳本進度推播 |
| GET | `/srv/api/ports` | SRV 可用串口 |
| POST | `/srv/api/connect` | `{"port":"COM4"}` |
| POST | `/srv/api/disconnect` | — |
| POST | `/srv/api/attach` | `{"sid":1,"pin":9}` |
| POST | `/srv/api/detach` | `{"sid":1}` |
| POST | `/srv/api/attach_all` | `{"servos":{"1":9,"2":10}}` |
| POST | `/srv/api/detach_all` | — |
| POST | `/srv/api/run` | SRV-only 腳本 |
| POST | `/srv/api/stop` | 停止 Servo |
| POST | `/srv/api/command` | 單步 Servo |
| WS | `/ws/srv` | SRV 狀態推播 |
| **POST** | **`/script/run`** | **混合腳本（SRV+KBD+MSE）** |
| **POST** | **`/script/stop`** | **停止混合腳本** |
| GET | `/script/status` | `{"running":true/false}` |

---

## 混合腳本 JSON 格式

Script Editor 的 **Export** 直接輸出此格式，可用 curl 執行：

```bash
# Windows PowerShell / CMD
curl.exe -X POST http://127.0.0.1:8000/script/run ^
  -H "Content-Type: application/json" ^
  --data-binary "@exported_script.json"

# Linux / macOS
curl -X POST http://127.0.0.1:8000/script/run \
  -H "Content-Type: application/json" \
  --data-binary "@exported_script.json"
```

JSON 格式：

```json
{
  "loop": false,
  "servos": { "1": 9, "2": 10 },
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
      "delay_ms": 200,
      "cmd_type": "COMBO",
      "mod1": "CTRL",
      "mod2": "",
      "key": "C"
    },
    {
      "type": "kbd",
      "delay_ms": 100,
      "cmd_type": "TYPE",
      "text": "hello world"
    },
    {
      "type": "kbd",
      "delay_ms": 0,
      "cmd_type": "KEY",
      "key": "ENTER"
    },
    {
      "type": "mse",
      "delay_ms": 100,
      "action": "MOVE",
      "x": 100,
      "y": -50
    },
    {
      "type": "mse",
      "delay_ms": 50,
      "action": "CLICK",
      "btn": "L"
    },
    {
      "type": "mse",
      "delay_ms": 0,
      "action": "SCROLL",
      "amount": -3
    }
  ]
}
```

也可以直接給 `cmd` 字串（KBD/MSE 都支援）：

```json
{ "type": "kbd", "delay_ms": 0, "cmd": "COMBO:CTRL+ALT+DELETE" }
{ "type": "mse", "delay_ms": 0, "cmd": "MOUSE:CLICK L" }
```

### 停止執行中的腳本

```bash
curl -X POST http://127.0.0.1:8000/script/stop
```

### 查詢腳本狀態

```bash
curl http://127.0.0.1:8000/script/status
# {"running": true}
```

---

## KBM 指令協議（38400 baud）

| 指令 | 說明 |
|------|------|
| `TYPE:<text>` | 輸入文字 |
| `KEY:<name>` | ENTER ESC BACKSPACE TAB DELETE F1–F12 等 |
| `DOWN:<mod>` | 按住 CTRL SHIFT ALT GUI |
| `UP:<mod>` | 放開修飾鍵 |
| `COMBO:<m>+<k>` | 組合鍵，例 `COMBO:CTRL+C` |
| `RELEASEALL` | 釋放所有按鍵 |
| `MOUSE:MOVE dx dy` | 相對移動 −127~127 |
| `MOUSE:CLICK L/R/M` | 單擊 |
| `MOUSE:DBLCLICK L` | 雙擊 |
| `MOUSE:DOWN/UP L` | 按住/放開 |
| `MOUSE:SCROLL n` | 滾輪，正=上 負=下 |
| `PING` | 連線測試 |

---

## MVP 驗證計畫

詳細步驟見 `http://localhost:8000/arch` → MVP 驗證計畫 tab

| Step | 說明 | 工具 |
|------|------|------|
| 1 | Pro Micro HID（via Nano_KB）| `python tools/test_promicro.py` |
| 2 | Nano_KB 橋接 | Arduino Serial Monitor 38400 |
| 3 | Nano_SRV Servo | `python tools/test_nano_servo.py` |
| 4 | FastAPI 串口連線 | `GET /health` |
| 5 | 完整端對端 + curl 腳本 | 網頁操作 + curl |
