"""
Tests for the built-in terminals.
Run with:  python -m actions._terminal_tests

A real shell is started on a real pseudo-terminal, and the widget is drawn on
Qt's offscreen platform, so no desktop is needed. What these cannot show is the
look on a real screen, typing through a real keyboard and input method, or
Claude Code and Gemini CLI signed in. Those are checked on the target machine.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from actions import terminal as term
from core import outcome
from core.terminal_session import (
    APP_CURSOR_KEYS, BRACKETED_PASTE, PtySession, TerminalScreen,
    resolve_folder, shell_command, shell_environment,
)

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def lines(screen) -> list[str]:
    return [l.rstrip() for l in screen.display]


# -- the screen -----------------------------------------------------------------

def test_screen() -> None:
    replies: list[str] = []
    s = TerminalScreen(40, 6, reply=replies.append)
    s.feed(b"hello \x1b[38:2::255:0:0mred\x1b[0m \x1b[>4;2mX\x1b[4:3mY\x1b[58;2;1;2;3mZ")
    check("text survives odd sequences", lines(s)[0] == "hello red XYZ", lines(s)[0])
    check("colon colour is read", s.buffer[0][6].fg == "ff0000", s.buffer[0][6].fg)
    check("modifyOtherKeys is not underline", not s.buffer[0][10].underscore)
    check("curly underline is underline", s.buffer[0][11].underscore)
    check("underline colour is not a style", not s.buffer[0][12].bold and not s.buffer[0][12].blink)

    s.feed(b"\x1b]0;my title\x07\x1b[6n\x1b[c\x1b]11;?\x1b\\\x1b[?u\x1b[>1u\x1bP+q544e\x1b\\")
    check("title", s.window_title == "my title", s.window_title)
    check("cursor position answered", "\x1b[1;14R" in replies, repr(replies))
    check("device attributes answered", "\x1b[?6c" in replies)
    check("background colour answered", any(r.startswith("\x1b]11;rgb:") for r in replies))
    check("queries leave no text", lines(s)[0] == "hello red XYZ", lines(s)[0])

    # every sequence split at every byte
    s = TerminalScreen(40, 6)
    for b in b"a\x1b[38;2;10;20;30mb\x1b]2;ti\x07c\xce\xb1\x1b[0m":
        s.feed(bytes([b]))
    check("split sequences", lines(s)[0] == "abcα" and s.window_title == "ti", lines(s)[0])
    check("split colour", s.buffer[0][1].fg == "0a141e")

    for i in range(20):
        s.feed(f"\r\nline{i}".encode())
    check("scrollback fills", len(s.history) == 15 and s.row_text(0).rstrip() == "abcα",
          str(len(s.history)))
    check("line ids", s.base == 0 and s.pushed == 15)
    s.feed(b"\x1b[?1049h\x1b[2Jalt!")
    check("alternate screen", s.alt and lines(s)[0] == "alt!")
    s.feed(b"\r\n" * 10)
    check("alternate screen keeps scrollback clean", len(s.history) == 15)
    s.feed(b"\x1b[?1049l")
    check("main screen restored", not s.alt and lines(s)[-1] == "line19", lines(s)[-1])
    s.resize(3, 40)
    check("shrink keeps the cursor line", lines(s) == ["line17", "line18", "line19"], str(lines(s)))
    check("shrink moves lines to scrollback", len(s.history) == 18)
    s.feed(b"\x1b[3J")
    check("clear scrollback", len(s.history) == 0 and s.base == s.pushed)

    s = TerminalScreen(20, 6)
    s.feed(b"top\r\nsecond")
    s.resize(3, 20)
    check("shrink of a short screen loses nothing", lines(s)[:2] == ["top", "second"], str(lines(s)))

    s.feed(b"\x1b[?2004h\x1b[?1h")
    check("modes", BRACKETED_PASTE in s.mode and APP_CURSOR_KEYS in s.mode)
    s.feed(b"\x1b[999;999;999;;;;x\x1b[\xff\xfe ok")
    check("garbage does not kill the screen", "ok" in lines(s)[-1] or "ok" in " ".join(lines(s)))


# -- starting the shell -----------------------------------------------------------

def test_environment() -> None:
    env = shell_environment({"PATH": "/v/.venv/bin:/usr/bin:/bin", "VIRTUAL_ENV": "/v/.venv",
                             "QT_SCALE_FACTOR": "1.5", "HOME": "/home/x", "TERM": "dumb"})
    check("venv off PATH", env["PATH"] == "/usr/bin:/bin", env["PATH"])
    check("venv vars gone", "VIRTUAL_ENV" not in env and "QT_SCALE_FACTOR" not in env)
    check("TERM set", env["TERM"] == "xterm-256color" and env["COLORTERM"] == "truecolor")
    check("rest kept", env["HOME"] == "/home/x")

    check("plain shell", shell_command("shell", "/bin/bash") == ["/bin/bash", "-i"])
    c = shell_command("claude", "/bin/bash")
    check("claude runs in the user's interactive shell",
          c[:3] == ["/bin/bash", "-i", "-c"] and c[3] == "claude; exec /bin/bash -i", str(c))
    check("gemini", shell_command("gemini", "/bin/zsh")[3].startswith("gemini; exec"))

    check("folder default", resolve_folder(None) == resolve_folder("") == resolve_folder("/no/such/dir"))
    check("folder given", str(resolve_folder("/tmp")) == os.path.realpath("/tmp"))


def test_pty() -> None:
    p = PtySession(shell_command("shell", "/bin/bash"), rows=10, cols=60)
    s = TerminalScreen(60, 10, reply=lambda d: p.write(d.encode()))

    def pump(until, seconds=5.0) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            d = p.read()
            if d is None:
                return until()
            if d:
                s.feed(d)
            if until():
                return True
            time.sleep(0.01)
        return until()

    text = lambda: "\n".join(lines(s))
    p.write(b"PS1='$ '; tty; stty size; echo T=$TERM; trap -p PIPE; echo end1\r")
    check("shell answers", pump(lambda: "\nend1" in text()), text())
    check("a real terminal", "/dev/pts/" in text(), text())
    check("size reaches the shell", "\n10 60" in text(), text())
    check("TERM", "T=xterm-256color" in text())
    check("SIGPIPE not ignored", "trap -- '' SIGPIPE" not in text(), text())

    p.resize(12, 70)
    s.resize(12, 70)
    p.write(b"stty size; echo end2\r")
    check("resize reaches the shell", pump(lambda: "\nend2" in text()) and "\n12 70" in text(), text())

    p.write(b"sleep 100\r")
    pump(lambda: False, 0.4)
    p.write(b"\x03")
    p.write(b"echo rc=$?\r")
    check("Ctrl+C stops the program", pump(lambda: "rc=130" in text()), text())

    big = b"x" * 20000
    p.write(b"cat >/dev/null <<'E'\r" + big + b"\rE\recho end3\r")
    ok = False
    end = time.time() + 8
    while time.time() < end and not ok:
        p.flush()
        ok = pump(lambda: "\nend3" in text(), 0.05)
    check("a large paste gets through", ok)

    p.write(b"sleep 300 &\recho pid=$!\r")
    check("background job", pump(lambda: "pid=" in text().split("echo pid=$!")[-1]), text())
    pid = int(text().split("pid=")[-1].split()[0])
    shell_pid = p.proc.pid
    p.close()
    check("closed", not p.alive and p.read() is None)
    gone = False
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    check("closing hangs up what was running", gone)
    check("shell reaped", p.proc.wait(timeout=5) is not None, str(shell_pid))
    p.close()   # twice is fine


# -- the voice tool ----------------------------------------------------------------

class Player:
    def __init__(self, count=0, responds=True):
        self.count, self.shown, self.responds, self.calls = count, False, responds, []

    def open_terminal(self, program, folder=""):
        self.calls.append(("open", program, folder))
        if self.responds:
            self.count += 1
            self.shown = True

    def show_terminals(self, show=True):
        self.calls.append(("show", show))
        if self.responds:
            self.shown = show

    def terminal_count(self):
        return self.count

    def terminals_shown(self):
        return self.shown


def test_tool() -> None:
    pl = Player()
    r = term.terminal({"program": "Claude Code"}, player=pl)
    check("open claude", r.startswith("status=done") and pl.calls[-1][:2] == ("open", "claude"), r)
    check("says who types", "cannot type" in r, r)
    r = term.terminal({"program": "gemini-cli", "folder": "/tmp"}, player=pl)
    check("open gemini in folder", pl.calls[-1] == ("open", "gemini", os.path.realpath("/tmp")), str(pl.calls[-1]))
    n = len(pl.calls)
    r = term.terminal({"program": "shell"}, player=pl)
    check("third is refused", outcome.is_problem(r) and "both" in r, r)
    check("third opened nothing", not any(c[0] == "open" for c in pl.calls[n:]))

    pl = Player()
    r = term.terminal({}, player=pl)
    check("default is a shell", pl.calls[-1][1] == "shell", r)
    r = term.terminal({"program": "shell", "folder": "/no/such/dir"}, player=pl)
    check("missing folder is reported", "folder_not_found" in r, r)
    r = term.terminal({"program": "rm -rf"}, player=pl)
    check("unknown program refused", outcome.is_problem(r), r)
    r = term.terminal({"action": "close"}, player=pl)
    check("no close by voice", outcome.is_problem(r), r)
    r = term.terminal({"action": "run", "command": "ls"}, player=pl)
    check("no command by voice", outcome.is_problem(r), r)
    r = term.terminal({"action": "hide"}, player=pl)
    check("hide", r.startswith("status=done") and pl.shown is False, r)
    r = term.terminal({"action": "show"}, player=pl)
    check("show", r.startswith("status=done") and pl.shown is True, r)

    slow = Player(responds=False)
    old, term._wait = term._wait, lambda c, seconds=2.0: bool(c())
    try:
        r = term.terminal({"program": "claude"}, player=slow)
    finally:
        term._wait = old
    check("no confirmation is not 'done'", r.startswith("status=unverified"), r)
    check("no window", outcome.is_problem(term.terminal({}, player=None)))
    check("tool declared", term.TOOL["name"] == "terminal" and term.TOOL["handler"] is term.terminal)
    props = term.TOOL["parameters"]["properties"]
    check("tool takes no command", set(props) == {"action", "program", "folder"}, str(set(props)))


# -- the widget ---------------------------------------------------------------------

def test_widget() -> None:
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        from gui.terminal import MAX_TERMINALS, TerminalDock, encode_key
    except Exception as e:      # no Qt on this machine
        print(f"  SKIP  widget tests ({type(e).__name__}: {e})")
        return
    K = Qt.Key
    check("enter", encode_key(K.Key_Return, "\r", False, False, False) == b"\r")
    check("shift+enter is a new line", encode_key(K.Key_Return, "\r", True, False, False) == b"\x1b\r")
    check("escape", encode_key(K.Key_Escape, "\x1b", False, False, False) == b"\x1b")
    check("ctrl+c", encode_key(K.Key_C, "\x03", False, True, False) == b"\x03")
    check("ctrl+c on a Greek layout", encode_key(K.Key_C, "", False, True, False) == b"\x03")
    check("up", encode_key(K.Key_Up, "", False, False, False) == b"\x1b[A")
    check("up, application mode", encode_key(K.Key_Up, "", False, False, False, True) == b"\x1bOA")
    check("ctrl+right", encode_key(K.Key_Right, "", False, True, False) == b"\x1b[1;5C")
    check("shift+tab", encode_key(K.Key_Backtab, "", True, False, False) == b"\x1b[Z")
    check("backspace", encode_key(K.Key_Backspace, "\x08", False, False, False) == b"\x7f")
    check("delete", encode_key(K.Key_Delete, "", False, False, False) == b"\x1b[3~")
    check("greek", encode_key(K.Key_A, "α", False, False, False) == "α".encode())
    check("alt+b", encode_key(K.Key_B, "b", False, False, True) == b"\x1bb")
    check("shift alone", encode_key(K.Key_Shift, "", True, False, False) == b"")

    app = QApplication.instance() or QApplication(sys.argv[:1])

    def spin(until, seconds=5.0) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            if until():
                return True
            time.sleep(0.01)
        return until()

    dock = TerminalDock()
    dock.resize(1200, 500)
    dock.show()
    counts: list[int] = []
    dock.count_changed.connect(counts.append)
    check("opens", dock.open("shell") and dock.open("shell"))
    check("two at most", MAX_TERMINALS == 2 and not dock.open("shell") and dock.count() == 2)
    a, b = (p.view for p in dock._panes)
    check("both start", spin(lambda: a.running and b.running))
    check("sized to the widget, half each",
          spin(lambda: 40 < a.screen.columns < 100 and abs(a.screen.columns - b.screen.columns) <= 1),
          f"{a.screen.columns} {b.screen.columns}")
    text = lambda v: "\n".join(l.rstrip() for l in v.screen.display)

    a.setFocus()
    QTest.keyClicks(a, "echo one-$((40+2))")
    QTest.keyClick(a, K.Key_Return)
    QTest.keyClicks(b, "echo two-$((50+5))")
    QTest.keyClick(b, K.Key_Return)
    check("typing reaches the first", spin(lambda: "one-42" in text(a)), text(a))
    check("typing reaches the second", spin(lambda: "two-55" in text(b)), text(b))
    check("they are separate", "two-55" not in text(a) and "one-42" not in text(b))

    b.paste("echo pasted-αβ")
    QTest.keyClick(b, K.Key_Return)
    check("paste, Greek", spin(lambda: "\npasted-αβ" in text(b)), text(b))
    sent: list[bytes] = []
    real_send, b._send = b._send, lambda data, follow=True: sent.append(data)
    b.screen.mode.add(BRACKETED_PASTE)
    b.paste("a\nb\x1b[201~c")
    b._send = real_send
    check("paste is bracketed and cannot end its own bracket",
          sent == [b"\x1b[200~a\rb[201~c\x1b[201~"], repr(sent))

    QTest.keyClicks(a, "seq 1 200")
    QTest.keyClick(a, K.Key_Return)
    check("output scrolls into history", spin(lambda: len(a.screen.history) > 100 and "\n200" in text(a)))
    a._scroll_by(50)
    check("scrolled back", a._scroll == 50)
    top = a.screen.base + a._top_index()
    a._sel = ((top, 0), (top + 1, 2))
    got = a.selected_text().split("\n")
    check("selection reads the scrolled lines", len(got) == 2 and got[0].isdigit()
          and int(got[1]) == int(got[0]) + 1, str(got))
    check("draws", not dock.grab().isNull())
    QTest.keyClick(a, K.Key_Return)
    check("a key returns to the bottom", a._scroll == 0)

    QTest.keyClicks(b, "exit")
    QTest.keyClick(b, K.Key_Return)
    check("exit closes its pane", spin(lambda: dock.count() == 1))
    check("a place is free again", dock.open("shell") and dock.count() == 2)
    views = [p.view for p in dock._panes]
    spin(lambda: all(v.running for v in views))
    dock.close_all()
    check("close_all stops them", not any(v.running for v in views))
    spin(lambda: False, 0.2)
    check("count was reported", counts[:3] == [1, 2, 1], str(counts))


def main() -> int:
    for t in (test_screen, test_environment, test_pty, test_tool, test_widget):
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} crashed", False, f"{type(e).__name__}: {e}")
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
