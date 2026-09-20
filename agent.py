"""Zalo agent: deterministic CDP for the work, Jev for the judgement calls.

Design rule, borrowed from Third Hand: the model never gets to name a target.
Python builds the closed set of real conversations and real messages, Jev only
picks from it, and Python re-checks the pick before acting on it.

Usage:
    python agent.py groups [substring]
    python agent.py open  "<what you mean>"
    python agent.py find  "<what you mean>" "<keyword>"
    python agent.py ask   "<what you mean>" "<question>"
"""
from __future__ import annotations

import asyncio
import sys

from jev import Jev, JevError
from zalo import Conversation, Message, Zalo, fold, open_zalo

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


def render(message: Message) -> str:
    who = "Bạn" if message.outgoing else (message.sender or "?")
    return f"  [{message.time:>5}] {who}: {message.text}"


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

        if command in {"open", "find", "ask"}:
            if len(argv) < 3:
                print(__doc__)
                return 2
            query = argv[2]
            conversation = await resolve_conversation(z, jev, query, groups_only=False)
            if conversation is None:
                print(f"Không tìm được hội thoại nào khớp với {query!r}.")
                return 1
            title = await z.open_conversation(conversation.id)
            print(f"Đã mở: {title}  ({conversation.id})")

            if command == "open":
                return 0

            messages = await z.read_messages(limit=300)
            print(f"Đã đọc {len(messages)} tin nhắn.")

            if command == "find":
                if len(argv) < 4:
                    print("Thiếu từ khoá cần tìm.")
                    return 2
                keyword = argv[3]
                hits = [m for m in messages if fold(keyword) in fold(m.text)]
                print(f"{len(hits)} tin nhắn chứa {keyword!r}:")
                for m in hits[-20:]:
                    print(render(m))
                return 0

            question = argv[3] if len(argv) > 3 else query
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
