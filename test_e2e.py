"""End-to-end: real Zalo over CDP, with a stubbed Jev standing in for the model.

The stub picks whichever candidate best matches by name, so the run proves the
whole path: enumerate real conversations, shortlist, let the model choose, then
re-check the choice, open the real group and read its real messages.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import resolve_conversation, shortlist
from jev import Jev
from zalo import fold, open_zalo

WANT = "hblab onsite korea"
CALLS: list[str] = []


class Stub(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key, spec = next(iter(body["questions"].items()))
        CALLS.append(key)
        criteria = spec["criteria"]
        best = max(criteria.items(),
                   key=lambda kv: sum(w in fold(kv[1]) for w in WANT.split()))
        payload = json.dumps({"answers": {key: {"choice": best[0]}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


def mask(text: str, keep: int = 34) -> str:
    text = (text or "").replace("\n", " ")
    return text[:keep] + ("…" if len(text) > keep else "")


async def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    jev = Jev(api_key="stub", endpoint=f"http://127.0.0.1:{server.server_port}/v1/systemone")

    cdp, z = await open_zalo()
    ok = True
    checks: list[tuple[str, bool]] = []

    def note(name: str, value: bool) -> bool:
        checks.append((name, bool(value)))
        return bool(value)

    try:
        everything = await z.list_conversations()
        print(f"1. Liệt kê: {len(everything)} hội thoại, "
              f"{sum(c.is_group for c in everything)} nhóm")
        ok &= note("co du hoi thoai", len(everything) > 50)

        short = shortlist(WANT, [c for c in everything if c.is_group])
        print(f"2. Shortlist: {len(short)} ứng viên, đầu bảng = {mask(short[0].name)}")
        ok &= note("shortlist <= 40", len(short) <= 40)

        chosen = await resolve_conversation(z, jev, WANT, groups_only=True)
        print(f"3. Jev chọn: {mask(chosen.name) if chosen else None} ({chosen.id if chosen else '-'})")
        ok &= note("jev chon dung nhom", chosen is not None and fold("onsite korea") in fold(chosen.name))

        title = await z.open_conversation(chosen.id)
        print(f"4. Mở thật: {mask(title, 40)}")
        ok &= note("mo dung nhom", fold("onsite korea") in fold(title))

        messages = await z.read_messages(limit=120)
        with_text = [m for m in messages if m.text]
        senders = {m.sender for m in messages if m.sender}
        print(f"5. Đọc: {len(messages)} tin, {len(with_text)} có chữ, "
              f"{len(senders)} người gửi")
        ok &= note("doc duoc tin nhan", len(messages) > 10 and len(with_text) > 0)

        hits = [m for m in messages if fold("korea") in fold(m.text)]
        print(f"6. Lọc 'korea': {len(hits)} tin khớp")
        for m in hits[-2:]:
            print(f"     [{m.time}] {mask(m.sender, 14)}: {mask(m.text, 40)}")

        print(f"\nSố lần gọi model: {len(CALLS)} ({', '.join(CALLS)})")
        for name, value in checks:
            print(f"   {'PASS' if value else 'FAIL'}  {name}")
        print("KET QUA:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await cdp.__aexit__()
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
