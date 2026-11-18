# Vision

Vision is a personal voice assistant for a Linux desktop. You talk to it, and it
opens and closes apps, searches the web, reads and writes your Google Calendar,
sets reminders, types and clicks for you, looks at your screen, works with your
files, and hands long jobs to background helpers while the conversation goes on.

It is a fork of [FatihMakes/Mark-LVII](https://github.com/FatihMakes/Mark-LVII),
set up for Ubuntu with GNOME on Wayland. Windows and macOS are not supported.

| Document | What is in it |
|---|---|
| This file | Every feature, how to start it, and where things are |
| [docs/VISION.md](docs/VISION.md) | Setup details: ask-first rules, Google Calendar sign-in, Wayland, models, installing |
| [AGENTS.md](AGENTS.md) | Rules for anyone (person or coding agent) changing the code |
| [docs/UPSTREAM_HISTORY.md](docs/UPSTREAM_HISTORY.md) | Upstream's release notes, kept as history |

---

## Quick start

```bash
git clone https://github.com/kostaskatsinas/vision
cd vision
./vision.sh
```

The first run creates a `.venv`, installs the packages and starts Vision. Paste
a Gemini API key into the setup screen. After that, `./vision.sh` just starts
it, and `./vision.sh --update` pulls the latest code first. More in
[docs/VISION.md](docs/VISION.md#installing).

---

## Features

### Talking to it

| Feature | What it does |
|---|---|
| Real-time voice | A live spoken conversation through the Gemini Live API, in any language. |
| Interrupt by voice | Start talking while Vision speaks and it stops within about a third of a second and listens; the first words you said are kept. On speakers this uses the desktop's own echo canceller (PipeWire's WebRTC `module-echo-cancel`, loaded through `pactl` for Vision's audio only and removed when Vision closes), so Vision does not stop at the sound of its own voice. With headphones it works without it. Turn it off in CONTROLS → INTERRUPT BY VOICE. With push-to-talk on, pressing the key interrupts instead. |
| Wake word | Say **"Vision"** or **"Hey Vision"**. It sleeps until called, goes back to sleep after 2 minutes of quiet or when you tell it to ("go to sleep", "κοιμήσου"), and sends no microphone audio while asleep (only a tenth of a second of generated silence every few seconds, so Gemini keeps the connection open). Detection runs on your PC (sherpa-onnx). Turn it on in CONTROLS → WAKE WORD. With the wake word off, Vision still puts itself to sleep after 1 hour with nobody talking to it, if the wake word is downloaded; change or turn this off with CONTROLS → AUTO-SLEEP (off, 30 min, 1 hour, 2 hours). |
| Push-to-talk | Hold **Ctrl+Space** and the microphone opens; it is closed the rest of the time. |
| Typing instead of talking | Type a request in the input box; it is handled the same way as speech. |
| Five voices | Charon, Puck, Kore, Fenrir or Aoede, switched live from the window. |
| Self-echo guard | It does not answer its own voice coming back through the speakers. |
| Instant acknowledgement | When a request will take a while, it says so straight away in your language. |
| Long conversations | Context is compressed as it goes, so one session can last hours. A dropped connection or a device change keeps the conversation. |
| Knows its own limits | Its name, abilities and what this session cannot do are worked out at startup and given to the model, so it says "I can't" instead of pretending. |

### Apps and windows

| Feature | What it does |
|---|---|
| Open an app | "Open Spotify". It checks the app really started. If it is already running, its window is brought forward instead of starting a second copy. |
| Open on a monitor | "Open terminal on the left monitor", "open Spotify on monitor 1", optionally tiled ("on the right half"). |
| Close an app | "Close Chrome". It asks the app to quit, so an app with unsaved work can show its save dialog. Vision's own processes are never closed this way. |
| Kill an app | "Kill Spotify". Force-quits every process of that app, for when closing is not enough. If the name matches several apps, it asks which one. |
| Window layout | With the GNOME Tiling Shell extension: tile left, right, top, bottom or centre, span tiles, untile, focus the next tile, cycle layouts, open the layout editor. |
| Move windows between monitors | "Move this window to monitor 3", "move it to the right screen". |
| Workspace scenes | "Save a scene called work: terminal on monitor 1 on the left, Chrome on monitor 2, Spotify on monitor 3", then "start my work scene". The apps open one after another, each on its monitor and tile; ones already running are moved there instead. Add or remove an app, list scenes or delete one by voice. Saving over a scene and deleting one can be undone. |
| Minimise, maximise, restore | Name the app ("minimise Spotify", "maximise Chrome") and only that app's window changes, whichever window has focus. Uses Vision's small GNOME window helper (see below). |
| Window shortcuts | Fullscreen, snap left or right, switch window, show desktop, lock the screen, open settings or the file manager. |

### Screen, keyboard and mouse (works on Wayland)

| Feature | What it does |
|---|---|
| Look at the screen | "What's on my screen?" A screenshot goes to Gemini with your question. |
| Look through the webcam | "What am I holding?" A camera frame goes to Gemini; the live camera shows in the window until you close it. |
| Type for you | Types the words you dictate into the focused window, including Greek. It does not press Enter unless you say so. |
| Click by description | "Click the Send button". It finds the element on screen and clicks it, and says when the click missed. |
| Mouse and keys | Click, double-click, right-click, scroll, move the pointer, press keys and shortcuts, copy and paste, repeat a key several times. |
| Several monitors | Works on the largest screen until you say "use the left screen" or "use screen 3". |
| Hands off | "Stop" or "hands off" releases the keyboard, mouse and screen at once. |

On Wayland this goes through GNOME's desktop portal. The first time, GNOME
asks you to share your screens; the orange indicator in the top bar shows while
Vision is using them. Details in [docs/VISION.md](docs/VISION.md#wayland).

### Computer settings

| Feature | What it does |
|---|---|
| Volume | Set it to a level, raise or lower it by an amount, mute, unmute. The level is read back, so "volume to 40" ends at 40. |
| Brightness | Set a level or step it up or down (`brightnessctl`). |
| Media | Play/pause, next, previous, stop and seek in music and video apps without focusing them (`playerctl`). |
| Dark mode, WiFi, display sleep | Switch them by voice. |
| Restart and shut down the PC | Only after you press CONFIRM on the banner. |
| Browser and editing keys | New tab, close tab, next tab, back, forward, reload, zoom, find on page, scroll, undo, redo, save, select all. |

### Web and information

| Feature | What it does |
|---|---|
| Web search | Five modes: search, news, research, price and compare. Gemini's grounded search first, DuckDuckGo as fallback. Results also appear in the panel under the head. |
| Browser control | Open a page, search, click, type, fill forms, read the page, manage tabs. It opens the browser itself. |
| Weather | Spoken, no browser: "What's the weather?", "Will it rain on Friday?", "Forecast for the next 5 days in Thessaloniki". Now, any day up to 16 days ahead, or several days in a row, from Open-Meteo (free, no key). With no city named it uses `weather_city` in `config/api_keys.json`, else the city Vision remembers ("my city is Patras"), else Athens. |
| Greek news | "What's the news?", "Greek news about the economy". Reads a short summary of the latest headlines from ERT, in.gr and Naftemporiki, or Google News Greece for a topic, in Greek or English. No browser. Replace the feeds with a `news_feeds` list in `config/api_keys.json`. |
| Flights | Searches Google Flights and reads out the best options. |
| News watch | "Keep an eye on news about X". Checks once a day and tells you when something new appears. |
| System status | CPU, memory, GPU and temperature, with a spoken alert when something runs hot. |

### Calendar, reminders and routines

| Feature | What it does |
|---|---|
| Google Calendar | "What's on today?", "when is my dentist appointment?", "add lunch with Maria tomorrow at 2", "move number 2 to Friday", "delete the dentist". Reads all your calendars and mentions overlaps. One-time setup in [docs/VISION.md](docs/VISION.md#google-calendar). |
| Phone reminders | "Remind me on my phone an hour before" adds a Google reminder to the event. |
| Timers | "Timer for 10 minutes", "tea timer 4 minutes", "how long is left?", "cancel the tea timer". Several can run at once. When one ends you get a notification, a sound and a spoken sentence, and the countdown shows under the clock. A running timer survives a restart of Vision. |
| Pomodoro | "Start a focus session": 25 minutes of work and 5 of break, four times, announced at each change. Lengths and rounds can be said ("50 minutes work, 10 break, 3 rounds"). "Stop the session" ends it. |
| PC reminders | "Remind me at 6 to call mum". A desktop notification at that time, also said aloud if Vision is running. |
| Routines | Things Vision does on its own at a time you choose: once, on chosen weekdays, monthly, every N minutes between two times, at startup, or after a quiet spell. A routine can say a fixed sentence, carry out an instruction ("tell me the weather and my agenda"), or start a background task. Set them by voice. |
| Opening briefing | Greets you, gives the time, recaps last time and reads the news. It is a routine, so you can change its topic or time. |
| Check-ins | After a quiet spell it may offer something useful. You choose what it may bring up, or turn it off. |
| Quiet hours | "Don't talk to me unprompted between 23:00 and 07:00". Reminders you set for an exact time still ring. |

### Files and documents

| Feature | What it does |
|---|---|
| File management | List, find, read, write, create, copy, move, rename and delete files and folders; find the largest files; show disk usage; tidy the Desktop by type or date. Deleting moves to the Trash and asks first. |
| Undo | "Undo" takes back the last file change (move, rename, create, copy, write, delete, Desktop tidy) or setting change (volume, brightness, dark mode). "What can you undo?" lists them. |
| Work on a dropped file | Drop a file on the window and ask: summarise a PDF or Word file, OCR or resize an image, analyse or filter a CSV or Excel sheet, validate JSON, review or run code, transcribe or trim audio and video, list or extract an archive, summarise a PowerPoint. |
| Wallpaper | Set it from a file or a web image, and ask what it is now. |
| Video and pictures on the HUD | "Play the new Dune trailer" plays it where the face is: a YouTube link, a search, a local file or a URL. Starts muted; pause, seek and unmute by voice. |
| Clipboard panel | Copy some text and a small panel offers Translate, Summarise, Explain or Fix. |

### Background work

| Feature | What it does |
|---|---|
| Background tasks | "Research the best 3D printers for ten minutes." The task runs while you keep talking, is saved after every step, survives a restart, and is announced when done. Results go to `reports/`. Ask how it is going, steer it, finish it early or cancel it. |
| Deep research | The `researcher` agent plans questions, reads sources in full, checks every quote against the saved page and writes a cited report. |
| Agents as files | An agent is a Markdown file in `agents/` (`general.md`, `researcher.md`) listing its tools, time budget and instructions. Background agents get no tool that acts on your PC. |
| Claude Code and Gemini CLI helpers | "Ask Claude to explain the code in ~/vision", "have Gemini compare these laptops". Runs on your own subscriptions in the background and announces the answer. Read-only by default; letting a helper write files in a folder asks first. Details in [docs/VISION.md](docs/VISION.md#helpers-claude-code-and-gemini-cli). |
| Built-in terminals | "Open a Claude terminal", "open a Gemini terminal", or the terminal button at the top of the window. A real terminal opens inside Vision, where the face is, and you type in it: Claude Code, Gemini CLI or a plain shell, with your normal sign-ins. Two can be open side by side. Vision cannot type into them or read them. Details in [docs/VISION.md](docs/VISION.md#built-in-terminals). |
| Coding agent | Builds a new project, or changes an existing folder you drop on the window, with progress on screen. Asks first, because it runs code and may install Python packages. |
| Code helper | Writes, edits, explains or runs a single code file. |
| Steam and Epic games | Lists installed games, updates or installs them, shows download status, and schedules updates for the night. |
| Messages | "Send Anna on WhatsApp: running late". Also Telegram and other apps. Asks first, and says plainly when it could not confirm the message arrived. |
| Reply in the open chat | With a conversation open on screen, in any chat app or web chat: "what did she write?", "draft a reply saying I'll be ten minutes late", "send it". The draft is shown and read to you first. Sending asks first, with the exact words on the banner, and it sends nothing if a different chat is open by then. |
| Spotify by name | "Play Radiohead on Spotify", "play the album OK Computer", "put on Discover Weekly", "what song is this?". It reads back what Spotify is playing before saying it worked. Needs the Spotify desktop app and a one-time setup in [docs/VISION.md](docs/VISION.md#spotify). |

### Memory

| Feature | What it does |
|---|---|
| Long-term memory | Remembers facts about you, your projects and preferences across sessions, and looks up older ones when needed. Nothing is silently forgotten. |
| Memory panel | SETUP → MEMORY shows every stored fact and when it was learned, with a button to delete each one. |
| Session summaries | Each conversation is summarised and mentioned the next time, once. |
| Language memory | Remembers the language you speak in. |

### Models and engines

| Feature | What it does |
|---|---|
| Gemini Live | The default engine for the voice conversation. Needs a Gemini API key (there is a free tier). |
| Model ladder | Every background Gemini call has a deadline and steps to the next model on a quota error, timeout or outage instead of failing. |
| Your own engine | SETUP → ENGINE & PROVIDERS: Whisper for hearing, any OpenAI-compatible model for thinking (Ollama, LM Studio, llama.cpp, vLLM, OpenAI, Groq, DeepSeek, Mistral, xAI, OpenRouter), and Piper, the system voice or ElevenLabs for speaking. Each part has a TEST button. |
| Offline fallback | Point background requests at a local Ollama model so Vision keeps working without quota or internet. |
| Small-model tool budget | A local model is given the most important tools in a shorter form, so it picks the right one more often. |
| Reasoning kept quiet | A model's `<think>` text is never read aloud. |

### The window

| Feature | What it does |
|---|---|
| HUD | Glass panels, CPU / RAM / GPU / network gauges, an activity log, and a TASKS / ROUTINES panel. |
| Face or reactor | An animated head with real lip-sync, blinking and gaze, or a 3-D reactor core. Both drawn in software, no GPU needed. While nothing is happening it redraws less often, and less again while muted, to leave the CPU alone. Asleep, the face shuts its eyes and holds still, and redraws about once a second. |
| Theme | Recolour the whole window from a colour wheel or a hex code. |
| Customise | Change the assistant's name, your name, the voice and the colour (SETUP → CUSTOMISE ASSISTANT). |
| Audio devices | Pick the microphone and speakers by name, test each with a live meter or a tone, and set the output volume (SETUP → AUDIO DEVICES). |
| Phone dashboard | Control Vision from your phone on the same Wi-Fi, paired with a one-time key from SETUP → REMOTE CONTROL. |
| Auto-start and shortcut | Start Vision when you log in, and add a desktop launcher (SETUP). |

### Safety

| Feature | What it does |
|---|---|
| Asks first | Deleting files, sending a message, replying in a chat on screen, starting a coding job, deleting a calendar event or inviting people, and letting a helper write files all wait for you to press CONFIRM on the banner. So do restart, shutdown and turning WiFi off. The banner names exactly what will happen and expires after 90 seconds. |
| Knows your face and voice | Say "learn my face" (look at the webcam for a few seconds) and "learn my voice" (talk or read the text on screen for about 20 seconds). From then on everything behind the CONFIRM banner only goes ahead when Vision recognises you: it checks the voice that gave the command and, if that is not enough, looks through the camera once. Anyone else is told no and nothing happens. Vision also greets you by name when it wakes and sees you. "Forget my face" / "forget my voice" deletes what it learned. Only numbers are stored, on this PC. See [docs/VISION.md](docs/VISION.md#face-and-voice). |
| Closing Vision | "Shut down" or "close Vision" closes Vision itself straight away, after a short goodbye. No click needed. |
| Honest results | Every action reports `done` (checked), `unverified` (sent but not confirmed), `failed`, `ask` or `started`. Vision never says something worked when it could not check. |
| Outside content is data | Web pages, documents and calendar text reach the model marked as data, so an instruction hidden in a page is reported, not followed. |
| Background agents cannot act | Research agents cannot open apps, send messages, change settings or touch files outside their task folder. |

The full list of what asks first is in [docs/VISION.md](docs/VISION.md#ask-first-rule).

---

## Requirements

| | |
|---|---|
| OS | Linux. Built and tested on Ubuntu with GNOME on Wayland. An X11 session may work but is not tested. |
| Python | 3.11 to 3.13 are tested. Newer versions (3.14) run with a warning from `setup.py`. |
| Microphone and speakers | Needed for voice. |
| Gemini API key | Needed for the default engine. A fully local setup needs none. |
| GPU | Not needed. |
| Optional system tools | `pactl` (volume), `brightnessctl` (brightness), `playerctl` (media), `gst-launch-1.0` with the PipeWire plugin (screen on Wayland), GNOME Tiling Shell extension (window tiling). Vision's own GNOME window helper (`gnome-extension/vision-windows@vision`) is installed and enabled by `./vision.sh`; the first time, log out and back in once so GNOME loads it. |

---

## Where things are

```
vision/
├── vision.sh              # One-command start (creates .venv on first run)
├── setup.py               # Installs the Python packages and browsers
├── main.py                # The voice session, audio, and the built-in tools
│                          #   (memory, undo, screen and camera, system status,
│                          #   news watch, shutting Vision down)
├── ui.py                  # VisionUI, the object every tool gets as `player`
├── gui/                   # The window: HUD, panels, drawers, reactor, theme, terminals
├── actions/               # Bundled tools, one file each, found automatically
│   ├── open_app.py              # Open apps, on a chosen monitor
│   ├── computer_control.py      # Type, click, close/kill apps, tile and move windows
│   ├── computer_settings.py     # Volume, brightness, media, power, shortcuts
│   ├── browser_control.py       # Drive the web browser
│   ├── web_search.py            # Search, news, research, price, compare
│   ├── google_calendar.py       # Google Calendar
│   ├── reminder.py              # Reminders on this PC
│   ├── timer.py                 # Countdown timers and pomodoro
│   ├── scenes.py                # Workspace scenes
│   ├── chat_reply.py            # Read and reply in the chat open on screen
│   ├── routines.py              # Routines and quiet hours
│   ├── background_tasks.py      # Background tasks and research
│   ├── cli_helper.py            # Claude Code and Gemini CLI helpers
│   ├── terminal.py              # Open the built-in terminals by voice
│   ├── dev_agent.py             # Coding agent
│   ├── code_helper.py           # Single-file code help
│   ├── file_controller.py       # Files and folders
│   ├── file_processor.py        # Work on a dropped file
│   ├── desktop.py               # Wallpaper and Desktop tidy
│   ├── video_player.py          # Video and pictures on the HUD
│   ├── send_message.py          # WhatsApp, Telegram and others
│   ├── weather_report.py        # Weather, spoken (Open-Meteo)
│   ├── greek_news.py            # Greek news headlines, spoken
│   ├── flight_finder.py         # Google Flights
│   ├── game_updater.py          # Steam and Epic
│   └── screen_processor.py, system_monitor.py, background_monitor.py,
│       proactive.py, youtube_video.py   # helpers used by the above
├── plugins/               # Extra tools, one file each; _template.py shows how
│   └── spotify.py               # Play by name in the Spotify app
├── agents/                # Background agents as Markdown files
├── core/
│   ├── ask_first.py             # Which tool calls wait for CONFIRM
│   ├── confirm.py               # The CONFIRM / CANCEL banner
│   ├── identity.py              # Knows your face and voice; locks the banner to you
│   ├── wayland_desktop.py       # Screen, mouse and keyboard on Wayland
│   ├── wake_word.py             # "Vision" wake word
│   ├── gemini.py                # Every background model call, with fallbacks
│   ├── tasks/                   # Background task engine
│   ├── routines/                # Routine engine
│   ├── timers.py                # Timer engine
│   ├── terminal_session.py      # Shell and screen behind the built-in terminals
│   ├── undo.py, outcome.py, appidentity.py, paths.py, ...
│   └── prompt.txt               # The assistant's instructions
├── providers/             # Optional engines: Whisper, local models, Piper
├── memory/                # Settings access and long-term memory
├── dashboard/             # Phone dashboard
├── config/                # api_keys.json (your keys and settings, not in git)
└── docs/                  # VISION.md, UPSTREAM_HISTORY.md
```

To add your own tool, copy `plugins/_template.py` to `plugins/<name>.py`. It
is picked up on the next start. Read [AGENTS.md](AGENTS.md) first.

---

## Tests

None of these need a microphone, a desktop or a Google account.

```bash
python -m providers._tests                # engines and tool declarations
python -m core._wayland_tests             # Wayland screen, mouse, keyboard, windows
python -m actions._google_calendar_tests  # Google Calendar
python -m actions._cli_helper_tests       # Claude Code and Gemini CLI helpers
python -m actions._features_tests         # Timers, scenes, Spotify, chat reply
python -m actions._weather_news_tests     # Spoken weather and Greek news
python -m actions._terminal_tests         # Built-in terminals
python -m core._sleep_tests               # Going to sleep by voice
python -m core._barge_in_tests            # Interrupting Vision by talking over it
python -m core._sleep_face_tests          # The face shuts its eyes and holds still asleep
python -m core._window_control_tests      # Minimise / maximise the named app only
python -m core._identity_tests            # Face and voice, and the owner lock
python -m core._keepalive_tests           # Keeping the Gemini session open (quiet, GoAway)
```

---

## Your data

Everything stays on your PC except what is sent to the services you use.

| What | Where |
|---|---|
| API keys and settings | `config/api_keys.json`, plain text. Treat it like a password file. |
| What Vision remembers about you | `memory/long_term.json` |
| Routines and quiet hours | `memory/routines.json` |
| Scenes and running timers | `memory/scenes.json`, `memory/timers.json` |
| Background tasks and reports | `memory/tasks/` and `reports/` |
| Google Calendar sign-in | `~/.config/vision/google_calendar_token.json` |
| Helper answers | `~/.vision/helpers/` |
| Your face and voice (numbers only, no photos or audio) | `config/identity/` |

All of these are kept out of git. While a Gemini Live session is open, your
voice is streamed to Google. Screenshots and camera frames go to Gemini when
you ask about them, and so does a picture of the screen when you ask Vision to
read or reply in a chat. A Spotify search sends the name you asked for to
Spotify. With a local engine, nothing leaves the PC.

---

## Credits and licence

The original assistant is [Mark-LVII by FatihMakes](https://github.com/FatihMakes/Mark-LVII).
The face model `core/face_model.obj` is MediaPipe's canonical face model
(Apache 2.0).

Licensed under [Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/):
personal and non-commercial use only.
