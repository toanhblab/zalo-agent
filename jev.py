"""Bounded decisions via TypeSafe's Jev (System One) model.

Jev never writes free text. Every call hands it a closed set of options and
gets back one of them, or a calibrated probability. That is what makes it safe
to put in an automation loop: it cannot invent a group that does not exist.

The API key is read inside this process from the macOS Keychain (or the
TYPESAFE_API_KEY environment variable) and is never logged or printed.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
MAX_CHOICES = 255
MAX_REQUEST_BYTES = 24_000
NONE_KEY = "__none__"


class JevError(RuntimeError):
    pass


def load_api_key(service: str = "com.thirdhand.openrouter",
                 account: str = "api-key") -> str:
    """Read the key from the environment, else from the login Keychain.

    The value is returned to the caller in memory only. Nothing here writes it
    to disk, to a log, or to stdout.
    """
    env = os.environ.get("TYPESAFE_API_KEY")
    if env:
        return env.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JevError(f"Could not read the Keychain: {exc}") from exc
    key = (out.stdout or "").strip()
    if not key:
        raise JevError(
            "No TypeSafe API key. Set TYPESAFE_API_KEY, or store one under "
            f"service {service!r}, account {account!r} in the Keychain.")
    return key


def _redact(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


@dataclass
class Jev:
    api_key: str
    endpoint: str = ENDPOINT
    timeout: float = 15.0

    @classmethod
    def from_keychain(cls) -> "Jev":
        return cls(api_key=load_api_key())

    def ask(self, state: dict, questions: dict) -> dict:
        body = json.dumps({"model": MODEL, "state": state, "questions": questions})
        if len(body.encode()) > MAX_REQUEST_BYTES:
            raise JevError(f"Request is {len(body.encode())} bytes, over the "
                           f"{MAX_REQUEST_BYTES} byte budget. Shortlist harder.")
        request = urllib.request.Request(
            self.endpoint, data=body.encode(), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = _redact(exc.read().decode("utf-8", "replace")[:400], self.api_key)
            raise JevError(f"Jev rejected the request (HTTP {exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise JevError(f"Jev is unreachable: {exc.reason}") from exc
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise JevError("Jev returned no answers block.")
        return answers

    # ------------------------------------------------------------- helpers
    def choose(self, key: str, criteria: dict[str, str], instructions: str,
               state: dict, allow_none: bool = True) -> str | None:
        """Pick one key from `criteria`, or None when Jev declines."""
        if not criteria:
            return None
        options = dict(list(criteria.items())[: MAX_CHOICES - 1])
        if allow_none:
            options[NONE_KEY] = "None of these matches the request"
        answers = self.ask(state, {key: {"type": "choice", "criteria": options,
                                         "instructions": instructions}})
        choice = (answers.get(key) or {}).get("choice")
        if choice is None or choice not in options:
            raise JevError(f"Jev returned an out-of-schema choice for {key!r}.")
        return None if choice == NONE_KEY else choice

    def score(self, key: str, instructions: str, state: dict) -> float:
        """Return a calibrated probability in [0, 1]."""
        answers = self.ask(state, {key: {"type": "noul",
                                         "instructions": instructions}})
        value = (answers.get(key) or {}).get("noul")
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise JevError(f"Jev returned an invalid probability for {key!r}.")
        return float(value)
