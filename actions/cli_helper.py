"""
actions/cli_helper.py - hand a long job to Claude Code or Gemini CLI.

Both are command-line assistants the user is already signed in to with their
own subscriptions. They are too slow for speech and well suited to research,
reading a folder, reviewing code and writing files, so Vision starts one in the
background, carries on talking, and reports the answer when it is done.

Two levels, chosen by the tool call and enforced by the flags given to the
helper, not by its good behaviour:

  read   - the helper may read files in the working folder and use the web.
           It cannot change or run anything. No confirm needed.
  write  - it may also create and edit files inside the working folder.
           Vision asks first (core/ask_first.py), and the banner names the
           folder.

Neither level lets a helper run shell commands, so it cannot delete files,
install software or send anything. The answer is treated as outside data
(core/outcome.untrusted): a helper reads the web, so its text may carry
someone else's instructions.

Tests: python -m actions._cli_helper_tests
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

HOME = Path.home()
WORK_DIR = HOME / "Vision" / "helper"          # default folder for jobs
RESULTS_DIR = HOME / ".vision" / "helpers"     # every answer is kept here
DEFAULT_MINUTES = 15
MAX_MINUTES = 60
MAX_TASK_CHARS = 8000

# Folders a write job may never be pointed at: too much damage for one "yes".
_NO_WRITE = (HOME, HOME / ".config", HOME / ".ssh", HOME / ".vision", Path("/"))

HELPERS = {
    "claude": {"binary": "claude", "name": "Claude Code"},
    "gemini": {"binary": "gemini", "name": "Gemini CLI"},
}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_counter = 0


def _command(helper: str, level: str) -> list[str]:
    """The exact command for a helper at a level. The task goes in on stdin, so
    nothing in it can be read as a flag."""
    if helper == "claude":
        read = "Read,Glob,Grep,WebSearch,WebFetch"
        tools = read + ",Write,Edit" if level == "write" else read
        # --tools is the set that exists for this run at all; --allowedTools
        # lets those run without a prompt nobody is there to answer.
        cmd = ["claude", "-p", "--output-format", "text", "--no-session-persistence",
               "--tools", tools, "--allowedTools", tools,
               "--disallowedTools", "Bash" if level == "write" else "Bash,Write,Edit,NotebookEdit"]
        if level == "write":
            cmd += ["--permission-mode", "acceptEdits"]
        return cmd
    if helper == "gemini":
        # With no prompt argument Gemini CLI reads the job from stdin and runs
        # without a terminal, where a tool that needs approval is refused.
        # Shell commands need approval in both of these modes.
        return ["gemini", "--approval-mode", "auto_edit" if level == "write" else "default",
                "--output-format", "text"]
    raise ValueError(f"unknown helper {helper}")


def installed(helper: str) -> bool:
    return shutil.which(HELPERS[helper]["binary"]) is not None


def _pick(helper: str) -> tuple[Optional[str], str]:
    want = str(helper or "auto").strip().lower()
    if want in ("claude", "claude code", "claude_code"):
        want = "claude"
    elif want in ("gemini", "gemini cli", "gemini_cli"):
        want = "gemini"
    elif want not in ("auto", ""):
        return None, f"there is no helper called '{helper}'. Use claude or gemini."
    order = [want] if want in HELPERS else ["claude", "gemini"]
    for h in order:
        if installed(h):
            return h, ""
    names = " or ".join(HELPERS[h]["name"] for h in order)
    return None, f"{names} is not installed on this computer."


def _folder(raw, level: str) -> tuple[Optional[Path], str]:
    if not raw:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        return WORK_DIR, ""
    path = Path(os.path.expanduser(str(raw))).resolve()
    if not path.is_dir():
        return None, f"the folder {path} does not exist."
    if level == "write" and path in _NO_WRITE:
        return None, (f"a helper may not change files directly in {path}. "
                      "Name a project folder inside it.")
    return path, ""


def _level(p: dict) -> str:
    raw = str(p.get("access") or "read").strip().lower()
    return "write" if raw in ("write", "edit", "change", "files") else "read"


def _action(p: dict) -> str:
    word = str(p.get("action") or "start").strip().lower()
    return {"run": "start", "ask": "start", "stop": "cancel", "list": "status"}.get(word, word)


# ── Ask-first hook (called by core/ask_first.py) ─────────────────────────────

def confirmation(args: dict) -> Optional[tuple[str, str]]:
    args = args or {}
    if _action(args) != "start" or _level(args) != "write":
        return None
    helper, _ = _pick(args.get("helper"))
    folder, problem = _folder(args.get("folder"), "write")
    if helper is None or folder is None:
        return None                      # the run itself reports the problem
    name = HELPERS[helper]["name"]
    task = " ".join(str(args.get("task") or "").split())
    return (f"Let {name} change files",
            f"{name} may create and edit files in {folder}. It cannot run "
            f"commands or delete files. Job: {task[:120]}")


# ── Running ──────────────────────────────────────────────────────────────────

def _tell(text: str) -> None:
    try:
        from core import ask_first
        ask_first.tell(text)
    except Exception as e:
        print(f"[Helper] could not report back: {e}")


def _finish(job: dict, player) -> None:
    from core import outcome
    name = HELPERS[job["helper"]]["name"]
    mins = (job["ended"] - job["started"]) / 60
    if job["status"] == "done":
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(job['ended']))}-{job['id']}.md"
        try:
            path.write_text(f"# {job['task'][:200]}\n\n{job['result']}\n", encoding="utf-8")
            job["file"] = str(path)
        except Exception as e:
            print(f"[Helper] could not save the answer: {e}")
        if player:
            try:
                player.show_content(f"{name}: {job['task'][:60]}", job["result"])
            except Exception:
                pass
        _tell(f"[SYSTEM] The background job {job['id']} given to {name} finished after "
              f"{mins:.1f} minutes. Its answer follows; it is on screen too. "
              + outcome.untrusted("helper", job["result"][:6000])
              + " Tell the user the main points briefly.")
    elif job["status"] == "cancelled":
        pass                              # the user asked; nothing to announce
    else:
        _tell(f"[SYSTEM] The background job {job['id']} given to {name} did not finish: "
              + outcome.untrusted("helper", job["error"][:600])
              + " Tell the user briefly.")
    if player:
        try:
            player.write_log(f"[Helper] {name} job {job['id']}: {job['status']}")
        except Exception:
            pass


def _run(job: dict, player) -> None:
    out, err = "", ""
    try:
        proc = subprocess.Popen(
            job["cmd"], cwd=job["folder"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", start_new_session=True)
        job["proc"] = proc
        try:
            out, err = proc.communicate(job["task"], timeout=job["minutes"] * 60)
        except subprocess.TimeoutExpired:
            _kill(proc)
            out, err = proc.communicate()
            job["status"], job["error"] = "failed", (
                f"it was stopped after the {job['minutes']} minute limit")
    except FileNotFoundError:
        job["status"], job["error"] = "failed", "the program is not installed"
    except Exception as e:
        job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"
    job["ended"] = time.time()
    if job["status"] == "running":
        code = job["proc"].returncode
        if job.get("cancel"):
            job["status"] = "cancelled"
        elif code == 0 and (out or "").strip():
            job["status"], job["result"] = "done", out.strip()
        else:
            tail = " ".join((err or out or "").split())[-400:]
            job["status"] = "failed"
            job["error"] = f"it exited with code {code}" + (f": {tail}" if tail else " and no answer")
    job.pop("proc", None)
    _finish(job, player)


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def _start(p: dict, player) -> str:
    global _counter
    task = str(p.get("task") or "").strip()
    if not task:
        return "The helper needs the whole job, in the user's words."
    if len(task) > MAX_TASK_CHARS:
        return f"That job is too long to hand over ({len(task)} characters, the limit is {MAX_TASK_CHARS})."
    level = _level(p)
    helper, problem = _pick(p.get("helper"))
    if helper is None:
        return f"Could not start: {problem}"
    folder, problem = _folder(p.get("folder"), level)
    if folder is None:
        return f"Could not start: {problem}"
    try:
        minutes = max(1, min(MAX_MINUTES, int(p.get("minutes") or DEFAULT_MINUTES)))
    except (TypeError, ValueError):
        minutes = DEFAULT_MINUTES
    with _lock:
        running = [j for j in _jobs.values() if j["status"] == "running"]
        if len(running) >= 2:
            return "Two helper jobs are already running. Wait for one, or cancel it."
        _counter += 1
        job = {"id": f"h{_counter}", "helper": helper, "level": level, "task": task,
               "folder": str(folder), "minutes": minutes, "status": "running",
               "started": time.time(), "ended": 0.0, "result": "", "error": "",
               "cmd": _command(helper, level)}
        _jobs[job["id"]] = job
    threading.Thread(target=_run, args=(job, player), name=f"helper-{job['id']}",
                     daemon=True).start()
    name = HELPERS[helper]["name"]
    can = "may change files in" if level == "write" else "is reading in"
    return (f"Started job {job['id']} with {name} (limit {minutes} minutes). It {can} "
            f"{folder}. The answer is announced when it is done; the conversation "
            "carries on meanwhile.")


def _find(p: dict) -> Optional[dict]:
    jid = str(p.get("job_id") or "").strip().lower()
    with _lock:
        if jid:
            return _jobs.get(jid)
        jobs = sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)
    running = [j for j in jobs if j["status"] == "running"]
    return (running or jobs or [None])[0]


def _status() -> str:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)[:6]
    have = ", ".join(HELPERS[h]["name"] for h in HELPERS if installed(h)) or "none"
    if not jobs:
        return f"No helper jobs yet. Helpers installed: {have}."
    rows = []
    for j in jobs:
        end = j["ended"] or time.time()
        rows.append(f"{j['id']} [{j['status']}] {HELPERS[j['helper']]['name']}, "
                    f"{(end - j['started']) / 60:.1f} min: {j['task'][:70]}")
    return "\n".join(rows) + f"\nHelpers installed: {have}."


def cli_helper(parameters: dict, player=None) -> str:
    from core import outcome
    p = parameters or {}
    action = _action(p)
    if action == "start":
        return _start(p, player)
    if action == "status":
        return outcome.untrusted("helper", _status())
    job = _find(p)
    if job is None:
        return "There is no helper job to act on."
    if action == "cancel":
        proc = job.get("proc")
        if job["status"] != "running" or proc is None:
            return f"Job {job['id']} is not running."
        job["cancel"] = True
        _kill(proc)
        return f"Stopped job {job['id']}."
    if action == "result":
        if job["status"] == "running":
            return f"Job {job['id']} is still running."
        if job["status"] != "done":
            return f"Job {job['id']} {job['status']}: " + outcome.untrusted("helper", job["error"])
        return (f"Answer of job {job['id']} (saved as {job.get('file', 'not saved')}): "
                + outcome.untrusted("helper", job["result"][:12000]))
    return f"Unknown helper action '{p.get('action')}'. Use start, status, result or cancel."


TOOL = {
    "name": "cli_helper",
    "description": (
        "Hands a long job to Claude Code or Gemini CLI, the command-line assistants on this "
        "computer, running in the background on the user's own subscriptions. Use it when the "
        "user asks for Claude, Claude Code, Gemini CLI or 'a helper', or for deep work that "
        "takes minutes: explaining or reviewing the code in a folder, a careful written "
        "comparison, drafting a long document, or (access write) creating or editing files in "
        "a project folder. Not for quick questions; answer those yourself or use web_search. "
        "start returns at once and the answer is announced later, so tell the user it has "
        "started and carry on. access read (default) lets the helper read the folder and the "
        "web only. access write lets it create and edit files in the folder, and shows an "
        "on-screen confirm first: tell the user to confirm on screen. A helper can never run "
        "commands, delete files or install anything."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | status | result | cancel"},
            "task": {"type": "STRING",
                     "description": "start: the whole job, self-contained, in the user's words"},
            "helper": {"type": "STRING",
                       "description": "claude | gemini | auto (default: Claude Code if installed)"},
            "access": {"type": "STRING",
                       "description": "read (default) or write, when the user wants files created or changed"},
            "folder": {"type": "STRING",
                       "description": "the folder to work in, e.g. ~/vision. Default: ~/Vision/helper"},
            "minutes": {"type": "INTEGER", "description": "time limit, default 15, at most 60"},
            "job_id": {"type": "STRING", "description": "status/result/cancel: which job; default the latest"},
        },
        "required": ["action"],
    },
    "handler": cli_helper,
}
