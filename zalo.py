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
import unicodedata
from dataclasses import dataclass, field

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
    "msg_text": "span.text",
    "msg_sender": ".message-sender-name-content",
    "msg_time": ".card-send-time__sendTime",
    "msg_side": '[class*="floating-menu-wrapper--"]',
    "onboard": "#chatOnboard",
}


def fold(text: str) -> str:
    """Casefold and strip Vietnamese diacritics for forgiving matching."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "D").casefold().strip()


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
class Message:
    id: str
    sender: str
    text: str
    time: str
    outgoing: bool

    @property
    def timestamp_ms(self) -> int | None:
        raw = self.id.removeprefix("bb_msg_id_")
        return int(raw) if raw.isdigit() else None


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

    # ------------------------------------------------------------ messages
    async def _rendered_messages(self) -> list[Message]:
        raw = await self.cdp.evaluate(f"""
          return Array.from(document.querySelectorAll({self.selectors['message']!r})).map(el => {{
            const q = s => el.querySelector(s);
            const text = q({self.selectors['msg_text']!r});
            const sender = q({self.selectors['msg_sender']!r});
            const time = q({self.selectors['msg_time']!r});
            const side = q({self.selectors['msg_side']!r});
            const cls = side ? side.className : '';
            return {{
              id: el.id,
              text: text ? text.innerText.trim() : '',
              sender: sender ? sender.innerText.trim() : '',
              time: time ? time.innerText.trim() : '',
              outgoing: /floating-menu-wrapper--(me|self)\\b/.test(cls),
            }};
          }});
        """)
        messages, last_sender = [], ""
        for item in raw:
            # Zalo prints the name once per run of messages from one person.
            if item["sender"]:
                last_sender = item["sender"]
            elif item["outgoing"]:
                last_sender = "Bạn"
            messages.append(Message(id=item["id"], sender=item["sender"] or last_sender,
                                    text=item["text"], time=item["time"],
                                    outgoing=item["outgoing"]))
        return messages

    async def read_messages(self, limit: int = 200, max_scrolls: int = 30,
                            settle: float = 0.6) -> list[Message]:
        """Collect messages, scrolling back until `limit` or the top is hit."""
        await self.prepare()
        collected: dict[str, Message] = {}
        for _ in range(max_scrolls):
            for message in await self._rendered_messages():
                collected.setdefault(message.id, message)
            if len(collected) >= limit:
                break
            at_top = await self.cdp.evaluate(f"""
              const s = window.__zScroller({self.selectors['chat_scroller']!r});
              if (!s) return true;
              const before = s.scrollTop;
              s.scrollTop = Math.max(0, before - s.clientHeight * 0.9);
              return before <= 1;
            """)
            await asyncio.sleep(settle)
            if at_top:
                for message in await self._rendered_messages():
                    collected.setdefault(message.id, message)
                break
        ordered = sorted(collected.values(), key=lambda m: m.timestamp_ms or 0)
        return ordered[-limit:]

    async def search_messages(self, keyword: str, **kwargs: object) -> list[Message]:
        needle = fold(keyword)
        messages = await self.read_messages(**kwargs)  # type: ignore[arg-type]
        return [m for m in messages if needle in fold(m.text)]


async def open_zalo(port: int = DEFAULT_PORT) -> tuple[CDP, Zalo]:
    cdp = await connect(port=port, title="Zalo")
    await cdp.__aenter__()
    return cdp, Zalo(cdp)
