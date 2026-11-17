"""
core/ask_first.py - Vision's ask-first rule.

Vision must ask before it deletes files, sends a message or email, or installs
software. Deleting a calendar event, inviting people to one and replying in a
chat on screen count too. This module decides which tool calls fall under that rule and parks
them behind the on-screen CONFIRM / CANCEL banner from core/confirm.py.

The decision is made here, in code, from the tool name and its arguments. The
model cannot skip it by phrasing the request differently or by passing a
"confirmed" flag, because the only way past the banner is the user's click.

Every place that runs a tool goes through `guard()`: the live conversation in
main.py and the plain runner that plugins use. A confirmed action runs on a
worker thread, and its result is reported back through the notifier that
main.py binds, so the assistant can tell the user how it went.
"""

from __future__ import annotations

from typing import Callable, Optional

from core import confirm as confirm_gate
from core import outcome

# Plugin tools are downloaded from the community, so their names are not known
# in advance. A plugin whose name says it sends, mails or installs is gated as
# a whole: asking once too often is cheaper than one unwanted email.
_RISKY_PLUGIN_WORDS = ("send", "mail", "message", "install", "delete")

_notify: Optional[Callable[[str], None]] = None


def bind(notify: Callable[[str], None]) -> None:
    """`notify(text)` reports a confirmed action's result to the assistant."""
    global _notify
    _notify = notify


def tell(text: str) -> None:
    """Pass a result that finished later (a confirmed action, a sign-in in the
    browser) back to the assistant, so it can tell the user."""
    if _notify:
        _notify(text)


def _short(text, limit: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _for_model(result: str, limit: int) -> str:
    """Shorten a result for the model without cutting off the end marker of
    outside data (core/outcome.untrusted), which would leave it unclosed."""
    if "<<<UNTRUSTED" in str(result):
        return outcome.untrusted("tool-result", _short(outcome.unwrap(result), limit))
    return _short(result, limit)


def describe(name: str, args: dict, is_plugin: bool = False) -> Optional[tuple[str, str]]:
    """(title, detail) when this call needs the user's OK, else None."""
    args = args or {}

    if name == "file_controller":
        if str(args.get("action", "")).lower().strip() == "delete":
            target = args.get("name") or args.get("path") or "a file"
            where = args.get("path", "")
            detail = f"Move '{target}' to the Trash"
            if args.get("name") and where:
                detail += f" (in {where})"
            return f"Delete {_short(target, 60)}", detail + "."
        return None

    if name == "send_message":
        platform = args.get("platform") or "WhatsApp"
        receiver = args.get("receiver") or "someone"
        text = args.get("message_text") or ""
        return (f"Send a {platform} message to {_short(receiver, 40)}",
                f'"{_short(text)}"')

    if name == "dev_agent":
        if str(args.get("mode") or "").strip().lower() == "status":
            return None
        what = args.get("description") or args.get("task") or args.get("request") or ""
        return ("Start a coding job",
                "It writes and runs code and may install Python packages. "
                + _short(what, 120))

    if name == "google_calendar":
        # Deleting an event, and inviting people (Google emails them). The
        # calendar module resolves which event, so the banner names it.
        from actions import google_calendar
        return google_calendar.confirmation(args)

    if name == "cli_helper":
        # Letting Claude Code or Gemini CLI create and edit files in a folder.
        from actions import cli_helper
        return cli_helper.confirmation(args)

    if name == "chat_reply":
        # Typing a reply into the chat open on screen and pressing Enter. The
        # module fixes the exact words here, so the banner shows what is sent.
        from actions import chat_reply
        return chat_reply.confirmation(args)

    if is_plugin:
        low = name.lower()
        if any(word in low for word in _RISKY_PLUGIN_WORDS):
            return (f"Run {name}", _short(", ".join(f"{k}={v}" for k, v in args.items())))

    return None


def guard(name: str, args: dict, run: Callable[[], str], is_plugin: bool = False) -> str:
    """Run the tool now, or park it behind the confirm banner.

    Returns the tool's own result, or the sentence telling the assistant to
    ask the user to confirm on screen."""
    asked = describe(name, args, is_plugin=is_plugin)
    if asked is None:
        return run()

    title, detail = asked

    def _confirmed() -> str:
        try:
            result = run() or "Done."
        except Exception as e:  # reported, never raised into the UI thread
            result = f"failed: {e}"
        if _notify:
            try:
                _notify(f"[SYSTEM] The user confirmed '{title}' on screen and it "
                        f"has now run. Result: {_for_model(result, 600)}. Tell the "
                        f"user briefly how it went.")
            except Exception:
                pass
        return _short(outcome.unwrap(result), 200)    # shown to the user

    return confirm_gate.request(f"ask_first:{name}", title, detail, _confirmed)
