"""Verify the Jev client against a local stub that speaks the TypeSafe contract.

This exercises everything except the real network hop: request shape, the byte
budget, choice validation, out-of-schema rejection, the none-of-these path, and
noul scoring. Run it with no API key and no internet.
"""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import shortlist
import jev as jev_module
from jev import NONE_KEY, Jev, JevError, load_api_key
from zalo import Conversation

RECEIVED: list[dict] = []
MODE = {"reply": "first"}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RECEIVED.append(body)
        questions = body["questions"]
        key, spec = next(iter(questions.items()))
        if spec["type"] == "noul":
            answers = {key: {"noul": 0.83}}
        elif MODE["reply"] == "none":
            answers = {key: {"choice": NONE_KEY}}
        elif MODE["reply"] == "bogus":
            answers = {key: {"choice": "not-an-option"}}
        else:
            answers = {key: {"choice": list(spec["criteria"])[0]}}
        payload = json.dumps({"answers": answers}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


def check(name: str, condition: bool) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    return condition


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/systemone"
    jev = Jev(api_key="test-key-not-real", endpoint=endpoint)

    conversations = [
        Conversation(id="g1", name="HBLAB Onsite Korea"),
        Conversation(id="g2", name="HBLAB x MOR"),
        Conversation(id="g3", name="Chess Meetup | HỘI QUÁN CỜ"),
        Conversation(id="544", name="Nhà hàng Phương Nam"),
    ]
    ok = True

    print("shortlist")
    picked = shortlist("hblab korea", conversations)
    ok &= check("ranks the two HBLAB groups first",
                {c.id for c in picked[:2]} == {"g1", "g2"})
    ok &= check("diacritics are folded",
                [c.id for c in shortlist("hoi quan co", conversations)][0] == "g3")

    print("choice")
    MODE["reply"] = "first"
    chosen = jev.choose("conversation",
                        {c.id: c.name for c in picked},
                        "Which conversation?", {"request": "hblab korea"})
    ok &= check("returns a real conversation id", chosen in {c.id for c in picked})
    sent = RECEIVED[-1]
    ok &= check("sends model jev-latest", sent["model"] == "jev-latest")
    ok &= check("offers a none-of-these option",
                NONE_KEY in sent["questions"]["conversation"]["criteria"])
    ok &= check("question type is choice",
                sent["questions"]["conversation"]["type"] == "choice")

    print("none path")
    MODE["reply"] = "none"
    ok &= check("declining maps to None",
                jev.choose("conversation", {"g1": "x"}, "?", {}) is None)

    print("schema guard")
    MODE["reply"] = "bogus"
    try:
        jev.choose("conversation", {"g1": "x"}, "?", {})
        ok &= check("rejects an out-of-schema choice", False)
    except JevError:
        ok &= check("rejects an out-of-schema choice", True)

    print("noul")
    MODE["reply"] = "first"
    score = jev.score("answers", "Does this answer the question?",
                      {"message": "Khoảng 500k bạn ạ"})
    ok &= check("returns a probability in range", 0.0 <= score <= 1.0)
    ok &= check("question type is noul",
                RECEIVED[-1]["questions"]["answers"]["type"] == "noul")

    print("byte budget")
    huge = {f"g{i}": "x" * 300 for i in range(300)}
    try:
        jev.choose("conversation", huge, "?", {})
        ok &= check("refuses an oversized request", False)
    except JevError as exc:
        ok &= check("refuses an oversized request", "budget" in str(exc))

    print("keychain identity")
    real_run, real_env = jev_module.subprocess.run, os.environ.pop("TYPESAFE_API_KEY", None)
    jev_module.subprocess.run = lambda *a, **k: SimpleNamespace(stdout="")
    try:
        load_api_key()
        ok &= check("reports a missing key", False)
    except JevError as exc:
        ok &= check("error names this project's Keychain service",
                    "com.toanhblab.zalo-agent" in str(exc))
        ok &= check("error names this project's Keychain account",
                    "typesafe-api-key" in str(exc))
    finally:
        jev_module.subprocess.run = real_run
        if real_env is not None:
            os.environ["TYPESAFE_API_KEY"] = real_env

    server.shutdown()
    print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
