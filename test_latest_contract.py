"""Pure-Python checks for the pieces `latest` stands on.

No Zalo, no network, no API key: every DOM row here is a literal of the shape
`_rendered_messages` returns, so the message model, the sender carry-over, the
newest-from-sender pick and the attachment window are all pinned without the
app running.
"""
from __future__ import annotations

from agent import as_dict, split_flags
from zalo import (Attachment, Message, attachments_after, attribute_senders,
                  latest_from)

OK = True


def check(label: str, condition: bool) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return condition


def row(msg_id: str, sender: str = "", text: str = "", outgoing: bool = False,
        attachments: list[dict] | None = None, qid: str = "") -> dict:
    return {"id": msg_id, "sender": sender, "text": text, "time": "",
            "outgoing": outgoing, "qid": qid, "attachments": attachments or []}


def check_timestamps() -> bool:
    """Zalo renders both id shapes, and both carry the same clock."""
    ok = True
    short = Message(id="bb_msg_id_1789736597562", sender="Thu Huyền", text="",
                    time="", outgoing=False)
    long = Message(id="bb_msg_id_1789736597562_619207842002092350_g694851834",
                   sender="Thu Huyền", text="", time="", outgoing=False)
    ok &= check("short id yields its epoch ms", short.timestamp_ms == 1789736597562)
    ok &= check("long id yields the same epoch ms", long.timestamp_ms == 1789736597562)
    ok &= check("both id shapes agree", short.timestamp_ms == long.timestamp_ms)

    junk = Message(id="bb_msg_id_draft", sender="", text="", time="", outgoing=False)
    ok &= check("an unparseable id is None, not a crash", junk.timestamp_ms is None)

    # The regression this guards: sorting used to collapse every long id to 0.
    ordered = sorted([long, short, junk], key=lambda m: m.timestamp_ms or 0)
    ok &= check("a None id sorts first instead of poisoning the order",
                ordered[0] is junk)

    keyed = Message(id="bb_msg_id_1", sender="", text="", time="", outgoing=False,
                    qid="8270921135534@1789736597562_61920784_g694851834")
    ok &= check("the resource key is the half after the @",
                keyed.key == "1789736597562_61920784_g694851834")
    ok &= check("no qid means no key", short.key == "")
    return ok


def check_sender_runs() -> bool:
    """Zalo prints a name once per run; the rest inherit it."""
    ok = True
    messages = attribute_senders([
        row("bb_msg_id_101", sender="Thu Huyền", text="đầu chuỗi"),
        row("bb_msg_id_102", text="cùng người gửi"),
        row("bb_msg_id_103", text="vẫn cùng người gửi"),
        row("bb_msg_id_104", sender="Phụ huynh A", text="người khác"),
        row("bb_msg_id_105", text="tin của tôi", outgoing=True),
        row("bb_msg_id_106", text="vẫn của tôi", outgoing=True),
    ])
    ok &= check("the named bubble keeps its name", messages[0].sender == "Thu Huyền")
    ok &= check("an unnamed bubble inherits the run",
                messages[1].sender == "Thu Huyền" and messages[2].sender == "Thu Huyền")
    ok &= check("a new name ends the run", messages[3].sender == "Phụ huynh A")
    ok &= check("an outgoing bubble is attributed to the user",
                messages[4].sender == "Bạn" and messages[5].sender == "Bạn")
    ok &= check("without carry-over the run would look like 6 senders",
                len({m.sender for m in messages}) == 3)
    return ok


def check_latest_from() -> bool:
    """The newest match wins, by clock and not by DOM order."""
    ok = True
    messages = attribute_senders([
        row("bb_msg_id_1789736597562", sender="Thu Huyền",
            text="bài tập về nhà ngày học thứ 40"),
        row("bb_msg_id_1789563556011", sender="Thu Huyền",
            text="bài tập về nhà ngày học thứ 39"),
        row("bb_msg_id_1789800000000", sender="Thu Huyền",
            text="cô gửi nhận xét btvn ạ"),
        row("bb_msg_id_1789900000000", sender="Phụ huynh A",
            text="con nộp bài tập về nhà ạ"),
    ])
    newest = latest_from(messages, "Thu Huyền")
    ok &= check("newest from a sender ignores DOM order",
                newest is not None and newest.id == "bb_msg_id_1789800000000")

    homework = latest_from(messages, "Thu Huyền", "ngày học thứ")
    ok &= check("a match phrase narrows to the newest matching one",
                homework is not None and homework.id == "bb_msg_id_1789736597562")
    ok &= check("another sender's message never matches",
                latest_from(messages, "Thu Huyền", "con nộp") is None)
    ok &= check("sender matching ignores diacritics and case",
                latest_from(messages, "thu huyen") is not None)
    ok &= check("an unknown sender yields None",
                latest_from(messages, "Không Có Ai") is None)
    return ok


def check_attachment_window() -> bool:
    """Files ride in their own bubbles moments after the text."""
    ok = True
    minute = 60_000
    base = 1789736597562
    messages = attribute_senders([
        row(f"bb_msg_id_{base}", sender="Thu Huyền", text="bài tập ngày học thứ 40"),
        row(f"bb_msg_id_{base + 4000}", qid=f"1@{base + 4000}_c_g1",
            attachments=[{"kind": "media", "key": f"{base + 4000}_c_g1"}]),
        row(f"bb_msg_id_{base + 200 * minute}", qid=f"1@{base + 200 * minute}_c_g1",
            attachments=[{"kind": "file", "key": f"{base + 200 * minute}_c_g1",
                          "name": "nhận xét.m4a"}]),
        row(f"bb_msg_id_{base - minute}", sender="Phụ huynh A",
            qid=f"2@{base - minute}_c_g1",
            attachments=[{"kind": "file", "key": f"{base - minute}_c_g1"}]),
    ])
    anchor = messages[0]
    near = attachments_after(messages, anchor, "Thu Huyền", minutes=90)
    ok &= check("an attachment 4s later is inside the window", len(near) == 1)
    ok &= check("an attachment 200min later falls outside",
                all(m.timestamp_ms == base + 4000 for m in near))

    wide = attachments_after(messages, anchor, "Thu Huyền", minutes=300)
    ok &= check("widening the window picks up the later one", len(wide) == 2)
    ok &= check("another sender's attachment is never collected",
                all(m.sender == "Thu Huyền" for m in wide))
    ok &= check("a message before the anchor is never collected",
                all((m.timestamp_ms or 0) >= base for m in wide))

    payload = as_dict(anchor, near)
    ok &= check("the JSON payload carries the message clock",
                payload["timestamp_ms"] == base)
    ok &= check("the JSON payload flattens attachments with their carrier",
                len(payload["attachments"]) == 1
                and payload["attachments"][0]["message_id"] == f"bb_msg_id_{base + 4000}")
    ok &= check("the JSON payload dates each attachment",
                payload["attachments"][0]["sent_at"].startswith("20"))
    return ok


def check_flags() -> bool:
    """The new commands take flags; the old ones stay positional."""
    ok = True
    positional, flags = split_flags(
        ["nhóm lớp", "--from", "Thu Huyền", "--match", "ngày học thứ",
         "--attach-window", "90", "--json"])
    ok &= check("positional arguments survive", positional == ["nhóm lớp"])
    ok &= check("valued flags are captured", flags["from"] == "Thu Huyền")
    ok &= check("a phrase with spaces stays one value",
                flags["match"] == "ngày học thứ")
    ok &= check("a bare flag becomes true", flags["json"] == "true")

    plain, none = split_flags(["nhóm cờ", "bàn cờ giá bao nhiêu"])
    ok &= check("the old positional commands are untouched",
                plain == ["nhóm cờ", "bàn cờ giá bao nhiêu"] and none == {})
    return ok


def check_expiry() -> bool:
    ok = True
    gone = Attachment(kind="file", key="1_2_g3", state="File không tồn tại")
    live = Attachment(kind="file", key="1_2_g3", state="Tải về để xem lâu dài",
                      downloadable=True)
    ok &= check("Zalo's own label marks an expired file", gone.expired)
    ok &= check("a downloadable file is not expired", not live.expired)
    return ok


def main() -> int:
    ok = True
    for label, fn in [("message id and key", check_timestamps),
                      ("sender runs", check_sender_runs),
                      ("newest from a sender", check_latest_from),
                      ("attachment window", check_attachment_window),
                      ("flag parsing", check_flags),
                      ("attachment expiry", check_expiry)]:
        print(f"\n{label}")
        ok &= fn()
    print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
