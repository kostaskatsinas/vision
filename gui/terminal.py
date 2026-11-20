"""gui/terminal.py - real terminals inside the Vision window.

Up to two terminals sit where the avatar normally is, side by side. Each one is
the user's own shell on a pseudo-terminal (core/terminal_session.py), so Claude
Code and Gemini CLI run in it exactly as they do in a terminal app, with the
same sign-ins.

  TerminalView   one terminal: draws the screen, sends the keys.
  TerminalPane   a view with its title bar and close button.
  TerminalDock   the page that holds the panes and the buttons to open them.

Only the user types here. Nothing in this file gives the assistant a way to
send text to a terminal.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSocketNotifier, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QFontMetricsF, QKeySequence, QPainter, QPen, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSplitter,
    QVBoxLayout, QWidget,
)

from core.terminal_session import (
    APP_CURSOR_KEYS, BRACKETED_PASTE, PROGRAMS, PtySession, TerminalScreen,
    resolve_folder, shell_command,
)
from gui.style import hud_font
from gui.theme import C

MAX_TERMINALS = 2

# Fixed colours, not tied to the accent: a program's red has to stay red.
_BG = "#02080c"
_FG = "#d5e4ea"
_SELECTION = QColor("#1f5a78")
_NAMED = {
    "black": "#1d2a31", "red": "#ff5c57", "green": "#5af78e", "brown": "#f3f99d",
    "blue": "#57a5ff", "magenta": "#ff6ac1", "cyan": "#5fe1f0", "white": "#d5e4ea",
    "brightblack": "#5c7078", "brightred": "#ff8a85", "brightgreen": "#8dfab0",
    "brightbrown": "#f8fbc0", "brightblue": "#8cc2ff", "brightmagenta": "#ff9dd6",
    "brightcyan": "#9aeef7", "brightwhite": "#ffffff",
}
_FOCUS_EVENTS = 1004 << 5
_MOUSE_MODES = (1000 << 5, 1002 << 5, 1003 << 5)
_MOUSE_SGR = 1006 << 5

_K = Qt.Key
# key -> (final letter, or number for the "~" form)
_CURSOR_KEYS = {_K.Key_Up: "A", _K.Key_Down: "B", _K.Key_Right: "C", _K.Key_Left: "D",
                _K.Key_Home: "H", _K.Key_End: "F"}
_TILDE_KEYS = {_K.Key_Insert: 2, _K.Key_Delete: 3, _K.Key_PageUp: 5, _K.Key_PageDown: 6,
               _K.Key_F5: 15, _K.Key_F6: 17, _K.Key_F7: 18, _K.Key_F8: 19, _K.Key_F9: 20,
               _K.Key_F10: 21, _K.Key_F11: 23, _K.Key_F12: 24}
_F1_F4 = {_K.Key_F1: "P", _K.Key_F2: "Q", _K.Key_F3: "R", _K.Key_F4: "S"}


def encode_key(key: int, text: str, shift: bool, ctrl: bool, alt: bool,
               app_cursor: bool = False) -> bytes:
    """The bytes a terminal sends for a key press, xterm style."""
    mod = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
    if key in _CURSOR_KEYS:
        ch = _CURSOR_KEYS[key]
        if mod > 1:
            return f"\x1b[1;{mod}{ch}".encode()
        return f"\x1b{'O' if app_cursor else '['}{ch}".encode()
    if key in _TILDE_KEYS:
        n = _TILDE_KEYS[key]
        return (f"\x1b[{n};{mod}~" if mod > 1 else f"\x1b[{n}~").encode()
    if key in _F1_F4:
        ch = _F1_F4[key]
        return (f"\x1b[1;{mod}{ch}" if mod > 1 else f"\x1bO{ch}").encode()
    if key in (_K.Key_Return, _K.Key_Enter):
        # Shift+Enter and Alt+Enter mean "new line, do not send" in Claude
        # Code and Gemini CLI; this is the form both read.
        return b"\x1b\r" if (shift or alt) else b"\r"
    if key == _K.Key_Backspace:
        base = b"\x08" if ctrl else b"\x7f"
        return b"\x1b" + base if alt else base
    if key == _K.Key_Backtab or (key == _K.Key_Tab and shift):
        return b"\x1b[Z"
    if key == _K.Key_Tab:
        return b"\t"
    if key == _K.Key_Escape:
        return b"\x1b"
    if ctrl:
        code = None
        if 0x40 <= key <= 0x5f:                 # @ A-Z [ \ ] ^ _
            code = key - 0x40
        elif key == _K.Key_Space:
            code = 0
        elif key == _K.Key_Question:
            code = 0x7f
        if code is not None:
            data = bytes([code])
            return b"\x1b" + data if alt else data
        if text and ord(text[0]) < 0x20:
            return text.encode()
        return b""
    if text:
        data = text.encode("utf-8", "replace")
        return b"\x1b" + data if alt else data
    return b""


def _colour(name: str, default: str) -> QColor:
    if name == "default":
        return QColor(default)
    hit = _NAMED.get(name)
    if hit:
        return QColor(hit)
    c = QColor("#" + name) if len(name) == 6 else QColor()
    return c if c.isValid() else QColor(default)


class TerminalView(QWidget):
    """One terminal. Starts its shell the first time it has a real size."""

    exited = pyqtSignal()
    started = pyqtSignal()
    start_failed = pyqtSignal(str)
    title_changed = pyqtSignal(str)

    _PAD = 6
    _FONT_PT = 10.5

    def __init__(self, program: str = "shell", folder: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.program = program if program in PROGRAMS else "shell"
        self._folder = resolve_folder(folder)
        self._session: Optional[PtySession] = None
        self._notifier: Optional[QSocketNotifier] = None
        self._scroll = 0                    # lines scrolled up from the bottom
        self._sel = None                    # ((line id, col), (line id, col))
        self._selecting = False
        self._title = ""
        self._pt = self._FONT_PT
        self._colour_cache: dict = {}

        self.screen = TerminalScreen(80, 24, reply=self._reply)
        self.screen.background = _BG.lstrip("#")
        self.screen.foreground = _FG.lstrip("#")

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 80)
        self._set_font(self._pt)

        self._paint_tmr = QTimer(self)      # many small writes, one repaint
        self._paint_tmr.setSingleShot(True)
        self._paint_tmr.setInterval(12)
        self._paint_tmr.timeout.connect(self.update)
        self._size_tmr = QTimer(self)       # a dragged splitter, one resize
        self._size_tmr.setSingleShot(True)
        self._size_tmr.setInterval(60)
        self._size_tmr.timeout.connect(self._apply_size)
        self._flush_tmr = QTimer(self)      # a large paste drains in pieces
        self._flush_tmr.setInterval(15)
        self._flush_tmr.timeout.connect(self._flush)

    # -- font and geometry ---------------------------------------------------

    def _set_font(self, pt: float) -> None:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        f.setPointSizeF(pt)
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setFixedPitch(True)
        f.setKerning(False)
        self._fonts = {}
        for bold in (False, True):
            for italic in (False, True):
                v = QFont(f)
                v.setBold(bold)
                v.setItalic(italic)
                self._fonts[(bold, italic)] = v
        fm = QFontMetricsF(f)
        self._cw = max(1.0, fm.horizontalAdvance("M" * 64) / 64)
        self._ch = float(max(1, round(fm.height())))
        self._ascent = fm.ascent()
        self._pt = pt

    def _grid(self) -> tuple[int, int]:
        cols = int((self.width() - 2 * self._PAD) // self._cw)
        rows = int((self.height() - 2 * self._PAD) // self._ch)
        return max(2, rows), max(8, cols)

    def _apply_size(self) -> None:
        if self.width() < 40 or self.height() < 30:
            return
        rows, cols = self._grid()
        if (rows, cols) != (self.screen.lines, self.screen.columns):
            self.screen.resize(rows, cols)
            if self._session:
                self._session.resize(rows, cols)
        if self._session is None:
            self._start(rows, cols)
        self._scroll = min(self._scroll, len(self.screen.history))
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Also what starts the shell: once the layout has settled, so the
        # program inside begins at its real size.
        self._size_tmr.start()

    def showEvent(self, e):
        super().showEvent(e)
        if self._session is None:
            self._size_tmr.start()

    def zoom(self, step: float) -> None:
        pt = self._FONT_PT if step == 0 else min(28.0, max(6.0, self._pt + step))
        self._set_font(pt)
        self._apply_size()

    # -- the shell -----------------------------------------------------------

    def _start(self, rows: int, cols: int) -> None:
        try:
            self._session = PtySession(shell_command(self.program), cwd=self._folder,
                                       rows=rows, cols=cols)
        except Exception as e:
            # No shell: the pane is removed by its owner, so a dead terminal
            # never takes one of the two places.
            self.start_failed.emit(f"{type(e).__name__}: {e}")
            return
        self._notifier = QSocketNotifier(self._session.fd, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_readable)
        self.started.emit()

    def _on_readable(self, *_a) -> None:
        if self._session is None:
            return
        data = self._session.read()
        if data is None:
            self.close_session()
            self.exited.emit()
            return
        if not data:
            return
        before = self.screen.pushed
        self.screen.feed(data)
        if self._scroll:
            # Reading older output: stay on the same lines as new ones arrive.
            self._scroll = min(len(self.screen.history),
                               self._scroll + self.screen.pushed - before)
        if self._sel and min(self._sel[0][0], self._sel[1][0]) < self.screen.base:
            self._sel = None
        if self.screen.window_title != self._title:
            self._title = self.screen.window_title
            self.title_changed.emit(self._title)
        if not self._paint_tmr.isActive():
            self._paint_tmr.start()

    def _reply(self, text: str) -> None:
        """Answers the screen gives to a program's questions (cursor position,
        colours)."""
        self._send(text.encode(), follow=False)

    def _send(self, data: bytes, follow: bool = True) -> None:
        if not data or self._session is None:
            return
        if follow and self._scroll:
            self._scroll = 0
            self.update()
        self._session.write(data)
        if not self._session.flush() and not self._flush_tmr.isActive():
            self._flush_tmr.start()

    def _flush(self) -> None:
        if self._session is None or self._session.flush():
            self._flush_tmr.stop()

    def close_session(self) -> None:
        """End the shell and whatever runs in it. Safe to call twice."""
        self._flush_tmr.stop()
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if self._session is not None:
            self._session.close()

    @property
    def running(self) -> bool:
        return self._session is not None and self._session.alive

    # -- painting ------------------------------------------------------------

    def _qcolor(self, name: str, default: str) -> QColor:
        key = (name, default)
        c = self._colour_cache.get(key)
        if c is None:
            c = self._colour_cache[key] = _colour(name, default)
        return c

    def _top_index(self) -> int:
        return self.screen.total_rows - self.screen.lines - self._scroll

    def _sel_range(self):
        if not self._sel:
            return None
        a, b = self._sel
        return (a, b) if a <= b else (b, a)

    def paintEvent(self, _e):
        p = QPainter(self)
        if not p.isActive():
            return
        scr = self.screen
        p.fillRect(self.rect(), QColor(_BG))
        cw, ch, pad = self._cw, self._ch, self._PAD
        top = self._top_index()
        base = scr.base
        sel = self._sel_range()
        cols = scr.columns
        cursor = scr.cursor
        show_cursor = (not cursor.hidden and self._scroll == 0 and self.running)

        for vy in range(scr.lines):
            index = top + vy
            if index < 0:
                continue
            line = scr.row(index)
            y = pad + vy * ch
            gid = base + index
            if sel and sel[0][0] <= gid <= sel[1][0]:
                s0 = sel[0][1] if gid == sel[0][0] else 0
                s1 = sel[1][1] if gid == sel[1][0] else cols - 1
            else:
                s0, s1 = 1, 0
            x = 0
            while x < cols:
                cell = line[x]
                picked = s0 <= x <= s1
                style = (cell.fg, cell.bg, cell.bold, cell.italics, cell.underscore,
                         cell.strikethrough, cell.reverse, picked)
                start = x
                chars = [cell.data]
                x += 1
                while x < cols:
                    nxt = line[x]
                    if (nxt.fg, nxt.bg, nxt.bold, nxt.italics, nxt.underscore,
                            nxt.strikethrough, nxt.reverse, s0 <= x <= s1) != style:
                        break
                    chars.append(nxt.data)
                    x += 1
                self._draw_run(p, pad + start * cw, y, start, chars, style)

        if show_cursor and 0 <= cursor.y < scr.lines:
            cx = min(cursor.x, cols - 1)
            rect = QRectF(pad + cx * cw, pad + cursor.y * ch, cw, ch)
            if self.hasFocus():
                cell = scr.buffer[cursor.y][cx]
                p.fillRect(rect, QColor(_FG))
                if cell.data.strip():
                    p.setFont(self._fonts[(cell.bold, cell.italics)])
                    p.setPen(QColor(_BG))
                    p.drawText(QPointF(rect.x(), rect.y() + self._ascent), cell.data)
            else:
                p.setPen(QPen(QColor(_FG), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

        if self._scroll:
            # A thin bar on the right: how far up the scrollback this is.
            total = len(scr.history) + scr.lines
            h = self.height()
            bar = max(18.0, h * scr.lines / total)
            pos = (h - bar) * (1 - self._scroll / max(1, len(scr.history)))
            p.fillRect(QRectF(self.width() - 3, pos, 3, bar), QColor(C.PRI_DIM))
        p.end()

    def _draw_run(self, p: QPainter, x0: float, y: float, start: int, chars: list, style) -> None:
        fg_n, bg_n, bold, italic, under, strike, reverse, picked = style
        fg = self._qcolor(fg_n, _FG)
        bg = self._qcolor(bg_n, _BG)
        if reverse:
            fg, bg = bg, fg
        cw, ch = self._cw, self._ch
        width = len(chars) * cw
        if picked:
            p.fillRect(QRectF(x0, y, width, ch), _SELECTION)
        elif reverse or bg_n != "default":
            p.fillRect(QRectF(x0, y, width, ch), bg)
        text = "".join(chars)
        if not text.strip():
            if not (under or strike):
                return
        p.setPen(fg)
        p.setFont(self._fonts[(bold, italic)])
        base_y = y + self._ascent
        if text.isascii():
            p.drawText(QPointF(x0, base_y), text)
        else:
            # Anything that may come from another font (Greek, box lines,
            # emoji) is placed cell by cell so the columns stay straight.
            run_start = None
            for i, c in enumerate(chars):
                if c and c.isascii():
                    if run_start is None:
                        run_start = i
                    continue
                if run_start is not None:
                    p.drawText(QPointF(x0 + run_start * cw, base_y), "".join(chars[run_start:i]))
                    run_start = None
                if c and c != " ":
                    p.drawText(QPointF(x0 + i * cw, base_y), c)
            if run_start is not None:
                p.drawText(QPointF(x0 + run_start * cw, base_y), "".join(chars[run_start:]))
        if under:
            p.fillRect(QRectF(x0, y + ch - 2, width, 1), fg)
        if strike:
            p.fillRect(QRectF(x0, y + ch / 2, width, 1), fg)

    # -- keyboard ------------------------------------------------------------

    def event(self, e):
        if e.type() == QEvent.Type.ShortcutOverride:
            # Keys belong to the terminal while it has the focus. Escape most
            # of all: the window uses it to stop Vision speaking, and Claude
            # Code and Gemini CLI use it all the time. The window's other
            # shortcuts (F4 mute, F11 fullscreen, push to talk) keep working.
            if e.key() == Qt.Key.Key_Escape or not self._is_window_shortcut(e):
                e.accept()
                return True
        return super().event(e)

    def _is_window_shortcut(self, e) -> bool:
        seq = QKeySequence(e.keyCombination())
        for sc in self.window().findChildren(QShortcut):
            if sc.isEnabled() and sc.key() == seq:
                return True
        return False

    def focusNextPrevChild(self, _next: bool) -> bool:
        return False                        # Tab is a key here, not "next field"

    def keyPressEvent(self, e):
        key, mods = e.key(), e.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        if ctrl and shift and key == Qt.Key.Key_C:
            self.copy_selection()
            return
        if (ctrl and shift and key == Qt.Key.Key_V) or (shift and not ctrl and key == Qt.Key.Key_Insert):
            self.paste()
            return
        if shift and not ctrl and key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            page = max(1, self.screen.lines - 1)
            self._scroll_by(page if key == Qt.Key.Key_PageUp else -page)
            return
        if ctrl and not alt and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(+1)
            return
        if ctrl and not alt and not shift and key == Qt.Key.Key_Minus:
            self.zoom(-1)
            return
        if ctrl and not alt and key == Qt.Key.Key_0:
            self.zoom(0)
            return

        data = encode_key(key, e.text(), shift, ctrl, alt,
                          app_cursor=APP_CURSOR_KEYS in self.screen.mode)
        if data:
            if self._sel and not self._selecting:
                self._sel = None
                self.update()
            self._send(data)
        e.accept()

    def inputMethodEvent(self, e):
        # Accented and composed characters (Greek tonos, dead keys) arrive
        # here on Wayland instead of as key presses.
        text = e.commitString()
        if text:
            self._send(text.encode("utf-8", "replace"))
        e.accept()

    def inputMethodQuery(self, query):
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            c = self.screen.cursor
            return QRect(int(self._PAD + c.x * self._cw), int(self._PAD + c.y * self._ch),
                         int(self._cw), int(self._ch))
        return super().inputMethodQuery(query)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        if _FOCUS_EVENTS in self.screen.mode:
            self._send(b"\x1b[I", follow=False)
        self.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if _FOCUS_EVENTS in self.screen.mode:
            self._send(b"\x1b[O", follow=False)
        self.update()

    # -- copy and paste ------------------------------------------------------

    def selected_text(self) -> str:
        sel = self._sel_range()
        if not sel:
            return ""
        scr = self.screen
        (g0, c0), (g1, c1) = sel
        lines = []
        for gid in range(g0, g1 + 1):
            index = gid - scr.base
            if not 0 <= index < scr.total_rows:
                continue
            a = c0 if gid == g0 else 0
            b = (c1 + 1) if gid == g1 else scr.columns
            lines.append(scr.row_text(index, a, b).rstrip())
        return "\n".join(lines)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def paste(self, text: Optional[str] = None) -> None:
        if text is None:
            text = QApplication.clipboard().text()
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\n", "\r")
        if BRACKETED_PASTE in self.screen.mode:
            # The program is told "this is a paste", so a pasted line break
            # does not run a command. The end marker may not appear inside.
            text = "\x1b[200~" + text.replace("\x1b", "") + "\x1b[201~"
        self._send(text.encode("utf-8", "replace"))

    # -- mouse ---------------------------------------------------------------

    def _cell_at(self, pos) -> tuple[int, int]:
        col = int((pos.x() - self._PAD) // self._cw)
        row = int((pos.y() - self._PAD) // self._ch)
        return (max(0, min(self.screen.lines - 1, row)),
                max(0, min(self.screen.columns - 1, col)))

    def _mouse_wanted(self, e) -> bool:
        """True when the program asked for mouse events and Shift is not held
        (Shift keeps the mouse for selecting text, as in other terminals)."""
        mode = self.screen.mode
        return (_MOUSE_SGR in mode and any(m in mode for m in _MOUSE_MODES)
                and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier))

    def _report_mouse(self, e, code: int, release: bool = False) -> None:
        row, col = self._cell_at(e.position())
        self._send(f"\x1b[<{code};{col + 1};{row + 1}{'m' if release else 'M'}".encode(),
                   follow=False)

    def _scroll_by(self, lines: int) -> None:
        new = max(0, min(len(self.screen.history), self._scroll + lines))
        if new != self._scroll:
            self._scroll = new
            self.update()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if not steps:
            return
        n = max(1, int(abs(steps) * 3 + 0.5))
        if self._mouse_wanted(e):
            for _ in range(min(n, 6)):
                self._report_mouse(e, 64 if steps > 0 else 65)
        elif self.screen.alt:
            # A full-screen program without mouse support: the wheel moves
            # its cursor, which scrolls less, man and the like.
            key = Qt.Key.Key_Up if steps > 0 else Qt.Key.Key_Down
            self._send(encode_key(key, "", False, False, False,
                                  APP_CURSOR_KEYS in self.screen.mode) * n)
        else:
            self._scroll_by(n if steps > 0 else -n)
        e.accept()

    def mousePressEvent(self, e):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if e.button() == Qt.MouseButton.MiddleButton:
            from PyQt6.QtGui import QClipboard
            self.paste(QApplication.clipboard().text(QClipboard.Mode.Selection)
                       or QApplication.clipboard().text())
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._mouse_wanted(e):
            self._report_mouse(e, 0)
            return
        row, col = self._cell_at(e.position())
        gid = self.screen.base + self._top_index() + row
        self._sel = ((gid, col), (gid, col))
        self._selecting = True
        self._moved = False
        self.update()

    def mouseMoveEvent(self, e):
        if not self._selecting or not self._sel:
            return
        row, col = self._cell_at(e.position())
        gid = self.screen.base + self._top_index() + row
        if (gid, col) != self._sel[1]:
            self._sel = (self._sel[0], (gid, col))
            self._moved = True
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._selecting:
            self._selecting = False
            if not getattr(self, "_moved", False):
                self._sel = None            # a plain click selects nothing
                self.update()
            else:
                from PyQt6.QtGui import QClipboard
                cb = QApplication.clipboard()
                if cb.supportsSelection():
                    cb.setText(self.selected_text(), QClipboard.Mode.Selection)
        elif self._mouse_wanted(e):
            self._report_mouse(e, 0, release=True)


def _link_css() -> str:
    return f"""
        QPushButton {{
            color: {C.TEXT_DIM}; background: transparent;
            border: none; padding: 2px 6px;
        }}
        QPushButton:hover {{ color: {C.PRI}; }}
        QPushButton:disabled {{ color: {C.BORDER}; }}
    """


def _link_btn(text: str, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setFont(hud_font(8, QFont.Weight.Bold))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)    # a click never takes the keys away
    b.setStyleSheet(_link_css())
    if tip:
        b.setToolTip(tip)
    return b


class TerminalPane(QWidget):
    """A terminal with its title bar."""

    closed = pyqtSignal(object)

    def __init__(self, program: str, folder: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.view = TerminalView(program, folder)
        self._name = PROGRAMS[self.view.program]["name"].upper()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 0, 2, 0)
        self._label = QLabel(f"▸  {self._name}")
        self._label.setFont(hud_font(8, QFont.Weight.Bold))
        self._label.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        bar.addWidget(self._label, stretch=1)
        x = _link_btn("✕", "Close this terminal and stop what runs in it")
        x.clicked.connect(self.close_pane)
        bar.addWidget(x)
        lay.addLayout(bar)

        frame = QWidget()
        frame.setObjectName("TermFrame")
        frame.setStyleSheet(f"#TermFrame {{ border: 1px solid {C.BORDER}; background: {_BG}; }}")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(1, 1, 1, 1)
        fl.addWidget(self.view)
        lay.addWidget(frame, stretch=1)

        self.view.title_changed.connect(self._on_title)
        self.view.exited.connect(self.close_pane)
        self.view.start_failed.connect(lambda _why: self.close_pane())

    def _on_title(self, title: str) -> None:
        title = " ".join(str(title).split())[:80]
        self._label.setText(f"▸  {self._name}  ·  {title}" if title else f"▸  {self._name}")

    def close_pane(self) -> None:
        self.view.close_session()
        self.closed.emit(self)


class TerminalDock(QWidget):
    """Up to two terminals side by side, and the buttons that open them.

    The window shows it in a panel under the face, whose header carries the
    open buttons (`open_buttons`) and the close cross; `header=False` leaves
    them for that header instead of putting a row of its own on top."""

    count_changed = pyqtSignal(int)     # how many terminals are open
    running_changed = pyqtSignal(int)   # how many of them have a shell that started
    start_failed = pyqtSignal(str)
    hide_requested = pyqtSignal()

    def __init__(self, parent=None, header: bool = True):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;" if not header else f"background: {C.BG};")
        self._panes: list[TerminalPane] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(*((8, 6, 8, 8) if header else (0, 0, 0, 0)))
        lay.setSpacing(4)

        self._open_btns = []
        for program, label in (("claude", "＋ CLAUDE"), ("gemini", "＋ GEMINI"), ("shell", "＋ SHELL"),
                               ("log", "＋ LOG")):
            b = _link_btn(label, f"Open a terminal with {PROGRAMS[program]['name']}")
            b.clicked.connect(lambda _=False, prog=program: self.open(prog))
            self._open_btns.append(b)
        if header:
            hdr = QHBoxLayout()
            title = QLabel("▣  TERMINALS")
            title.setFont(hud_font(9, QFont.Weight.Bold))
            title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
            hdr.addWidget(title)
            hdr.addStretch()
            for b in self._open_btns:
                hdr.addWidget(b)
            hide = _link_btn("▾  HIDE", "Hide the terminals. They keep running.")
            hide.clicked.connect(self.hide_requested.emit)
            hdr.addWidget(hide)
            lay.addLayout(hdr)

        self._split = QSplitter(Qt.Orientation.Horizontal)
        self._split.setHandleWidth(8)
        self._split.setChildrenCollapsible(False)
        self._split.setStyleSheet(f"""
            QSplitter::handle {{ background: transparent; }}
            QSplitter::handle:hover {{ background: {C.PRI_GHO}; }}
        """)
        lay.addWidget(self._split, stretch=1)

        self._hint = QLabel(
            "No terminal is open. Use ＋ CLAUDE, ＋ GEMINI or ＋ SHELL above to open\n"
            "one, or ＋ LOG to see what Vision prints. Two can be open at the same time.")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setFont(hud_font(9))
        self._hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(self._hint, stretch=1)
        self._sync()

    @property
    def open_buttons(self) -> list:
        return list(self._open_btns)

    def count(self) -> int:
        return len(self._panes)

    def open(self, program: str = "shell", folder: Optional[str] = None) -> bool:
        """Open a terminal. False when both places are taken."""
        if len(self._panes) >= MAX_TERMINALS:
            return False
        pane = TerminalPane(program if program in PROGRAMS else "shell", folder)
        pane.closed.connect(self._on_closed)
        pane.view.started.connect(self._report_running)
        pane.view.start_failed.connect(self.start_failed.emit)
        self._panes.append(pane)
        self._split.addWidget(pane)
        n = len(self._panes)
        self._split.setSizes([10_000] * n)      # equal shares
        self._sync()
        pane.view.setFocus()
        self.count_changed.emit(n)
        return True

    def _on_closed(self, pane) -> None:
        if pane not in self._panes:
            return
        self._panes.remove(pane)
        pane.setParent(None)
        pane.deleteLater()
        self._sync()
        if self._panes:
            self._panes[-1].view.setFocus()
        self.count_changed.emit(len(self._panes))
        self._report_running()

    def running(self) -> int:
        return sum(1 for p in self._panes if p.view.running)

    def _report_running(self) -> None:
        self.running_changed.emit(self.running())

    def _sync(self) -> None:
        any_open = bool(self._panes)
        self._split.setVisible(any_open)
        self._hint.setVisible(not any_open)
        full = len(self._panes) >= MAX_TERMINALS
        for b in self._open_btns:
            b.setEnabled(not full)

    def focus_terminal(self) -> None:
        if self._panes:
            self._panes[-1].view.setFocus()

    def close_all(self) -> None:
        """Stop every terminal. Called when Vision closes; asks nothing."""
        for pane in list(self._panes):
            pane.view.close_session()
