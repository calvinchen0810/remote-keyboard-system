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
- `TESSERACT_CMD` (optional full path to `tesseract.exe`; required if OCR engine cannot be found automatically)

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

### 2) FIFO Script Queue + Event Bus

- All submitted scripts enter one FIFO queue, including scripts with `evt` steps. Submit A, wait for its HTTP response, then submit B and C to guarantee A -> B -> C order. Concurrent HTTP requests are ordered by server lock acquisition, not client send time.
- An HTTP success response means imported/enqueued, not execution completed. `/script/status` reports the active runner and number of queued scripts.
- Only the active script runs its steps and waits for its events. A waiting for an image blocks B and C even if their images are already visible.
- New submissions no longer enter the EVT Pool. Legacy pool endpoints remain available for compatibility.
- The right-hand **FIFO Queue** summary replaces the obsolete EVT Pool panel. It shows Idle/Running/Waiting/Paused, the current waiting/paused event and step, and the number of later queued scripts (excluding the active job). It is read-only, not an event history or a queue editor. `/script/status.waiting` restores the active wait after reload; WebSocket transitions and status polling keep the summary current. Stop/clear resets it to Idle and zero queued.
- EVT/IMG gates wait indefinitely until they match. There is no automatic timeout, countdown or automatic pause; the old `DEFAULT_EVT_TIMEOUT_MS` environment setting no longer controls script waits. Later jobs remain queued while the current gate waits.
- Use **PAUSE** / **PAUSE ACTIVE** or `POST /script/pause` to suspend the active FIFO at safe checkpoints. Current conditions are disarmed, not deleted. **RESUME** / **RESUME ACTIVE** or `POST /script/resume` continues the same step without replaying earlier KBD/SRV/MSE actions. Events buffered for the waiting gate during pause are discarded; condition hit counts/cooldown reset on resume.
- `/script/status` includes `paused` and manual `pause` metadata with unique `pause_id`, active job, optional event/step and timestamp. `running` stays true while suspended. RESUME must echo that pause_id; missing/stale IDs, duplicate resume or no paused job return 409. WebSocket `script_paused` / `script_resumed` and status polling restore the UI after reload, while superseded polls cannot restore a stopped pause.
- Manual pause freezes script delays and prevents later queued KBM writes. Already dispatched hardware actions cannot be recalled; in-progress recognition or servo operations may finish, but later script work waits for resume. Pause at a terminal job transition returns 409; try again after the next job becomes active. OCR/IMAGE_MOVE retain their separate recognition/search safety timeouts; those do not trigger automatic PAUSED state.
- `/script/stop` and clear cancel the paused job and discard queued jobs. Other fatal execution failures keep their existing behavior; this pause policy specifically covers EVT/IMG waits. A script with `loop: true` otherwise keeps its turn until stopped.
- Invalid servo mapping IDs are rejected before enqueueing. Unexpected per-script initialization/execution errors are reported without stranding later queued scripts.

### 3) Script Editor: Edit / Execution Status

- **Edit** retains the local draft. **Execution Status** is a separate read-only view of submitted work and never loads steps into the draft. KBD TYPE text is shown in full (not masked), escaped as text; avoid sharing this screen if scripts contain secrets.
- Each `/script/run` submission returns a generated `job_id`. An optional top-level `name` (1-80 characters) appears in the job selector; unnamed jobs show a shortened ID. The backend cannot infer the filename used by curl `@file.json`. The editor's optional Name field is preserved on export/import.
- **Follow Active** selects the active job as FIFO advances. Selecting a queued job turns follow off and previews immutable submitted steps without arming conditions, changing order or running anything. No job editing, reorder, delete, rerun or history database is provided.
- The view highlights current/completed/failed/pending steps, loop iteration and waiting/recognition/location phase. EVT/IMG shows **no timeout**; remaining time applies only to bounded OCR/image search. It includes OCR results, IMAGE_MOVE centers/HID coordinates and condition name/ROI/score/threshold/hits; thumbnails use matching SNAP data when still available. Queued condition previews show inactive status and no borrowed live runtime from another job.
- `/script/status.runtime` and WebSocket `script_runtime` supply lightweight, revisioned metadata (active job and queued list, no full steps/templates). `GET /script/jobs/{job_id}` returns read-only step/condition detail; inline template base64 is omitted. Old/retired sessions and superseded detail requests are rejected client-side.
- Status polling while this view is open refreshes condition scores and countdowns. Refresh/reconnect restores current submitted work; the local editing draft is not persisted by this feature. Completed jobs are removed rather than retained as history. Stop/clear removes the active/queued records; clear also resets the editor as before.
- **STOP** cancels the active job and clears FIFO; **PAUSE ACTIVE / RESUME ACTIVE** control the active job, never the selected queued preview. Manual resume remains pause_id-bound; hardware cancellation guards are preserved.

Example named request:

```json
{"name":"Unlock dialog","steps":[{"type":"evt","evt":"HELLO"},{"type":"kbd","cmd_type":"TYPE","text":"0037"}]}
```

### 4) Visual Monitor

- Condition evaluates at about 3 fps
- On match edge, emits `IMG.MATCH.<condition_id>`
- Broadcasts visual events by websocket
- Imported conditions stay disarmed until their script becomes active. Only that script's imported or referenced conditions can be armed during its turn; manually arming another condition during execution has no effect.
- At every script boundary, old visual events are discarded. Active conditions receive fresh hit counts, edge state and cooldown. Manual pause disarms them while retaining the same job; resume rearms fresh runtime. Merely waiting never times out or disarms a condition. Completion/cancellation disarms them before a job ends.
- If A and B use the same image, B can match the image still on screen after A completes. B must accumulate its own `min_hits` through fresh evaluations; it never reuses A's match event or runtime. Disappearance/reappearance is not required.
- Each hit must use a distinct camera capture newer than condition activation. Re-reading one cached frame while the camera is stalled cannot satisfy `min_hits` or trigger the next script.
- Importing identical image bundles creates independent snapshot/condition IDs. Scripts referencing the same existing condition ID also receive fresh runtime on each turn.

### 5) Auto Attach Servo in Script Mode

- When a queued script becomes active, required servo IDs are auto-attached if pin mapping exists
- Queued scripts do not attach servos ahead of their turn

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
- conditions are created disarmed and are armed only when their queued script becomes active
- `IMG.MATCH.<old_id>` in steps is remapped to new imported IDs
- websocket emits visual sync event
- if camera is not running, server auto starts camera for IMG bundle import

---

## Curl Examples

### Image search and absolute movement (Windows)

Prerequisites: the confirmed Pro Micro `MOUSE:ABS` firmware, one Windows display, a capture-card image of the entire desktop with no borders/cropping, and matching source/capture aspect ratio. Keep the target layout stable during locate/move/click. Legacy MOVE/MOVE_TO behavior is unchanged.

1. Take a SNAP and select **MOV**. Draw around a distinctive target. Set threshold, stable samples and timeout; **Search ROI** is normalized `[left, top, width, height]` (default full frame). Image movement has no button setting and never clicks automatically.
2. **Test Locate** searches fresh captures and displays the match box, pixel center, normalized HID center and annotated image. It never moves or clicks.
3. **Add Step** creates `MSE / IMAGE_MOVE`. Alternatively add MSE, select IMAGE_MOVE and configure the target. **Target** reopens settings; drawing a new rectangle replaces the stored template, otherwise its embedded template is preserved.
4. Add a separate MSE **CLICK** or **DBLCLICK** step with L/R/M as needed. Its delay controls the interval after movement. Export/import or submit through `/script/run`; schema version 5 embeds the PNG, frame dimensions and settings in `image_move`, independent of SNAP IDs and IMG conditions.

```json
{
  "schema_version": 5,
  "steps": [{
    "type": "mse",
    "action": "IMAGE_MOVE",
    "delay_ms": 0,
    "image_move": {
      "template_b64": "<PNG base64 produced by SNAP selection>",
      "source_width": 1280,
      "source_height": 720,
      "search_roi": [0, 0, 1, 1],
      "threshold": 0.92,
      "timeout_ms": 5000,
      "stable_hits": 2
    }
  }, {"type":"mse","action":"DBLCLICK","btn":"L","delay_ms":100}]
}
```

The placeholder above is explanatory, not an executable template. Use exported JSON for real runs. Each stable sample must be a distinct capture newer than step start. Multiple matches, a low-detail template, aspect-ratio change or timeout aborts the current job. Narrow the search ROI instead of arbitrarily choosing duplicates. IMAGE_MOVE sends only MOUSE:ABS and requires OK; failure prevents later steps. CLICK/DBLCLICK are independent steps operating at the cursor's current position, so keep their interval short and avoid other pointer/window changes. All MSE serial writes have cancellation guards; stop/clear prevents queued late writes, but cannot recall transmitted commands. Other FIFO jobs retain their order.

IMAGE_CLICK and its old API/configuration are removed, not retained as aliases. Old IMAGE_CLICK imports/submissions are rejected rather than silently becoming movement or clicking. Recreate/export them using MOV and optional separate CLICK/DBLCLICK. Pro Micro firmware requires no change for this split.

**Windows physical acceptance:** On the controlled Windows PC, open `http://<controller-IP>:8000/static/click-target.html` and make the browser full-screen. Keep Windows in single-display mode, disable any capture-card scaling/cropping, and do not move the physical mouse during the run. The page has nine distinct visual targets and records the target, pixel coordinates and HID coordinates received from actual clicks.

- Select a target from SNAP, use Test Locate, and check the preview center is inside that target.
- Run one IMAGE_MOVE and confirm only the cursor moves; the page's click counter must not increment. Then run a separate CLICK or DBLCLICK with L/R and verify the expected action.
- Repeat at top-left, center and bottom-right, then at the Windows DPI settings and capture resolutions you intend to use.
- Submit A/B/C image-move scripts and confirm target order. Test stop/clear while searching with a deliberately absent target: no later clicks or queued script actions should occur.
- Duplicate the target on screen and confirm Test Locate rejects ambiguity; then narrow Search ROI and confirm a unique result.

The page itself sends no serial commands. Browser automation/mock-frame tests verify the software path, not physical HID accuracy; actual Windows/capture-card acceptance must be observed on the controlled PC.

### Four-digit OCR and keyboard variables

1. Install the Python requirements, then install [Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html) with English (`eng`) trained data. The Python package alone does not install the OCR executable.
2. On Windows, Tesseract is detected through PATH or the standard `Program Files/Tesseract-OCR` directory. For a custom location, set the environment variable before starting the server:

```powershell
$env:TESSERACT_CMD = 'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

3. Take a SNAP and choose its **OCR** action. Select only the four-digit region, set the variable name, and use **Test OCR** to recognize fresh live camera captures. **Add Step** saves the ROI in the script. Existing OCR steps can be edited through their **ROI** button using a new SNAP.
4. Add a KBD step, select **TYPE**, and choose the earlier OCR variable from **Variable...**, or enter `${digits}`. Add a separate KEY/ENTER step if needed.

```json
{
  "schema_version": 3,
  "loop": false,
  "steps": [
    {
      "type": "ocr",
      "delay_ms": 0,
      "roi": [0.05, 0.10, 0.20, 0.08],
      "variable": "digits",
      "timeout_ms": 5000,
      "stable_hits": 2
    },
    { "type": "kbd", "cmd_type": "TYPE", "text": "${digits}" },
    { "type": "kbd", "cmd_type": "KEY", "key": "ENTER" }
  ]
}
```

- ROI coordinates are normalized `[x, y, width, height]` relative to the raw camera image. The example coordinates must be replaced using the actual SNAP selection. Changing source aspect ratio/framing requires selecting the ROI again.
- The SNAP is only a setup reference. Runtime uses distinct captures newer than the OCR step, and the same four ASCII digits must appear in consecutive captures (default two). Invalid results reset the agreement count; no arbitrary substring extraction or letter-to-digit guessing is used.
- Results are strings, preserving `0037`. Variable names use letters, digits and underscores, cannot start with a digit, and are at most 32 characters.
- Variables belong only to the current script iteration. OCR clears its target before recognition; every loop starts empty. Undefined/malformed `${name}` placeholders abort before keyboard transmission. Substitution applies only to structured KBD TYPE `text`, not raw `cmd`, KEY, COMBO or mouse commands.
- OCR timeout or engine failure aborts the current script; the FIFO runner then advances to the next job. Stop/clear cancels waiting OCR without typing a late result. A cancelled background Tesseract call may finish its bounded timeout, but cannot submit keyboard commands.
- OCR steps export/import with all settings inline and do not require their setup SNAP. They may coexist with image-condition bundles. Older script formats remain supported; schema version 3 identifies OCR exports.
- `POST /visual/ocr/test` accepts the OCR settings object and returns `{ "ok": true, "variable": "digits", "value": "0037" }`. It does not change script variables. Invalid settings return 422, missing OCR dependencies return 503, and recognition timeout returns 408.
- PyInstaller includes the Python wrapper via the spec, but Tesseract executable, its DLLs and `tessdata/eng.traineddata` are external prerequisites. Install them on the target PC and configure `TESSERACT_CMD`; a Python-only install or EXE build is not enough. Existing non-OCR functions remain available without the engine.
- Real capture-card text needs calibration/testing. Whitelisting and agreement checks reduce malformed readings but do not guarantee OCR accuracy.

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

### Manually pause and resume the FIFO

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/script/pause
$status = Invoke-RestMethod http://127.0.0.1:8000/script/status
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/script/resume -ContentType 'application/json' -Body (@{pause_id=$status.pause.pause_id} | ConvertTo-Json)
```

The RESUME button supplies the ID automatically. This does not resubmit or restart the script. EVT/IMG waits indefinitely until matched or manually paused/stopped; resume continues the same step. Stop/clear discards the suspended job and its queue. The old retry endpoint is replaced by pause/resume. Restart the backend and refresh the browser to load this policy and controls.

### Clear snapshots, conditions and scripts

From the repository root, use the included `clear_command.json`:

```powershell
curl.exe -X POST http://127.0.0.1:8000/script/run -H "Content-Type: application/json" --data-binary "@clear_command.json"
```

```json
{
  "command": "clear",
  "targets": ["snapshots", "conditions", "scripts"]
}
```

- The command is processed directly, not placed in the script queue.
- It cancels and awaits the running script, discards queued scripts, clears the EVT Pool and buffered events, then deletes conditions (including persisted data and match runtime) and snapshots.
- All connected web pages clear their Script Editor, visual lists and progress display. Reload pages opened before this feature was installed once to load the new notification handler.
- All three targets are required. Unknown commands, partial/unknown/duplicate targets and extra fields such as `steps`, `loop` or `img_bundle` return HTTP 400 before clearing anything.
- Repeating the command is safe: empty pools return success with zero counts.
- Camera and serial connections, servo configuration and existing logs are preserved. Actions already sent to hardware are not guaranteed to stop.
- If conditions cannot be persisted, HTTP 500 is returned. Scripts have already been stopped, but conditions and snapshots are retained; fix storage access and retry.

Example response when already empty:

```json
{
  "ok": true,
  "command": "clear",
  "cleared": {
    "running_scripts": 0,
    "queued_scripts": 0,
    "pending_scripts": 0,
    "conditions": 0,
    "snapshots": 0
  }
}
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

### Script waiting or later scripts not starting

- Check the active script's `evt` step value and whether its image is visible
- For nonvisual events, emit the corresponding `EVT:<name>` from the target side
- Inspect `GET /script/status`; queued scripts cannot bypass the active script's event wait or loop
- Later scripts' conditions are intentionally disarmed until their turn

---

## Validation Tools

- `python tools/test_promicro.py`
- `python tools/test_nano_servo.py`
- `python tools/test_evt_flow.py`
- `server/env/Scripts/python.exe -m unittest discover -s tools -p test_clear_command.py -v`
- `node --test tools/test_script_editor.cjs`
- `server/env/Scripts/python.exe -m unittest discover -s tools -p test_ocr_digits.py -v`
- `server/env/Scripts/python.exe -m unittest discover -s tools -p test_image_move.py -v`
- `server/env/Scripts/python.exe -m unittest discover -s tools -p test_script_runtime.py -v`
- `powershell -ExecutionPolicy Bypass -File .\tools\send_event.ps1`
