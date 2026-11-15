# Vision

Vision is a personal voice assistant for a Linux desktop. It opens and closes
apps, searches the web, reads and creates calendar events, sets reminders, and
hands longer jobs to background helpers.

This repository is a fork of
[FatihMakes/Mark-LVII](https://github.com/FatihMakes/Mark-LVII). The upstream
project provides the voice engine, the window, the tools and the plugin system.
This document describes what the fork adds and how it is set up on the target
machine. The upstream documentation is in [`readme.md`](../readme.md).

## Status

Planning is done and no fork-specific code has been written yet. The steps
below are the agreed order of work.

## Target machine

| | |
|---|---|
| OS | Linux, Wayland session |
| CPU | Intel Core i9-7920X, 12 cores / 24 threads |
| RAM | 30 GiB |
| GPU | NVIDIA GeForce GTX 1080 Ti, 11 GB VRAM |

## Requirements

**Day one**

- Open and close applications
- Search the web
- Read the calendar and create calendar events
- Set reminders
- Voice as the main way to talk to it

**Always ask first.** Vision must wait for a button press from the user before
it does any of these:

- Deleting files
- Sending a message or an email
- Installing an application

## Plan

1. **Run the upstream app unchanged** on the target machine with a Gemini API
   key, and confirm voice, the wake word, opening and closing apps, web search
   and reminders.
2. **Enforce the ask-first rules.** Upstream's confirmation gate
   (`core/confirm.py`) covers shutdown, restart and WiFi. File deletes go to the
   trash with undo. Extend the gate so the three actions above always wait for
   the user.
3. **Add Google Calendar** as a plugin for reading and creating events.
4. **Add a Wayland backend** for typing, clicking and seeing the screen (see
   below).
5. **Add the other models:** a local model for offline use, and Claude Code and
   Gemini CLI as background helpers.

A custom "Hey Vision" wake word is a small extra step after step 1. Upstream
ships a pretrained "Hey Jarvis" model (`core/wake_word.py`), so a new phrase
needs its own trained model. The assistant's displayed name is an existing
setting and needs no code change.

## Ask-first rule

Vision asks before it deletes a file, sends a message or email, or installs
software. `core/ask_first.py` decides which tool calls need this from the tool
name and arguments, and parks them behind the on-screen CONFIRM / CANCEL banner
(`core/confirm.py`). Nothing runs until you click CONFIRM; the banner expires
after 90 seconds. Once confirmed, the result is passed back so Vision can tell
you how it went.

| Tool call | Needs a click |
|---|---|
| `file_controller` with `action=delete` | yes |
| `send_message` (WhatsApp, Telegram, Signal, Discord, Instagram, Messenger) | yes |
| `dev_agent` build or improve jobs (may `pip install` packages) | yes |
| Any plugin whose name contains send, mail, message, install or delete | yes |
| Everything else | no |

## Wayland

Everything on the day-one list works on Wayland as is. Applications are
launched through `gtk-launch` and `xdg-open`, which do not depend on the display
server.

Upstream switches three abilities off when it detects a Wayland session
(`core/platform_support.py`): typing and clicking in other applications,
capturing the screen, and raising another application's window. The planned
backend restores them:

| Ability | Approach |
|---|---|
| Typing and clicking | `ydotool`, which injects input through the kernel's uinput device and works on any compositor. Needs a one-time setup with root. |
| Seeing the screen | The desktop's screenshot portal (`xdg-desktop-portal`), approved once by the user. |
| Raising a window | Depends on the desktop: a KDE tool on Plasma, a small extension on GNOME. |

Logging in to an Xorg session is the fallback and needs no code.

## Models

| Job | Model | Why |
|---|---|---|
| Live voice conversation | Gemini Live through a Gemini API key | Low latency speech in and out. The API has a free tier. |
| Long background tasks | Claude Code and Gemini CLI, signed in on the machine | They run on existing subscriptions. Too slow for speech, well suited to research, coding and file work. |
| Offline fallback | A local model through Ollama, with Whisper and Piper for speech | The GPU fits a model of roughly 8 to 14 billion parameters. Weaker than Gemini at choosing the right tool. |

Consumer Claude and Gemini subscriptions do not include API access, so they
cannot power the live voice loop directly. The current terms and quotas for
using each command line tool this way still need to be confirmed before step 5.

## Installing

Run Vision in a Python virtual environment, not in Docker. It needs direct
access to the microphone, speakers, desktop and running applications, and a
container would have to be opened up for each of those.

Upstream supports Python 3.11 to 3.13.

```bash
git clone https://github.com/kostaskatsinas/vision
cd vision
python3 -m venv .venv
source .venv/bin/activate
python setup.py
python main.py
```

`setup.py` installs the Python packages and the Playwright browsers. On first
launch, paste a Gemini API key into the setup screen.

After cloning, you can do all of the above with one command instead:

```bash
./vision.sh            # create .venv and install on first run, then start
./vision.sh --update   # pull the latest code first, then start
```

`vision.sh` only re-runs `setup.py` when `requirements.txt` or `setup.py`
changes. To start Vision from any folder, link it onto your PATH once:

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/vision.sh" ~/.local/bin/vision
```

Then type `vision` in any terminal.

Some voice actions call native tools. Install the ones you use with your
package manager:

| Action | Package |
|---|---|
| Volume | `pulseaudio-utils` (`pactl`) |
| Brightness | `brightnessctl` |
| Reminders | `systemd` (`systemd-run`) or `at` |
| Opening links | `xdg-utils` (`xdg-open`) |

## Keeping up with upstream

Fork-specific documentation lives in `docs/`, and changes to upstream files are
kept small, so upstream updates can be merged with few conflicts.

```bash
git remote add upstream https://github.com/FatihMakes/Mark-LVII
git fetch upstream
git merge upstream/main
```

## Licence

Upstream is licensed under CC BY-NC 4.0, and this fork keeps that licence.
Personal use is allowed. Commercial use is not. Credit for the original work
goes to FatihMakes.
