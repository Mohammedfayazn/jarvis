# Jarvis AI Voice Assistant

A real-time AI voice assistant built with Python and Google Gemini Live API.

## Features

* 🎤 Real-time microphone input
* 🤖 Google Gemini Live integration
* 🔊 AI voice responses
* 📝 Input & output transcription
* ⚡ Async architecture for low latency
* 🖥️ Live HUD in the browser: listening / thinking / speaking / sleeping
* 💤 "Jarvis so jao" to sleep, "Hey Jarvis" to wake - offline wake word
* 🪟 Window control: switch, minimize, maximize and close apps by voice
* 🌐 Browser control: YouTube playback, closing tabs and windows
* 🧠 Personal memory: remembers facts you tell it, survives sleep and restart

## Installation

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```env
GOOGLE_API_KEY=your_api_key_here
```

Window control needs Windows (it uses the Win32 API).

## Run

```bash
python main.py
```

## Tech Stack

* Python
* Google Gemini Live API
* PyAudio
* AsyncIO
* openWakeWord (offline "Hey Jarvis")
* pywin32, pygetwindow, pyautogui, psutil (window control)

## Architecture

```
 mic ──> mic thread ──> mic_queue ──┬─> send_loop ──> Gemini Live ──> receive_loop
                                    │                                  │   │
                                    └─> wake word (while asleep)       │   └─> tool calls
                                                                       v
 speaker <── speaker thread <── spk_queue <────────────────────────────┘
                                                                       │
 HUD (browser) <── WebSocket <── EventBus <── state / transcripts ─────┘
```

| File | Role |
|---|---|
| `main.py` | Audio threads, Gemini Live session, reconnects, sleep/wake, tool dispatch |
| `prompts.py` | Jarvis's system instruction - persona, tools, honesty rules |
| `ui_server.py`, `ui/` | Local HUD page and the WebSocket that drives it |
| `wake_word.py` | Offline "Hey Jarvis" detector used while Jarvis sleeps |
| `browser_tools.py`, `browser_tabs.ps1` | YouTube, browser windows, tabs (via UI Automation) |
| `window_manager.py` | Controlling any app's windows |
| `memory/` | Personal memory - SQLite-backed facts, survives sleep/restart |

Nothing leaves the machine while Jarvis sleeps: the Gemini connection is
closed and only the local wake-word model hears the mic.

### Window manager

`window_manager.py` works standalone too:

```bash
python window_manager.py list
python window_manager.py "close excel"
python window_manager.py "switch to vscode"
python window_manager.py "minimize teams"
python window_manager.py "close all windows except current"
```

A name is resolved in two steps: an alias table first (so "visual studio"
means `devenv.exe` and "vs code" means `Code.exe` - different apps), then
fuzzy matching on window titles ("budget" finds `Budget 2026.xlsx - Excel`).
Only windows that appear in Alt+Tab are considered.

Closing is deliberately careful:

* Windows are closed with `WM_CLOSE` - the same as clicking X - never
  killed, so apps like Excel still get to ask "Save changes?".
* Before closing, it asks for confirmation when several windows match or
  unsaved work is detected (title markers like `*` / `●`, and Office's own
  `Saved` flag over COM). "Close all except current" always asks, and says
  which window it will keep.
* A confirmation carries a token derived from the exact windows that will
  close; Jarvis can only get it by asking you, and it stops working if the
  windows change in between.
* It never closes the window Jarvis runs in (terminal, VS Code), the HUD,
  or Windows' own shell.
* It reports what actually happened: a window stuck on a save prompt is
  reported as still open, not as closed.

### Personal memory

`memory/` is a small clean-architecture module: `db.py` (raw SQLite),
`models.py` (the `Memory` shape and the fixed categories - Personal,
Family, Work, Preferences, Projects, Reminders), `embeddings.py` (optional
semantic search via sentence-transformers), `service.py`
(`MemoryManager` - fuzzy + semantic recall), `intent.py` (rule-based
"remember ..." / question parsing, used outside the live Gemini session),
and `assistant.py` (the dict-in/dict-out functions wired into `main.py` as
`remember_this` / `recall_memory` / `forget_memory` / `list_memories`).

Facts persist in `memory/data/jarvis_memory.db` (gitignored) across sleep
*and* restarts - unlike the conversation itself, which resets every time
Jarvis wakes up. Recall works even with a typo or a rephrased question
("what time does my daughter's school start?" finds a fact stored as
"daughter's school: 8:30"), first by fuzzy key/keyword matching, then by
semantic similarity if `sentence-transformers` is installed - it is an
optional, heavy dependency, and everything still works without it.

Jarvis only stores something you explicitly asked it to remember, or asks
first ("yeh yaad rakh loon?") if it notices a fact you mentioned in
passing - it never saves anything silently.

```bash
python -m memory.init_db      # create/verify the database file
python -m memory.examples     # runnable walkthrough, uses a throwaway db
```

## Tests

```bash
python -m unittest discover tests
```

## Demo

Speak into your microphone and receive real-time voice responses from Gemini.

## License

MIT License
