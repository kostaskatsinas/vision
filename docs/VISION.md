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

Steps 1 to 4 of the plan below are done: Vision runs on the target machine,
asks before risky actions, reads and writes Google Calendar, and sees, clicks
and types on Wayland. Step 5 is built: Claude Code and Gemini CLI work as
background helpers, and a local model can be switched on (see Models).

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
3. **Add Google Calendar** for reading, creating, moving and deleting events
   (see below).
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
| `google_calendar` with `action=delete` | yes, and the banner names the event |
| `google_calendar` creating an event with attendees (Google emails them) | yes |
| `cli_helper` with `access=write` (Claude Code or Gemini CLI may change files in a folder) | yes, and the banner names the folder |
| Any plugin whose name contains send, mail, message, install or delete | yes |
| Everything else | no |

## Google Calendar

`actions/google_calendar.py` talks to the Google Calendar API with your own
Google sign-in key, so your calendar goes only between this PC and Google.

| Say | What happens |
|---|---|
| "What's on today?" / "What do I have this week?" | Lists events from all the calendars you show in Google Calendar, numbered |
| "When is my dentist appointment?" | Searches the next 90 days |
| "Add lunch with Maria tomorrow at 2" | Creates a one-hour event in your main calendar and mentions any overlap |
| "Remind me on my phone an hour before" | Adds a Google reminder (`reminder_minutes`) to the event |
| "Move number 2 to Friday at 10" | Moves it and keeps its length; guests are not emailed |
| "Delete the dentist" | Shows the confirm banner with the exact event first |
| "Invite anna@example.com" | Shows the confirm banner first, because Google emails the invitation |

The `reminder` tool still sets quick reminders on this PC only (a desktop
notification, also spoken). Calendar reminders come from Google, so they reach
your phone.

**One-time setup** (about 10 minutes, in the browser):

1. Open [console.cloud.google.com](https://console.cloud.google.com/), sign in
   with the Google account whose calendar Vision should use, and create a
   project called `Vision`.
2. Open the [Google Calendar API page](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
   for that project and click **Enable**.
3. Open **Google Auth Platform** (search for it at the top) and click **Get
   started**. App name `Vision`, your email as the support and contact email,
   audience **External**. Agree and create.
4. In **Audience**, click **Publish app** and confirm. Without this, Google
   ends the sign-in every 7 days. Nobody else can use it unless you give
   them the file from the next step.
5. In **Clients**, click **Create client**, choose **Desktop app**, name it
   `Vision`, create it, and click **Download JSON**.
6. Leave the file in `~/Downloads` (Vision picks up the newest
   `client_secret_*.json` there) or save it as
   `~/.config/vision/google_client.json`.
7. Say "connect my Google Calendar". The browser opens Google's sign-in. Google
   says the app is not verified, because it is your own: click **Advanced**,
   then **Go to Vision**, tick the calendar permissions and continue. Vision
   says when it is connected.

The sign-in is kept in `~/.config/vision/google_calendar_token.json`, readable
only by you. Vision asks for two permissions: events on your calendars, and the
list of your calendars. It cannot change calendar settings or sharing. "Sign out
of Google Calendar" deletes the token; to revoke it at Google too, remove Vision
at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

Tests that need no Google account: `python -m actions._google_calendar_tests`.

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
cannot power the live voice loop directly. They do cover the command-line
tools, which is how Vision uses them.

### Helpers: Claude Code and Gemini CLI

`actions/cli_helper.py` hands a long job to one of them in the background.
Vision keeps talking and announces the answer when it is done; the full answer
is shown on screen and saved in `~/.vision/helpers/`.

| Say | What happens |
|---|---|
| "Ask Claude to explain how the wake word works in ~/vision" | Claude Code reads that folder and answers. No confirm. |
| "Have Gemini compare these three laptops in detail" | Gemini CLI researches on the web. No confirm. |
| "Ask Claude to add a README to ~/projects/demo" | Confirm banner first, naming the folder. Then it may create and edit files there. |
| "How is the helper doing?" / "Stop the helper" | Status, or cancel. |

What a helper is allowed to do is set by the flags Vision starts it with:

| Level | Claude Code | Gemini CLI |
|---|---|---|
| read (default) | only `Read, Glob, Grep, WebSearch, WebFetch` exist | `--approval-mode default` |
| write (after CONFIRM) | the same plus `Write, Edit`, with `acceptEdits` | `--approval-mode auto_edit` |

Neither level allows shell commands, so a helper cannot delete files, install
software or send anything. Claude Code runs with `--restricted`, which holds it
to the folder you named: checked on the target machine, it could neither read
nor write a neighbouring folder. Gemini CLI could not be checked there (its
sign-in fails), so treat it as untested until it is. A write job is refused for your home folder itself,
`~/.config`, `~/.ssh` and `~/.vision`. Jobs stop after 15 minutes unless you
give another limit (at most 60), and two can run at once.

Both tools must be signed in on this PC: run `claude` or `gemini` once in a
terminal. Each job counts against that subscription's usage limits.

### Local model (offline fallback)

Upstream already speaks to Ollama; nothing in the fork is needed. It is not
installed by default because it is a large download.

1. Install Ollama (asks for your password):
   `curl -fsSL https://ollama.com/install.sh | sh`
2. Download a model that fits the 11 GB card with room to spare:
   `ollama pull qwen3:8b` (about 5 GB; upstream was tested with it).
3. In Vision open ⚙ → ENGINE & PROVIDERS, choose **Ollama**, pick the model,
   and set it as the provider for background requests. Vision then falls back
   to it when Gemini is out of quota or the internet is down.
4. Optional, for a fully offline voice: in the same panel click the install
   buttons for Whisper (hearing) and Piper (speaking), then choose MY OWN
   SETUP. Expect slower answers and more mistakes in choosing tools than with
   Gemini Live.

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
