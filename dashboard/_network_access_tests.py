"""Tests for dashboard.server._ensure_network_access with fake firewall tools.

Run: python dashboard/_network_access_tests.py   (from the repo root)
"""
from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import server  # noqa: E402

_results = {"passed": 0, "failed": 0}


def check(name: str, ok: bool, detail: str = "") -> None:
    _results["passed" if ok else "failed"] += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


def _tool(folder: str, name: str, body: str) -> None:
    path = os.path.join(folder, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def _run(folder: str, log: str, deny: bool = False) -> tuple[str, str]:
    open(log, "w").close()
    env = dict(os.environ)
    os.environ["PATH"] = folder + ":/usr/bin:/bin"
    if deny:
        os.environ["DENY"] = "1"
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            server._ensure_network_access(8765, 8766, 8765)
    finally:
        os.environ.clear()
        os.environ.update(env)
    with open(log) as f:
        return out.getvalue(), f.read()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "log")
        _tool(tmp, "pkexec", f'echo "PKEXEC $*" >> {log}; [ -n "$DENY" ] && exit 126; "$@"')
        _tool(tmp, "sudo", "exit 1")
        _tool(tmp, "ufw", f'[ "$1" = status ] && {{ echo "Status: active"; exit 0; }}; echo "ufw $*" >> {log}')

        out, calls = _run(tmp, log)
        check("ufw: both ports opened with one prompt",
              calls.count("PKEXEC") == 1 and "ufw allow 8765/tcp" in calls
              and "ufw allow 8766/tcp" in calls
              and calls.splitlines().count("ufw allow 8765/tcp") == 1,
              calls)
        check("ufw: success is printed", "allowed" in out, out)

        out, calls = _run(tmp, log, deny=True)
        check("refused prompt prints the manual command",
              "Run manually" in out and "ufw allow 8766/tcp" in out, out)

        os.remove(os.path.join(tmp, "ufw"))
        _tool(tmp, "iptables",
              f'[ "$1" = -S ] && {{ echo "-P INPUT DROP"; exit 0; }}; '
              f'[ "$1" = -D ] && exit 1; echo "iptables $*" >> {log}')
        out, calls = _run(tmp, log)
        check("iptables: both ports accepted in one prompt",
              calls.count("PKEXEC") == 1
              and "iptables -I INPUT 1 -p tcp --dport 8765 -j ACCEPT" in calls
              and "iptables -I INPUT 1 -p tcp --dport 8766 -j ACCEPT" in calls,
              calls)

        _tool(tmp, "iptables",
              f'[ "$1" = -S ] && {{ printf "%s\\n" "-P INPUT ACCEPT" "-A INPUT -j lan" '
              f'"-A lan -p tcp --dport 8765 -j DROP"; exit 0; }}; '
              f'[ "$1" = -D ] && exit 1; echo "iptables $*" >> {log}')
        out, calls = _run(tmp, log)
        check("iptables: a drop in a custom chain is found and the rule goes first",
              "iptables -I INPUT 1 -p tcp --dport 8765 -j ACCEPT" in calls, out + calls)

        # An ACCEPT already exists below the jump: it is removed and re-added on top.
        state = os.path.join(tmp, "has_rule")
        open(state, "w").close()
        _tool(tmp, "iptables",
              f'[ "$1" = -S ] && {{ printf "%s\\n" "-A INPUT -j lan" "-A INPUT -p tcp --dport 8765 -j ACCEPT" '
              f'"-A lan -j DROP"; exit 0; }}; '
              f'if [ "$1" = -D ]; then [ -e {state} ] && {{ rm {state}; echo "iptables $*" >> {log}; exit 0; }}; exit 1; fi; '
              f'echo "iptables $*" >> {log}')
        out, calls = _run(tmp, log)
        lines = calls.splitlines()
        d = "iptables -D INPUT -p tcp --dport 8765 -j ACCEPT"
        i = "iptables -I INPUT 1 -p tcp --dport 8765 -j ACCEPT"
        check("iptables: an existing ACCEPT below a jump is moved to the top",
              d in lines and i in lines and lines.index(d) < lines.index(i), calls)

        _tool(tmp, "iptables", '[ "$1" = -S ] && { echo "-P INPUT ACCEPT"; exit 0; }')
        out, calls = _run(tmp, log)
        check("no active firewall: nothing is run", "PKEXEC" not in calls and not out, out + calls)

    print(f"{_results['passed']} passed, {_results['failed']} failed")
    return 1 if _results["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
