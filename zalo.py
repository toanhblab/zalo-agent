"""Read-oriented control of the Zalo desktop client over CDP.

Zalo is an Electron app. Launch it with a debugging port first:

    env -u ELECTRON_RUN_AS_NODE open -a /Applications/Zalo.app \
        --args --remote-debugging-port=9222

Everything here reads the real DOM, so there is no OCR and no coordinate
guessing. Only `open_conversation` mutates the UI, and it does so by
dispatching a click on a row that is currently rendered.
"""
from __future__ import annotations

import asyncio
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from cdp import CDP, CDPError, connect

DEFAULT_PORT = 9222

# --- DOM contract, kept in one place so a Zalo update is cheap to patch ----
SEL = {
    "list": "#conversationList .ReactVirtualized__Grid__innerScrollContainer",
    "scroller": "#conversationList",
    "row": ".msg-item",
    "row_id": "anim-data-id",
    "row_name": ".conv-item-title__name .truncate, .conv-item-title__name",
    "row_preview": ".z-conv-message__preview-message",
    "row_time": ".preview-time",
    "row_click": ".conv-item",
    "chat_view": "#chatView",
    "chat_scroller": "#chatViewContainer",
    "chat_header": "#header .header-title",
    "message": '[id^="bb_msg_id_"]',
    # The body of a text message lives in the rich-text container. `span.text`
    # looks right but only ever matches the filename node of an attachment
    # bubble, so it reads every text message as empty.
    "msg_text": '[data-component="message-text-content"]',
    "msg_sender": ".message-sender-name-content",
    "msg_time": ".card-send-time__sendTime",
    "msg_side": '[class*="floating-menu-wrapper--"]',
    "msg_qid": "[data-qid]",
    "msg_file": ".file-message-v2",
    "msg_file_name": ".file-message__content-title",
    "msg_file_size": ".file-message__content-info-size",
    "msg_file_state": ".cloud-title",
    "msg_file_actions": ".file-message__content-actions",
    "msg_file_download": "a.file-message__actions.download",
    "msg_photo": '[data-component="photo"]',
    "onboard": "#chatOnboard",
}

# Zalo keeps every attachment it has fetched under a per-conversation resource
# tree, filed by the message key. That key is exactly the half of `data-qid`
# after the "@", which is how a cached blob maps back to the message.
ZALO_MEDIA = Path.home() / "Library/Application Support/ZaloData/media"
RESOURCE_KINDS = ("file", "video", "picture")


def fold(text: str) -> str:
    """Casefold, strip Vietnamese diacritics, and normalise whitespace.

    Zalo renders conversation titles with non-breaking spaces, so a query typed
    with ordinary spaces never matches unless every kind of space collapses to
    a single plain one.
    """
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    spaced = "".join(" " if unicodedata.category(c) == "Zs" or c in "\t\r\n" else c
                     for c in stripped)
    collapsed = " ".join(spaced.split())
    return collapsed.replace("đ", "d").replace("Đ", "D").casefold()


@dataclass
class Conversation:
    id: str
    name: str
    preview: str = ""
    time: str = ""
    unread: str = ""

    @property
    def is_group(self) -> bool:
        return self.id.startswith("g")


@dataclass
class Attachment:
    """One file, photo or video hanging off a message."""

    kind: str           # "file" for a named file, "media" for a photo or video
    key: str            # <sentMs>_<cliMsgId>_<groupId>, the resource-tree key
    name: str = ""      # only a file bubble carries a filename
    size: str = ""
    state: str = ""     # Zalo's own cloud label, e.g. "File không tồn tại"
    downloadable: bool = False

    @property
    def expired(self) -> bool:
        """Zalo drops attachments from its servers after roughly two weeks."""
        return not self.downloadable and "không tồn tại" in self.state


@dataclass
class Message:
    id: str
    sender: str
    text: str
    time: str
    outgoing: bool
    qid: str = ""
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def timestamp_ms(self) -> int | None:
        """Milliseconds since the epoch, taken from the element id.

        Zalo renders the same message under either `bb_msg_id_<ms>` or
        `bb_msg_id_<ms>_<cliMsgId>_<groupId>`, and it can switch between the two
        across re-renders. Only the leading field is the clock.
        """
        raw = self.id.removeprefix("bb_msg_id_").split("_")[0]
        return int(raw) if raw.isdigit() else None

    @property
    def key(self) -> str:
        """The resource-tree key Zalo files this message's attachments under."""
        return self.qid.split("@", 1)[1] if "@" in self.qid else ""


def attribute_senders(items: list[dict]) -> list[Message]:
    """Turn raw DOM rows into messages, carrying the sender across a run.

    Zalo prints a name once per run of consecutive messages from one person, so
    every later bubble in the run has an empty name node. Skipping this step
    does not lose a few names, it mis-attributes most of the conversation.
    """
    messages: list[Message] = []
    last_sender = ""
    for item in items:
        # Zalo separates the parts of a display name with U+00A0, which makes an
        # exported name compare unequal to the one a caller typed.
        item = dict(item, sender=" ".join((item.get("sender") or "").split()))
        if item.get("sender"):
            last_sender = item["sender"]
        elif item.get("outgoing"):
            last_sender = "Bạn"
        messages.append(Message(
            id=item["id"],
            sender=item.get("sender") or last_sender,
            text=item.get("text", ""),
            time=item.get("time", ""),
            outgoing=bool(item.get("outgoing")),
            qid=item.get("qid", ""),
            attachments=[Attachment(**a) for a in item.get("attachments", [])],
        ))
    return messages


def latest_from(messages: list[Message], sender: str,
                match: str | None = None) -> Message | None:
    """The newest message from `sender`, optionally one containing `match`.

    Ordering is by `timestamp_ms`, never by the rendered clock: Zalo only prints
    a send time on the last bubble of each run.
    """
    needle = fold(sender)
    wanted = fold(match) if match else ""
    hits = [m for m in messages
            if fold(m.sender) == needle and (not wanted or wanted in fold(m.text))]
    if not hits:
        return None
    return max(hits, key=lambda m: m.timestamp_ms or 0)


def attachments_after(messages: list[Message], anchor: Message, sender: str,
                      minutes: float = 90.0) -> list[Message]:
    """Messages from `sender` carrying attachments within `minutes` of `anchor`.

    A homework post is always plain text; the worksheets and demo videos arrive
    as separate bubbles moments later, so the attachment belongs to the window
    rather than to the message.
    """
    start = anchor.timestamp_ms
    if start is None:
        return []
    needle = fold(sender)
    window = minutes * 60_000
    return [m for m in messages
            if m.attachments and fold(m.sender) == needle
            and m.timestamp_ms is not None
            and 0 <= m.timestamp_ms - start <= window]


def resource_root(group_id: str) -> Path | None:
    """Zalo's on-disk attachment tree for one conversation, if it exists."""
    for resource in sorted(ZALO_MEDIA.glob("*/ZaloDownloads/resource")):
        candidate = resource / group_id
        if candidate.is_dir():
            return candidate
    return None


def cached_attachment(root: Path, key: str) -> Path | None:
    """The blob Zalo has already fetched for `key`, if any.

    Names are `<key>` for files and videos and `<key>_<hash>.jxl` for pictures;
    a trailing `_t` is the thumbnail, never the real thing.
    """
    for kind in RESOURCE_KINDS:
        for path in sorted((root / kind).glob(key + "*")):
            if path.name.endswith("_t") or not path.is_file():
                continue
            return path
    return None


# Zalo's lists are react-virtualized: the node you can query is not the node
# that scrolls. Walk up to the nearest genuinely scrollable ancestor.
JS_SCROLLER = """
window.__zScroller = (sel) => {
  const root = document.querySelector(sel);
  if (!root) return null;
  const scrolls = (n) => {
    const style = getComputedStyle(n).overflowY;
    return n.scrollHeight > n.clientHeight + 10 &&
           (style === 'auto' || style === 'scroll' || style === 'overlay');
  };
  // The chat pane scrolls in a descendant; the conversation list scrolls in an
  // ancestor. Prefer the tallest scrollable descendant, then walk up.
  let best = null;
  for (const n of root.querySelectorAll('*')) {
    if (n.clientHeight >= 200 && scrolls(n) &&
        (!best || n.scrollHeight > best.scrollHeight)) best = n;
  }
  if (best) return best;
  let n = root;
  while (n && n !== document.body) {
    if (scrolls(n)) return n;
    n = n.parentElement;
  }
  return root;
};
return null;
"""


@dataclass
class Zalo:
    cdp: CDP
    selectors: dict[str, str] = field(default_factory=lambda: dict(SEL))

    async def prepare(self) -> None:
        """Install the scroller helper in the page."""
        await self.cdp.evaluate(JS_SCROLLER)

    # ---------------------------------------------------------------- lists
    async def _rendered_rows(self) -> list[Conversation]:
        rows = await self.cdp.evaluate(f"""
          const inner = document.querySelector({self.selectors['list']!r});
          if (!inner) return [];
          return Array.from(inner.querySelectorAll({self.selectors['row']!r})).map(el => {{
            const q = s => el.querySelector(s);
            const name = q({self.selectors['row_name']!r});
            const prev = q({self.selectors['row_preview']!r});
            const time = q({self.selectors['row_time']!r});
            return {{
              id: el.getAttribute({self.selectors['row_id']!r}) || '',
              name: name ? name.innerText.trim() : '',
              preview: prev ? prev.innerText.trim() : '',
              time: time ? time.innerText.trim() : '',
            }};
          }}).filter(r => r.id);
        """)
        return [Conversation(**r) for r in rows]

    async def list_conversations(self, max_scrolls: int = 40,
                                 settle: float = 0.35) -> list[Conversation]:
        """Walk the virtualised list top to bottom and collect every row."""
        await self.prepare()
        await self.cdp.evaluate(
            f"window.__zScroller({self.selectors['scroller']!r}).scrollTop = 0; return null;")
        await asyncio.sleep(settle)
        seen: dict[str, Conversation] = {}
        for _ in range(max_scrolls):
            for row in await self._rendered_rows():
                seen.setdefault(row.id, row)
            at_end = await self.cdp.evaluate(f"""
              const s = window.__zScroller({self.selectors['scroller']!r});
              const before = s.scrollTop;
              s.scrollTop = before + s.clientHeight * 0.8;
              return s.scrollTop <= before + 1;
            """)
            await asyncio.sleep(settle)
            if at_end:
                for row in await self._rendered_rows():
                    seen.setdefault(row.id, row)
                break
        return list(seen.values())

    async def find_conversations(self, query: str, groups_only: bool = False,
                                 **kwargs: object) -> list[Conversation]:
        needle = fold(query)
        found = [c for c in await self.list_conversations(**kwargs)  # type: ignore[arg-type]
                 if needle in fold(c.name)]
        return [c for c in found if c.is_group] if groups_only else found

    # ------------------------------------------------------------- opening
    async def _click_row(self, thread_id: str) -> bool:
        row = self.selectors["row"]
        attr = self.selectors["row_id"]
        click = self.selectors["row_click"]
        return await self.cdp.evaluate(f"""
          const el = document.querySelector(
              {row!r} + '[' + {attr!r} + '="' + {thread_id!r} + '"]');
          if (!el) return false;
          el.scrollIntoView({{block: 'center'}});
          const t = el.querySelector({click!r}) || el;
          for (const type of ['mousedown', 'mouseup', 'click']) {{
            t.dispatchEvent(new MouseEvent(type, {{bubbles: true, cancelable: true,
                                                   view: window, button: 0}}));
          }}
          return true;
        """)

    async def open_conversation(self, thread_id: str, settle: float = 2.0,
                                max_scrolls: int = 60, step_wait: float = 0.25) -> str:
        """Bring the row into the virtualised window, click it, return the title.

        React-virtualized re-renders asynchronously, so the sweep has to yield
        between scroll steps instead of looping inside one JS tick.
        """
        await self.prepare()
        if not await self._click_row(thread_id):
            await self.cdp.evaluate(
                f"window.__zScroller({self.selectors['scroller']!r}).scrollTop = 0; return null;")
            await asyncio.sleep(step_wait)
            for _ in range(max_scrolls):
                if await self._click_row(thread_id):
                    break
                at_end = await self.cdp.evaluate(f"""
                  const s = window.__zScroller({self.selectors['scroller']!r});
                  const before = s.scrollTop;
                  s.scrollTop = before + s.clientHeight * 0.7;
                  return s.scrollTop <= before + 1;
                """)
                await asyncio.sleep(step_wait)
                if at_end:
                    if await self._click_row(thread_id):
                        break
                    raise CDPError(f"Conversation {thread_id} was not found in the list.")
            else:
                raise CDPError(f"Conversation {thread_id} was not found in the list.")
        await asyncio.sleep(settle)
        return await self.current_conversation()

    async def current_conversation(self) -> str:
        return await self.cdp.evaluate(f"""
          const h = document.querySelector({self.selectors['chat_header']!r});
          return h ? h.innerText.replace(/\\u00a0/g, ' ').split('\\n')[0].trim() : '';
        """)

    async def ensure_conversation(self, thread_id: str, name: str) -> str:
        """Confirm the chat pane still shows `name`, reopening it when it drifted.

        The debugging port drives the human's own Zalo, so the open conversation
        can change under the tool at any moment. Every pass has to re-check
        rather than assume, or it silently reads the wrong thread.
        """
        title = await self.current_conversation()
        if fold(title) == fold(name):
            return title
        return await self.open_conversation(thread_id)

    # --------------------------------------------------------- attachments
    async def request_download(self, key: str) -> str:
        """Ask Zalo to pull one attachment into its own cache.

        This clicks the client's own "Lưu về máy" control on the bubble that
        owns `key`. Nothing is sent, no URL is forged, and the file lands in
        Zalo's resource tree rather than in the user's Downloads folder.

        Returns the bubble's cloud state, `"not-in-dom"` when the message is not
        rendered, or `"expired"` when Zalo no longer offers the file.
        """
        stamp = key.split("_")[0]
        sel = self.selectors
        return await self.cdp.evaluate(f"""
          const stamp = {stamp!r};
          // The same message renders as bb_msg_id_<ms> or bb_msg_id_<ms>_<...>,
          // so look it up by prefix rather than by exact id.
          const el = document.getElementById('bb_msg_id_' + stamp) ||
                     document.querySelector('[id^="bb_msg_id_' + stamp + '"]');
          if (!el) return 'not-in-dom';
          const bubble = el.querySelector({sel['msg_file']!r});
          const state = el.querySelector({sel['msg_file_state']!r});
          const label = state ? state.innerText.trim() : '';
          const link = el.querySelector({sel['msg_file_download']!r});
          if (!link) return label || 'expired';
          // The actions row only renders while hovered.
          const box = el.querySelector({sel['msg_file_actions']!r});
          if (box) box.classList.remove('none');
          for (const type of ['mouseover', 'mouseenter', 'mousedown',
                              'mouseup', 'click']) {{
            link.dispatchEvent(new MouseEvent(type, {{bubbles: true,
                cancelable: true, view: window, button: 0}}));
          }}
          return label || 'requested';
        """)

    async def fetch_attachment(self, group_id: str, key: str,
                               timeout: float = 25.0,
                               poll: float = 1.0) -> tuple[Path | None, str]:
        """Return the cached blob for `key`, asking Zalo for it when absent.

        The second element says why there is no path, so a caller can tell an
        expired file apart from a bubble that simply was not rendered. The
        latter means the pane moved under us and is a fault to report, never a
        row to drop quietly.
        """
        root = resource_root(group_id)
        if root is None:
            raise CDPError(f"No Zalo resource tree for {group_id}. Is Zalo signed in?")
        existing = cached_attachment(root, key)
        if existing is not None:
            return existing, ""
        state = await self.request_download(key)
        if state == "not-in-dom":
            return None, "not-in-dom"
        if state == "expired" or "không tồn tại" in state:
            return None, "expired"
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(poll)
            waited += poll
            landed = cached_attachment(root, key)
            if landed is not None:
                return landed, ""
        return None, f"timeout ({state})"

    # ------------------------------------------------------------ messages
    async def _rendered_messages(self) -> list[Message]:
        sel = self.selectors
        raw = await self.cdp.evaluate(f"""
          return Array.from(document.querySelectorAll({sel['message']!r})).map(el => {{
            const q = s => el.querySelector(s);
            const text = q({sel['msg_text']!r});
            const sender = q({sel['msg_sender']!r});
            const time = q({sel['msg_time']!r});
            const side = q({sel['msg_side']!r});
            const holder = q({sel['msg_qid']!r});
            const cls = side ? side.className : '';
            const files = Array.from(el.querySelectorAll({sel['msg_file']!r})).map(f => {{
              const name = f.querySelector({sel['msg_file_name']!r});
              const size = f.querySelector({sel['msg_file_size']!r});
              const state = f.querySelector({sel['msg_file_state']!r});
              const dl = f.querySelector({sel['msg_file_download']!r});
              const owner = f.closest('[data-qid]');
              const qid = owner ? owner.getAttribute('data-qid') : '';
              return {{
                kind: 'file',
                key: qid.indexOf('@') >= 0 ? qid.split('@')[1] : '',
                name: name ? (name.getAttribute('title') || name.innerText.trim()) : '',
                size: size ? size.innerText.trim() : '',
                state: state ? state.innerText.trim() : '',
                downloadable: !!dl,
              }};
            }});
            const media = Array.from(el.querySelectorAll({sel['msg_photo']!r})).map(p => {{
              const qid = p.getAttribute('data-qid') || '';
              return {{
                kind: 'media',
                key: qid.indexOf('@') >= 0 ? qid.split('@')[1] : '',
                name: '', size: '', state: '', downloadable: false,
              }};
            }});
            return {{
              id: el.id,
              qid: holder ? (holder.getAttribute('data-qid') || '') : '',
              text: text ? text.innerText.trim() : '',
              sender: sender ? sender.innerText.trim() : '',
              time: time ? time.innerText.trim() : '',
              outgoing: /floating-menu-wrapper--(me|self)\\b/.test(cls),
              attachments: files.concat(media).filter(a => a.key),
            }};
          }});
        """)
        return attribute_senders(raw)

    async def read_messages(self, limit: int = 200, max_scrolls: int = 30,
                            settle: float = 0.6, deep: bool = False,
                            until_ts: int | None = None,
                            max_age_days: float | None = None,
                            idle_rounds: int = 3,
                            pin_wait: float = 2.5) -> list[Message]:
        """Collect messages, scrolling back until `limit` or the top is hit.

        Zalo only asks the server for older history while the chat pane sits
        pinned at `scrollTop === 0`; simply reaching the top and stopping yields
        whatever was already rendered and nothing more. `deep=True` pins there
        and waits, repeating until `idle_rounds` consecutive rounds add nothing.

        `until_ts` (epoch ms) and `max_age_days` set how far back is far enough,
        so a caller that wants "this week" does not pay for the whole history.
        """
        await self.prepare()
        floor_ts = until_ts
        if max_age_days is not None:
            cutoff = int((time.time() - max_age_days * 86_400) * 1000)
            floor_ts = cutoff if floor_ts is None else max(floor_ts, cutoff)

        collected: dict[str, Message] = {}

        def oldest_ts() -> int | None:
            stamps = [m.timestamp_ms for m in collected.values()
                      if m.timestamp_ms is not None]
            return min(stamps) if stamps else None

        def deep_enough() -> bool:
            if floor_ts is not None:
                back = oldest_ts()
                return back is not None and back <= floor_ts
            return len(collected) >= limit

        idle = 0
        for _ in range(max_scrolls):
            for message in await self._rendered_messages():
                collected.setdefault(message.id, message)
            if deep_enough():
                break
            at_top = await self.cdp.evaluate(f"""
              const s = window.__zScroller({self.selectors['chat_scroller']!r});
              if (!s) return true;
              const before = s.scrollTop;
              s.scrollTop = Math.max(0, before - s.clientHeight * 0.9);
              return before <= 1;
            """)
            await asyncio.sleep(settle)
            if not at_top:
                continue
            if not deep:
                for message in await self._rendered_messages():
                    collected.setdefault(message.id, message)
                break
            # Pinned at the top is the only place Zalo fetches older history.
            before = len(collected)
            await self.cdp.evaluate(
                f"window.__zScroller({self.selectors['chat_scroller']!r})"
                ".scrollTop = 0; return null;")
            await asyncio.sleep(pin_wait)
            for message in await self._rendered_messages():
                collected.setdefault(message.id, message)
            idle = idle + 1 if len(collected) == before else 0
            if idle >= idle_rounds:
                break

        ordered = sorted(collected.values(), key=lambda m: m.timestamp_ms or 0)
        if floor_ts is not None:
            return [m for m in ordered if (m.timestamp_ms or 0) >= floor_ts]
        return ordered[-limit:]

    async def search_messages(self, keyword: str, **kwargs: object) -> list[Message]:
        needle = fold(keyword)
        messages = await self.read_messages(**kwargs)  # type: ignore[arg-type]
        return [m for m in messages if needle in fold(m.text)]


async def open_zalo(port: int = DEFAULT_PORT) -> tuple[CDP, Zalo]:
    cdp = await connect(port=port, title="Zalo")
    await cdp.__aenter__()
    return cdp, Zalo(cdp)
