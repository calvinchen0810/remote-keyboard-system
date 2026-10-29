"""
tools/test_evt_flow.py
MVP Step 6 — EVT 事件驅動腳本驗證（控制端）

用途：
    1. 向控制端 FastAPI 發佈一個含獨立 EVT step 的腳本
  2. 腳本會等待被控端送出對應 EVT:<name>
  3. 你可以搭配 tools/send_event.ps1 在被控端送出事件

執行：
  python tools/test_evt_flow.py --url http://127.0.0.1:8000 --event HELLO
"""

from __future__ import annotations

import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_script(event_name: str) -> dict:
    return {
        "loop": False,
        "steps": [
            {
                "type": "kbd",
                "delay_ms": 0,
                "cmd_type": "TYPE",
                "text": f"Waiting for EVT:{event_name}...",
            },
            {
                "type": "evt",
                "delay_ms": 0,
                "evt": event_name,
            },
            {
                "type": "kbd",
                "delay_ms": 0,
                "cmd_type": "TYPE",
                "text": f"EVT:{event_name} received",
            },
            {
                "type": "kbd",
                "delay_ms": 0,
                "cmd_type": "KEY",
                "key": "ENTER",
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RKS EVT script test")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="RKS server base URL")
    parser.add_argument("--event", default="HELLO", help="Expected EVT name")
    parser.add_argument("--timeout", type=int, default=120, help="Wait timeout in seconds")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    script = build_script(args.event)

    print(f"Posting EVT script to {base}/script/run")
    try:
        resp = post_json(f"{base}/script/run", script)
    except (HTTPError, URLError) as exc:
        print(f"[ERR] unable to post script: {exc}")
        return 1

    print(f"[OK] script queued: {json.dumps(resp, ensure_ascii=False)}")
    print(f"Waiting up to {args.timeout}s for completion...")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            status = get_json(f"{base}/script/status")
        except Exception as exc:
            print(f"[WARN] status check failed: {exc}")
            time.sleep(1)
            continue

        print(f"  status: running={status.get('running')} queued={status.get('queued', 0)}")
        if not status.get("running") and int(status.get("queued", 0)) == 0:
            print("[OK] script finished")
            return 0
        time.sleep(1)

    print("[ERR] timeout waiting for EVT script completion")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
