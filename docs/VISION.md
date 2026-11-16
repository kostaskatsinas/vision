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

The wake word is "Vision" ("Hey Vision" works too). Turn it on from CONTROLS
→ WAKE WORD; the first click installs `sherpa-onnx` and downloads a 17 MB
model into `config/wake_word/`. Vision then sleeps until it hears its name and
goes back to sleep after 2 minutes of silence. Upstream's wake word was a
pretrained model that only knew its own phrase, so `core/wake_word.py` now uses
a keyword spotter that takes the phrase as text and needs no training. If it
wakes too easily or not easily enough, change `DEFAULT_THRESHOLD` there.

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

Vision stays on Wayland. Upstream switches seeing the screen, the mouse and the
keyboard off there, because pyautogui, mss and xdotool silently do nothing.
`core/wayland_desktop.py` replaces them with GNOME's own door for this, the
xdg-desktop-portal RemoteDesktop and ScreenCast interfaces:

| Ability | How |
|---|---|
| See the screen | One PipeWire stream per monitor; a frame is read with `gst-launch-1.0 pipewiresrc` |
| Move, click, drag, scroll | `NotifyPointerMotionAbsolute`, `NotifyPointerButton`, `NotifyPointerAxisDiscrete` |
| Type and shortcuts | `NotifyKeyboardKeysym` for text, `NotifyKeyboardKeycode` for keys and shortcuts |

At startup `wayland_desktop.install()` registers a stand-in for the `pyautogui`
module, so the existing actions work unchanged.

**Permission.** The first use opens a GNOME dialog; tick the monitors and click
Share. The approval is remembered in `~/.config/vision/portal_token` (delete it
to be asked again). While Vision is using the screen or input, GNOME shows its
orange screen-sharing indicator in the top bar, and its stop button ends the
session. Vision also lets go by itself after 60 seconds without use.

**When it acts.** Vision looks at the screen only when asked, and clicks or
types only when asked in that request. It types the words dictated and does not
press Enter unless told to. "Stop" or "hands off" releases everything
(`computer_control` action `release`). Screenshots are sent to Gemini.

**Several monitors.** Positions are on the working screen: the largest one
until you say otherwise ("use the left screen", "use screen 3", "next screen").

**Typing Greek.** GNOME only accepts a letter that the active keyboard layout
holds, so Vision switches layout with Super+Space for Greek letters and
switches back afterwards. Accented letters are typed as dead key plus letter.

**Limits.**
- If you move the mouse while Vision is clicking, the two fight over the
  pointer. Vision places the pointer again right before pressing, but it cannot
  read where the pointer really is.
- Raising another program's window is not possible; clicking it is.
- `VISION_NO_WAYLAND_DESKTOP=1` switches this off.

Needs `jeepney` (in `requirements.txt`) and `gst-launch-1.0` with the PipeWire
plugin (`sudo apt install gstreamer1.0-tools gstreamer1.0-pipewire` if absent).
Tests that need no desktop: `python -m core._wayland_tests`.

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
