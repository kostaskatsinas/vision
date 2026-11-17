"""
core/terminal_session.py - a shell on a pseudo-terminal, and the screen it draws.

The built-in terminals (gui/terminal.py) are made of two parts that have no Qt
in them, so they live here and can be tested without a window:

  PtySession      starts the user's own shell on a pseudo-terminal, the same
                  way a terminal app does, and reads and writes it.
  TerminalScreen  turns what the shell prints into a grid of coloured cells,
                  with scrollback. The escape-code work is done by pyte.

These terminals are for the user to type into. They are not a tool the
assistant can use: nothing here lets Vision send a command to a shell. The
hidden helpers in actions/cli_helper.py are a separate thing and are unchanged.

Tests: python -m actions._terminal_tests
"""
from __future__ import annotations

import codecs
import fcntl
import os
import pty
import pwd
import re
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Optional

import pyte
from pyte.screens import Char, StaticDefaultDict

SCROLLBACK_LINES = 5000

# What a terminal can be opened with. "command" is typed into the user's own
# interactive shell, so their PATH, aliases and sign-ins apply; when it exits,
# the shell stays, as it would in any terminal.
PROGRAMS = {
    "claude": {"name": "Claude Code", "command": "claude"},
    "gemini": {"name": "Gemini CLI", "command": "gemini"},
    "shell": {"name": "Shell", "command": ""},
}


# -- starting the shell --------------------------------------------------------

def user_shell() -> str:
    """The user's login shell, as a terminal app would pick it."""
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    shell = shell or os.environ.get("SHELL") or "/bin/bash"
    return shell if os.access(shell, os.X_OK) else "/bin/sh"


def shell_environment(base: Optional[dict] = None) -> dict:
    """The environment for the shell: the user's own, without the pieces that
    only belong to Vision's Python process."""
    env = dict(os.environ if base is None else base)
    venv = env.pop("VIRTUAL_ENV", "")
    if venv:
        # vision.sh activates .venv; a terminal should see the normal python.
        venv_bin = os.path.join(venv, "bin")
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if p and p != venv_bin)
    for key in ("VIRTUAL_ENV_PROMPT", "QT_SCALE_FACTOR", "QT_LOGGING_RULES",
                "PYTHONHOME", "LINES", "COLUMNS"):
        env.pop(key, None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return env


def shell_command(program: str, shell: Optional[str] = None) -> list[str]:
    """The command line for a terminal that starts with `program`."""
    shell = shell or user_shell()
    command = PROGRAMS[program]["command"]
    if not command:
        return [shell, "-i"]
    # An interactive shell so ~/.bashrc (nvm, aliases, PATH) is read, then the
    # program, then a plain shell so the terminal stays usable after it exits.
    return [shell, "-i", "-c", f"{command}; exec {shlex.quote(shell)} -i"]


def resolve_folder(folder: Optional[str]) -> Path:
    """Where a terminal starts. Anything that is not an existing folder falls
    back to the home folder."""
    if folder:
        try:
            p = Path(str(folder)).expanduser()
            if not p.is_absolute():
                p = Path.home() / p
            if p.is_dir():
                return p.resolve()
        except (OSError, RuntimeError, ValueError):
            pass
    return Path.home()


# Runs in the child between fork and the shell: make the pseudo-terminal the
# controlling terminal (job control and Ctrl+C need it) and put back the
# signals Python ignores, which would otherwise be inherited by every program
# started in the terminal.
_TRAMPOLINE = (
    "import fcntl, os, signal, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)


class PtySession:
    """One shell on one pseudo-terminal. Not thread-safe: the GUI thread owns it."""

    def __init__(self, argv: list[str], cwd: Optional[Path] = None,
                 env: Optional[dict] = None, rows: int = 24, cols: int = 80):
        self._out = bytearray()
        self.fd, slave = pty.openpty()
        try:
            self._set_size(slave, rows, cols)
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _TRAMPOLINE, *argv],
                stdin=slave, stdout=slave, stderr=slave,
                cwd=str(cwd or Path.home()),
                env=shell_environment() if env is None else env,
                start_new_session=True, close_fds=True)
        except Exception:
            os.close(self.fd)
            self.fd = -1
            raise
        finally:
            os.close(slave)
        os.set_blocking(self.fd, False)

    @staticmethod
    def _set_size(fd: int, rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))

    @property
    def alive(self) -> bool:
        return self.fd >= 0

    def read(self, limit: int = 65536) -> Optional[bytes]:
        """What is waiting to be read, up to `limit` bytes. b"" when there is
        nothing yet, None once the shell has gone."""
        if self.fd < 0:
            return None
        try:
            data = os.read(self.fd, limit)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:                     # EIO: the last program closed the terminal
            return None
        return data or None

    def write(self, data: bytes) -> None:
        if self.fd < 0 or not data:
            return
        self._out += data
        self.flush()

    def flush(self) -> bool:
        """Push pending input to the shell. True when nothing is left; a large
        paste can take several calls."""
        while self._out and self.fd >= 0:
            try:
                n = os.write(self.fd, bytes(self._out[:4096]))
            except BlockingIOError:
                return False
            except InterruptedError:
                continue
            except OSError:
                self._out.clear()
                break
            del self._out[:n]
        return True

    def resize(self, rows: int, cols: int) -> None:
        if self.fd >= 0:
            try:
                self._set_size(self.fd, rows, cols)
            except OSError:
                pass

    def close(self) -> None:
        """Hang up, as closing a terminal window does. Never blocks."""
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        self._out.clear()
        proc = self.proc
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass

        def _reap():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

        threading.Thread(target=_reap, daemon=True, name="terminal-reap").start()


# -- the screen ----------------------------------------------------------------

ESC = "\x1b"
_CSI = re.compile(r"\x1b\[([<=>?!]?)([0-9;:]*)([ -/]*)([@-~])")
_CSI_PARTIAL = re.compile(r"\x1b\[[<=>?!]?[0-9;:]*[ -/]*\Z")
_STRING = re.compile(r"\x1b([\]P_^X])([^\x07\x1b]*)(?:\x07|\x1b\\)")
_STRING_HOLD = 8192            # an unfinished title/string longer than this is dropped
_ALT_SCREEN = (47, 1047, 1049)
BRACKETED_PASTE = 2004 << 5    # pyte keeps private modes shifted by five bits
APP_CURSOR_KEYS = 1 << 5


def _sgr(params: str) -> str:
    """Rewrite one colour/style sequence into the plain form pyte reads.

    Newer programs write `38:2::r:g:b` and `4:3` (curly underline) and set an
    underline colour with 58; pyte would print the leftovers as text or read
    the colour's numbers as styles."""
    out: list[str] = []
    groups = params.split(";")
    i = 0
    while i < len(groups):
        g = groups[i]
        i += 1
        if ":" in g:
            sub = g.split(":")
            head = sub[0]
            if head in ("38", "48"):
                nums = [s for s in sub[1:] if s != ""]
                if nums[:1] == ["2"] and len(nums) >= 4:
                    out += [head, "2", *nums[-3:]]
                elif nums[:1] == ["5"] and len(nums) >= 2:
                    out += [head, "5", nums[1]]
            elif head == "4":
                out.append("24" if sub[1:2] == ["0"] else "4")
            # 58:... (underline colour) and anything else: dropped
            continue
        if g == "58":
            kind = groups[i] if i < len(groups) else ""
            i += {"2": 4, "5": 2}.get(kind, 0)
            continue
        if g == "59":
            continue
        out.append(g)
    if not out and params:
        return ""
    return f"{ESC}[{';'.join(out)}m"


class TerminalScreen(pyte.Screen):
    """A pyte screen with scrollback, the alternate screen (used by full-screen
    programs such as vim and less) and a filter for the sequences pyte does
    not understand."""

    def __init__(self, columns: int = 80, lines: int = 24,
                 reply: Optional[Callable[[str], None]] = None):
        self.history: deque = deque(maxlen=SCROLLBACK_LINES)
        self.pushed = 0                 # lines ever moved into the scrollback
        self.alt = False
        self.window_title = ""
        self.background = "000000"      # reported to programs that ask (OSC 11)
        self.foreground = "ffffff"
        self._main = None
        self._reply = reply
        self._hold = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        super().__init__(columns, lines)
        self._stream = pyte.Stream(self)

    # -- input from the shell ------------------------------------------------

    def feed(self, data: bytes) -> None:
        text = self._clean(self._decoder.decode(data))
        if not text:
            return
        try:
            self._stream.feed(text)
        except Exception:
            # A sequence pyte tripped over: drop it and start the parser
            # again. Losing one sequence is better than a dead terminal.
            self._stream = pyte.Stream(self)

    def _clean(self, text: str) -> str:
        if self._hold:
            text, self._hold = self._hold + text, ""
        if ESC not in text:
            return text
        out: list[str] = []
        pos = 0
        while True:
            i = text.find(ESC, pos)
            if i < 0:
                out.append(text[pos:])
                break
            out.append(text[pos:i])
            nxt = text[i + 1:i + 2]
            if nxt == "":
                self._hold = text[i:]
                break
            if nxt == "[":
                m = _CSI.match(text, i)
                if m is None:
                    if _CSI_PARTIAL.match(text, i):
                        self._hold = text[i:]
                        break
                    out.append(ESC)
                    pos = i + 1
                    continue
                out.append(self._csi(m))
                pos = m.end()
                continue
            if nxt in "]P_^X":
                m = _STRING.match(text, i)
                if m is None:
                    if len(text) - i < _STRING_HOLD:
                        self._hold = text[i:]
                    break
                if nxt == "]":
                    self._osc(m.group(2))
                pos = m.end()
                continue
            out.append(text[i:i + 2])       # a two-character escape: pyte's job
            pos = i + 2
        return "".join(out)

    def _csi(self, m: re.Match) -> str:
        prefix, params, inter, final = m.groups()
        if final == "m" and not prefix and not inter:
            return _sgr(params) if (":" in params or "58" in params or "59" in params) else m.group(0)
        if prefix in ("<", "=", ">", "!"):
            # Keyboard-protocol switches, "which terminal are you" and soft
            # reset. pyte would read `CSI > 4 ; 2 m` as "underline, dim".
            if prefix == ">" and final == "c" and self._reply:
                self._reply(f"{ESC}[>0;10;1c")          # secondary device attributes
            return ""
        if prefix == "?" and final not in "hlJK":
            # Queries such as `CSI ? u`; answering "not supported" by silence
            # is what older terminals do. Device status is the exception.
            if final == "n" and params == "6" and self._reply:
                self._reply(f"{ESC}[?{self.cursor.y + 1};{self.cursor.x + 1}R")
            return ""
        if inter:
            return ""                    # cursor shape, DECRQM and friends
        if final == "t":
            return ""                    # window manipulation
        return m.group(0)

    def _osc(self, body: str) -> None:
        code, _, value = body.partition(";")
        if code in ("0", "2"):
            self.window_title = value[:200]
        elif code in ("10", "11") and value == "?" and self._reply:
            # Programs ask for the colours to pick a light or dark theme.
            c = self.foreground if code == "10" else self.background
            rgb = "/".join(c[k:k + 2] * 2 for k in (0, 2, 4))
            self._reply(f"{ESC}]{code};rgb:{rgb}{ESC}\\")

    def write_process_input(self, data: str) -> None:
        if self._reply:
            self._reply(data)

    # -- scrollback ----------------------------------------------------------

    def index(self) -> None:
        top = self.margins.top if self.margins else 0
        bottom = self.margins.bottom if self.margins else self.lines - 1
        if not self.alt and top == 0 and self.cursor.y == bottom:
            self.history.append(self.buffer[0])
            self.pushed += 1
        super().index()

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        if how == 3:
            # "Clear the scrollback as well": sent before a full redraw, so
            # keeping the old lines would show everything twice.
            if not self.alt:
                self.history.clear()
            self.dirty.update(range(self.lines))
            return
        super().erase_in_display(how, *args, **kwargs)

    # -- alternate screen ----------------------------------------------------

    def _blank_buffer(self):
        return defaultdict(lambda: StaticDefaultDict[int, Char](self.default_char))

    def set_mode(self, *modes, **kwargs) -> None:
        if kwargs.get("private") and not self.alt and any(m in _ALT_SCREEN for m in modes):
            self._main = (self.buffer, self.cursor.x, self.cursor.y, self.cursor.attrs)
            self.buffer = self._blank_buffer()
            self.alt = True
            self.cursor.x = self.cursor.y = 0
            self.dirty.update(range(self.lines))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs) -> None:
        if kwargs.get("private") and self.alt and any(m in _ALT_SCREEN for m in modes):
            self.buffer, x, y, attrs = self._main
            self._main = None
            self.alt = False
            self.cursor.x = min(x, self.columns - 1)
            self.cursor.y = min(y, self.lines - 1)
            self.cursor.attrs = attrs
            self.set_margins()
            self.dirty.update(range(self.lines))
        super().reset_mode(*modes, **kwargs)

    def resize(self, lines: Optional[int] = None, columns: Optional[int] = None) -> None:
        lines = lines or self.lines
        columns = columns or self.columns
        if lines == self.lines and columns == self.columns:
            return
        if lines < self.lines:
            # pyte always cuts a shorter screen from the top, which throws
            # away a short session's whole content. Cut only what has to go
            # to keep the cursor on screen, keep that in the scrollback, and
            # take the rest from the empty bottom.
            drop = min(self.lines - lines, max(0, self.cursor.y - lines + 1))
            if drop:
                if not self.alt:
                    self.history.extend(self.buffer[y] for y in range(drop))
                    self.pushed += drop
                for y in range(self.lines - drop):
                    self.buffer[y] = self.buffer[y + drop]
                self.cursor.y -= drop
            for y in range(lines, self.lines):
                self.buffer.pop(y, None)
            self.lines = lines
        super().resize(lines, columns)
        self.cursor.y = min(self.cursor.y, self.lines - 1)
        self.set_margins()
        self.dirty.update(range(self.lines))

    # -- reading -------------------------------------------------------------

    def row(self, index: int):
        """A line by its position in scrollback + screen, top line first."""
        n = len(self.history)
        return self.history[index] if index < n else self.buffer[index - n]

    @property
    def total_rows(self) -> int:
        return len(self.history) + self.lines

    @property
    def base(self) -> int:
        """The lasting number of the oldest line still held. A line keeps its
        number (base + position) while newer lines push it up the scrollback,
        which is what lets a mouse selection stay on the same text."""
        return self.pushed - len(self.history)

    def row_text(self, index: int, start: int = 0, end: Optional[int] = None) -> str:
        line = self.row(index)
        end = self.columns if end is None else end
        return "".join(line[x].data for x in range(start, end))
