"""End-to-end smoke test for a running local Web UI server."""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

from websockets.sync.client import connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--prompt", default="检查注册功能")
    args = parser.parse_args(argv)
    parsed = urlsplit(args.url)
    origin = f"http://{parsed.hostname}:{parsed.port or 80}"
    with connect(args.url, origin=origin) as websocket:
        ready = json.loads(websocket.recv())
        websocket.send(
            json.dumps(
                {"type": "run", "text": args.prompt},
                ensure_ascii=False,
            )
        )
        event_types: list[str] = []
        assistant_payloads: list[dict] = []
        while "turn.completed" not in event_types:
            message = json.loads(websocket.recv())
            if message.get("type") == "event":
                event = message["event"]
                event_types.append(event["event_type"])
                if event["event_type"] == "assistant.message":
                    assistant_payloads.append(event["payload"])

    print("websocket_smoke=passed")
    print(f"session={ready['session_id']}")
    print(f"events={len(event_types)}")
    print(f"tool_requested={'tool.requested' in event_types}")
    print(f"reasoning_visible={'assistant.reasoning_delta' in event_types}")
    browser_has_raw = any(
        "reasoning" in payload or "reasoning_details" in payload
        for payload in assistant_payloads
    )
    print(f"browser_has_raw_reasoning={browser_has_raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
