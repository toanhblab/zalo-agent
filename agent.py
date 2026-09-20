"""Zalo agent: deterministic CDP for the work, Jev for the judgement calls.

Design rule, borrowed from Third Hand: the model never gets to name a target.
Python builds the closed set of real conversations and real messages, Jev only
picks from it, and Python re-checks the pick before acting on it.

Usage:
    python agent.py groups [substring]
    python agent.py open   "<what you mean>"
    python agent.py find   "<what you mean>" "<keyword>"
    python agent.py ask    "<what you mean>" "<question>"
    python agent.py latest "<what you mean>" --from "<display name>"
                           [--match "<phrase>"] [--attach-window 90] [--json]
    python agent.py attachments "<what you mean>" --from "<display name>"
                           --since YYYY-MM-DD --out <directory>

`latest` and `attachments` are fully deterministic and never call Jev: they are
the surface another app is meant to drive.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import shutil
import sys
from pathlib import Path

from jev import Jev, JevError
from zalo import (Conversation, Message, Zalo, attachments_after,
                  cached_attachment, fold, latest_from, open_zalo,
                  resource_root)

SHORTLIST = 40
RELEVANCE_THRESHOLD = 0.60


def shortlist(query: str, conversations: list[Conversation],
              limit: int = SHORTLIST) -> list[Conversation]:
    """Rank locally so the request stays inside Jev's byte budget."""
    needle = fold(query)
    words = [w for w in needle.split() if len(w) > 1]

    def rank(c: Conversation) -> tuple[int, int]:
        name = fold(c.name)
        hits = sum(1 for w in words if w in name)
        exact = 3 if needle and needle in name else 0
        return (exact * 10 + hits, -len(name))

    scored = sorted(conversations, key=rank, reverse=True)
    keep = [c for c in scored if rank(c)[0] > 0] or scored
    return keep[:limit]


async def resolve_conversation(z: Zalo, jev: Jev | None, query: str,
                               groups_only: bool = False) -> Conversation | None:
    """Turn a loose phrase into one real conversation."""
    everything = await z.list_conversations()
    pool = [c for c in everything if c.is_group] if groups_only else everything
    candidates = shortlist(query, pool)
    if not candidates:
        return None

    exact = [c for c in candidates if fold(c.name) == fold(query)]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    if jev is None:
        return candidates[0]

    criteria = {c.id: f"{c.name} [{'group' if c.is_group else 'direct'}]"
                for c in candidates}
    chosen = jev.choose(
        key="conversation",
        criteria=criteria,
        instructions=(
            f'Which Zalo conversation does the user mean by "{query}"? '
            "Match on the conversation name only. Choose none if no name plausibly "
            "refers to it."),
        state={"request": query, "app": "Zalo",
               "candidateCount": len(candidates), "totalConversations": len(pool)},
    )
    if chosen is None:
        return None
    # Python re-checks the model's pick against the real list.
    return next((c for c in candidates if c.id == chosen), None)


async def judge_messages(jev: Jev, question: str, messages: list[Message],
                         conversation: str, limit: int = 12) -> list[tuple[Message, float]]:
    """Score how well each candidate message answers the question."""
    judged: list[tuple[Message, float]] = []
    for message in messages[:limit]:
        if not message.text:
            continue
        probability = jev.score(
            key="answers",
            instructions=(f'Does this message answer the question "{question}"? '
                          "Judge only by the message text supplied."),
            state={"question": question, "conversation": conversation,
                   "sender": message.sender, "time": message.time,
                   "message": message.text[:600]},
        )
        judged.append((message, probability))
    return sorted(judged, key=lambda pair: pair[1], reverse=True)


def split_flags(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split `--name value` pairs out of a positional argument list.

    The existing commands are positional and stay that way; only the new ones
    take flags, so a full argparse rewrite would churn more than it buys.
    """
    positional: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            name = token[2:]
            if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                flags[name] = argv[index + 1]
                index += 2
            else:
                flags[name] = "true"
                index += 1
        else:
            positional.append(token)
            index += 1
    return positional, flags


def iso(message: Message) -> str:
    stamp = message.timestamp_ms
    if stamp is None:
        return ""
    return datetime.datetime.fromtimestamp(stamp / 1000).isoformat(timespec="seconds")


def as_dict(message: Message, attachments: list[Message]) -> dict:
    """The shape another app consumes: one message plus its nearby files."""
    return {
        "id": message.id,
        "timestamp_ms": message.timestamp_ms,
        "sent_at": iso(message),
        "sender": message.sender,
        "text": message.text,
        "attachments": [
            {"kind": a.kind, "key": a.key, "name": a.name, "size": a.size,
             "state": a.state, "downloadable": a.downloadable,
             "message_id": carrier.id, "sent_at": iso(carrier)}
            for carrier in attachments for a in carrier.attachments
        ],
    }


def render(message: Message) -> str:
    who = "Bạn" if message.outgoing else (message.sender or "?")
    return f"  [{message.time:>5}] {who}: {message.text}"


async def run_latest(z: Zalo, conversation: Conversation, title: str,
                     flags: dict[str, str]) -> int:
    """Print the newest message from one sender. Deterministic; no Jev."""
    sender = flags.get("from", "")
    if not sender:
        print("Thiếu --from \"<tên hiển thị>\".")
        return 2
    match = flags.get("match")
    window = float(flags.get("attach-window", 90))
    days = float(flags["days"]) if "days" in flags else 30.0

    messages = await z.read_messages(limit=2000, max_scrolls=200, deep=True,
                                     max_age_days=days)
    message = latest_from(messages, sender, match)
    if message is None:
        what = f" khớp {match!r}" if match else ""
        print(f"Không thấy tin nào của {sender!r}{what} trong {days:g} ngày gần đây "
              f"({len(messages)} tin đã quét).")
        return 1

    nearby = attachments_after(messages, message, sender, window)
    if flags.get("json") == "true":
        print(json.dumps(as_dict(message, nearby), ensure_ascii=False, indent=1))
        return 0

    print(f"\nNhóm      : {title}")
    print(f"Người gửi : {message.sender}")
    print(f"Gửi lúc   : {iso(message)}  (id {message.id})")
    print(f"Đã quét   : {len(messages)} tin, {days:g} ngày gần nhất")
    print("-" * 72)
    print(message.text)
    print("-" * 72)
    files = [a for carrier in nearby for a in carrier.attachments]
    print(f"Đính kèm trong {window:g} phút sau tin: {len(files)}")
    for carrier in nearby:
        for a in carrier.attachments:
            label = a.name or f"<{a.kind}>"
            note = a.state or ("tải được" if a.downloadable else "")
            print(f"  [{iso(carrier)[11:]}] {label}  {note}")
    return 0


async def run_attachments(z: Zalo, conversation: Conversation, title: str,
                          flags: dict[str, str]) -> int:
    """Copy one sender's attachments out of Zalo's cache into `--out`."""
    sender = flags.get("from", "")
    since = flags.get("since", "")
    out = flags.get("out", "")
    if not (sender and since and out):
        print("Cần đủ --from \"<tên>\" --since YYYY-MM-DD --out <thư mục>.")
        return 2
    try:
        start = datetime.datetime.strptime(since, "%Y-%m-%d")
    except ValueError:
        print(f"--since {since!r} không phải dạng YYYY-MM-DD.")
        return 2

    since_ms = int(start.timestamp() * 1000)
    messages = await z.read_messages(limit=5000, max_scrolls=300, deep=True,
                                     until_ts=since_ms)
    needle = fold(sender)
    carriers = [m for m in messages
                if m.attachments and fold(m.sender) == needle
                and (m.timestamp_ms or 0) >= since_ms]
    total = sum(len(m.attachments) for m in carriers)
    print(f"{len(messages)} tin đã quét; {sender} có {total} tệp từ {since}.")

    root = resource_root(conversation.id)
    if root is None:
        print(f"Không thấy kho tệp cục bộ của Zalo cho {conversation.id}.")
        return 1

    out_dir = Path(out).expanduser()
    index: list[dict] = []
    saved = expired = failed = drifted = 0
    for carrier in carriers:
        # The pane belongs to a person who may be using Zalo right now, so the
        # open conversation is re-checked on every message rather than once.
        await z.ensure_conversation(conversation.id, title)
        day = iso(carrier)[:10]
        for attachment in carrier.attachments:
            entry = {"message_id": carrier.id, "sent_at": iso(carrier),
                     "sender": carrier.sender, "kind": attachment.kind,
                     "key": attachment.key, "name": attachment.name,
                     "size": attachment.size, "state": attachment.state}
            source = cached_attachment(root, attachment.key)
            reason = ""
            if source is None:
                source, reason = await z.fetch_attachment(conversation.id,
                                                          attachment.key)
            if source is None:
                if reason == "not-in-dom":
                    drifted += 1
                    entry["result"] = "LỖI: bong bóng không có trong DOM"
                    print(f"  ! {attachment.key}: bong bóng không có trong DOM "
                          f"(khung chat đã trôi hoặc lịch sử chưa nạp tới)")
                elif reason == "expired" or attachment.expired:
                    expired += 1
                    entry["result"] = "hết hạn trên máy chủ Zalo"
                else:
                    failed += 1
                    entry["result"] = f"không tải được ({reason})"
                index.append(entry)
                continue
            # Only a file bubble names its payload. Zalo stores a photo or
            # video without any extension, so recover one from the resource
            # folder it was filed under.
            suffix = source.suffix or {"video": ".mp4", "picture": ".jxl"}.get(
                source.parent.name, "")
            name = attachment.name or f"{day}-{attachment.key.split('_')[0]}{suffix}"
            destination = out_dir / day / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
            saved += 1
            entry["result"] = "đã lưu"
            entry["file"] = str(destination.relative_to(out_dir))
            index.append(entry)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.json").write_text(
        json.dumps({"conversation": title, "conversation_id": conversation.id,
                    "sender": sender, "since": since, "items": index},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Đã lưu {saved}, hết hạn {expired}, hỏng {failed}, "
          f"không thấy trong DOM {drifted}. Chỉ mục: {out_dir/'index.json'}")
    if drifted:
        print(f"! {drifted} tệp không đọc được vì bong bóng vắng mặt — chạy lại "
              f"để lấy nốt.")
    # A bubble that was never rendered is a fault, not an empty result.
    return 1 if drifted or (total and not saved) else 0


async def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]

    try:
        jev: Jev | None = Jev.from_keychain()
    except JevError as exc:
        print(f"! Jev unavailable, falling back to local matching: {exc}")
        jev = None

    cdp, z = await open_zalo()
    try:
        if command == "groups":
            needle = argv[2] if len(argv) > 2 else ""
            convs = await z.list_conversations()
            groups = [c for c in convs if c.is_group]
            if needle:
                groups = [c for c in groups if fold(needle) in fold(c.name)]
            print(f"{len(groups)} nhóm:")
            for c in groups:
                print(f"  {c.id:<22} {c.time:>9}  {c.name}")
            return 0

        if command in {"open", "find", "ask", "latest", "attachments"}:
            positional, flags = split_flags(argv[2:])
            if not positional:
                print(__doc__)
                return 2
            query = positional[0]
            # `latest` and `attachments` are the machine-facing surface, so they
            # must not depend on a model to decide which conversation is meant.
            picker = None if command in {"latest", "attachments"} else jev
            conversation = await resolve_conversation(z, picker, query,
                                                      groups_only=False)
            if conversation is None:
                print(f"Không tìm được hội thoại nào khớp với {query!r}.")
                return 1
            title = await z.open_conversation(conversation.id)
            print(f"Đã mở: {title}  ({conversation.id})")

            if command == "open":
                return 0

            # The port drives the human's own Zalo, so the pane can drift at any
            # moment. Re-check it rather than trusting the open above.
            title = await z.ensure_conversation(conversation.id, title)

            if command == "latest":
                return await run_latest(z, conversation, title, flags)
            if command == "attachments":
                return await run_attachments(z, conversation, title, flags)

            messages = await z.read_messages(limit=300)
            print(f"Đã đọc {len(messages)} tin nhắn.")

            if command == "find":
                if len(positional) < 2:
                    print("Thiếu từ khoá cần tìm.")
                    return 2
                keyword = positional[1]
                hits = [m for m in messages if fold(keyword) in fold(m.text)]
                print(f"{len(hits)} tin nhắn chứa {keyword!r}:")
                for m in hits[-20:]:
                    print(render(m))
                return 0

            question = positional[1] if len(positional) > 1 else query
            words = [w for w in fold(question).split() if len(w) > 2]
            candidates = [m for m in messages
                          if m.text and any(w in fold(m.text) for w in words)]
            candidates = (candidates or [m for m in messages if m.text])[-12:]
            if jev is None:
                print("Không có Jev; đây là các tin khớp từ khoá:")
                for m in candidates:
                    print(render(m))
                return 0
            ranked = await judge_messages(jev, question, candidates, title)
            strong = [(m, p) for m, p in ranked if p >= RELEVANCE_THRESHOLD]
            print(f"\nJev chấm {len(ranked)} tin, {len(strong)} tin vượt ngưỡng:")
            for m, p in (strong or ranked[:3]):
                print(f"  [{p:.2f}]" + render(m)[1:])
            return 0

        print(__doc__)
        return 2
    finally:
        await cdp.__aexit__()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
