"""Exercise the `ask` path: real messages scored by a stubbed noul model.

The stub returns a probability derived from real word overlap, so the ranking
you see is driven by the actual message text read out of Zalo.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import judge_messages, resolve_conversation
from jev import Jev
from zalo import fold, open_zalo

GROUP = "chess meetup"
QUESTION = "bàn cờ giá bao nhiêu"
CALLS: list[str] = []


class Stub(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key, spec = next(iter(body["questions"].items()))
        CALLS.append(spec["type"])
        if spec["type"] == "noul":
            text = fold(str(body["state"].get("message", "")))
            words = [w for w in fold(QUESTION).split() if len(w) > 2]
            hits = sum(1 for w in words if w in text)
            answer = {key: {"noul": min(0.99, hits / max(1, len(words)))}}
        else:
            criteria = spec["criteria"]
            best = max(criteria.items(),
                       key=lambda kv: sum(w in fold(kv[1]) for w in GROUP.split()))
            answer = {key: {"choice": best[0]}}
        payload = json.dumps({"answers": answer}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


def mask(text: str, keep: int = 40) -> str:
    text = (text or "").replace("\n", " ")
    return text[:keep] + ("…" if len(text) > keep else "")


async def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    jev = Jev(api_key="stub",
              endpoint=f"http://127.0.0.1:{server.server_port}/v1/systemone")

    cdp, z = await open_zalo()
    try:
        conversation = await resolve_conversation(z, jev, GROUP, groups_only=True)
        if conversation is None:
            print("FAIL: không tìm được nhóm")
            return 1
        title = await z.open_conversation(conversation.id)
        print(f"Nhóm: {mask(title)}")

        messages = await z.read_messages(limit=200)
        words = [w for w in fold(QUESTION).split() if len(w) > 2]
        candidates = [m for m in messages
                      if m.text and any(w in fold(m.text) for w in words)]
        candidates = (candidates or [m for m in messages if m.text])[-12:]
        print(f"Đọc {len(messages)} tin, đưa {len(candidates)} tin lên model")

        ranked = await judge_messages(jev, QUESTION, candidates, title)
        print(f"Model chấm {len(ranked)} tin ({CALLS.count('noul')} lượt noul):")
        for message, probability in ranked[:5]:
            print(f"  [{probability:.2f}] {mask(message.sender, 14)}: "
                  f"{mask(message.text)}")

        ok = bool(ranked) and all(0.0 <= p <= 1.0 for _, p in ranked)
        ok = ok and ranked == sorted(ranked, key=lambda pair: pair[1], reverse=True)
        print("KET QUA:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await cdp.__aexit__()
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
