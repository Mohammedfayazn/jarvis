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
* 🛠️ Project co-pilot: opens projects in VS Code, tracks tasks, remembers
  decisions, reads git history and code, suggests what to do next
* 🕌 Islamic tutor: Quran lessons for the family, recitation playback,
  memorisation tests with pronunciation feedback, sourced answers
* 🧸 Child speech coach: playful English/Dutch/Hindi practice for a young
  child - words, say-it-with-me, conversation, picture stories
* 💬 WhatsApp: reads out unread / today's / recent messages and sends
  dictated replies (English, Hindi, Hinglish, emoji), always asking first
* 🧑‍🤝‍🧑 User profiles: "speak to my daughter" switches Jarvis to Child Mode -
  short slow sentences, long pauses, and only the child's tools
* 🖥️ System controls: volume, screen brightness, folders and files, battery,
  media keys, clipboard, and power - with the risky ones asking first

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

## Run as a Windows app (no console, starts at logon)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1               # build dist\Jarvis\Jarvis.exe + self-test
powershell -ExecutionPolicy Bypass -File scripts\create_desktop_shortcut.ps1 # Desktop shortcut
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1         # start at logon (-Remove to undo)
powershell -ExecutionPolicy Bypass -File scripts\stop_jarvis.ps1             # stop the background app
```

* It's a **folder** build (`dist\Jarvis\Jarvis.exe` plus its files), not a
  single exe: the speech models need PyTorch (~570 MB), which a one-file exe
  would unpack to a temp folder on every start.
* **At logon Jarvis starts asleep**: only the offline "Hey Jarvis" detector
  listens, nothing goes to Google until you say it, and no browser tab
  opens. The HUD is at http://127.0.0.1:8765/ whenever you want it; the
  Desktop shortcut opens it (and never starts a second Jarvis).
* **Data and settings** live in `%LOCALAPPDATA%\Jarvis\`: the `.env` with
  your API key, all databases, and `jarvis.log` (where the windowless app
  writes what the console used to show). `python main.py` uses the same
  folder, so both see the same memories and progress. Older data in
  `memory/data` etc. is copied there once; the originals are left alone.
* `Jarvis.exe --self-test` checks a build without starting a session.
* Rebuilding stops a running Jarvis.exe first (it locks its own files).

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
| `projects/` | Project co-pilot - project memory, tasks, git, code insights |
| `islamic/` | Islamic tutor - Quran lessons, recitation checking, family progress |
| `speech_coach/` | Child speech coach - vocabulary, pronunciation, sentences, stories |
| `audio_runtime.py` | Audio player + utterance recorder shared by both tutors |

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

### Project co-pilot

`projects/` turns Jarvis into an engineering co-pilot. Say "open Homemade
project" and it finds the folder, opens it in VS Code, indexes the README,
docs and manifests, reads git, and tells you when you last worked on it,
what the recent commits were about, your open tasks and a suggested next
step. Everything it says comes from four sources: its stored memory, git,
your docs, and your own end-of-day summaries.

```
intent.py        IntentRouter          text command -> use case (CLI, tests)
assistant.py     ProjectAssistant      use cases; what main.py's tools call
recommend.py     RecommendationEngine  ranked next actions, each with a reason
brain.py         ProjectBrain          per-project memory; tasks.py TaskManager
git_analyzer.py  GitAnalyzer           read-only git: log, status, diff, branches
indexer.py       ProjectIndexer        README/docs/manifests/layout -> summary
code_insights.py CodeInsights          search, TODOs, read file, modules, health
locator.py       ProjectLocator        spoken name -> folder
vscode.py        VSCodeController
models.py, db.py entities and SQLite schema
```

Personal facts stay in `memory/` (its MemoryManager); `projects/` remembers
work. Schema (`projects/data/projects.db`, gitignored):

| Table | Holds |
|---|---|
| `projects` | name, path, description, current status, docs index, last opened / worked on |
| `tasks` | title, description, status (Todo / In Progress / Blocked / Done), priority, timestamps |
| `entries` | goals, decisions, notes, blockers, next steps, recurring problems, coding preferences (project-less = applies to all projects) |
| `work_sessions` | start/end, what you accomplished, what comes next |

Voice examples: "open Homemade project and resume work", "what was I
working on?", "what changed this week?", "what's blocking progress?",
"what should I work on next?", "daily project summary", "explain the
project architecture", "find TODO comments", "suggest missing tests", "add
task: kitchen availability screen", "we decided to use Supabase for auth".
When you say you're done for the day, Jarvis asks what you accomplished
and what's next, and stores both.

Projects are found in `~/dev`, `~/Documents`, `~/source/repos`,
`~/projects`, `~/code`, `~/repos`, `~/src`, `~/Desktop` and `~/workspace`
(two levels deep), or in the folders listed in `JARVIS_PROJECT_ROOTS`.

Safety: git is only ever read, never changed. File reads stay inside the
project folder, and secret-looking files (`.env`, keys, keystores,
credentials) are never read or searched - whatever is read goes to the
Gemini API.

The same brain works without voice:

```bash
python -m projects "open homemade project"
python -m projects --no-editor "what should i work on next"
python -m projects                    # interactive
python -m projects.init_db            # create/verify the database
```

Not built yet (candidates for later): recommendations are rule-based,
not LLM-generated; code explanations only cover what one file read
returns; projects open in VS Code only (not Visual Studio).

### Islamic tutor

`islamic/` makes Jarvis a Quran learning companion for the whole family:
Arabic letters, basic tajweed, verse-by-verse memorisation, duas and
manners, sized for a 4-6 year old or for an adult, with each learner's
progress kept separately.

**Scripture is never generated.** Quran text is the Tanzil-verified Uthmani
text (via api.alquran.cloud) with the Sahih International translation;
hadith come from the fawazahmed0/hadith-api dataset with their grading;
recitation audio is Mishary Alafasy's. Everything is cached locally after
the first fetch. Jarvis's system prompt forbids quoting Quran or hadith
from its own memory, and tells it to keep Quran, hadith, scholarly
explanation and differing opinions visibly separate, and to say "I'm not
certain" rather than guess. If a source can't be reached and isn't cached,
the tutor says so instead of teaching from memory.

```
knowledge.py  IslamicKnowledgeBase  labelled Quran/hadith lookups + answer policy
teacher.py    QuranTeacher          alphabet, tajweed, surah, dua, manners lessons
child.py      ChildLearningMode     3-option voice quizzes, praise, never "wrong"
recitation.py RecitationAnalyzer    local speech model -> word/letter comparison
tracker.py    LearningTracker       profiles, progress, spaced repetition, weak areas
planner.py    LessonPlanner         daily plan: Quran, dua, manners, revision
assistant.py  IslamicTutor          use cases; what main.py's tools call
runtime.py    AudioPlayer, RecitationRecorder (live session)
sources.py    verified sources + cache;  curriculum.py  what is taught
```

Lessons appear in a panel on the HUD (in the Amiri Quran font) and the
recitation plays by itself; while it plays, and while someone is
reciting, the microphone is not sent to Gemini, so use the panel's Stop /
Cancel buttons to interrupt.

**Recitation checking** runs a Quran-trained Whisper model
(`tarteel-ai/whisper-base-ar-quran`) locally - the recording never leaves
the computer and is not saved. It reports missing and extra words and
letters dropped or swapped (e.g. ه for ح, with how to tell them apart).
It does **not** judge tajweed (elongation, ghunnah, qalqalah) and says so;
a teacher should still listen. If it can barely hear anything it asks for
another try rather than marking a child wrong. Needs `torch` and
`transformers` (optional; everything else works without them).

Voice examples: "add Zunaira, she's 4", "teach Quran", "start Zunaira's
lesson", "continue yesterday's lesson", "test Surah Al-Ikhlas", "quiz
time", "play Surah Al-Mulk", "daily Islamic lesson", "how is Zunaira
doing?", "what does the Quran say about parents?".

### Child speech coach

`speech_coach/` is a friendly practice companion for a young child growing
up with English, Dutch and Hindi. **It is not a medical or diagnostic
tool**: it never labels, never compares with other children, and the
parent report ends by pointing to a speech therapist (logopedist) for any
concern.

```
coach.py          SpeechCoach            10-minute daily session + use cases
conversation.py   ConversationEngine     starters, follow-ups, sentence length, language mix
pronunciation.py  PronunciationAnalyzer  local Whisper + gentle word comparison
vocabulary.py     VocabularyTrainer      48 picture words x 3 languages (Dutch with de/het)
storytelling.py   StorytellingModule     emoji picture scenes, built up to full sentences
tracker.py        ProgressTracker        words, sounds, sentences, sessions (local SQLite)
dashboard.py      ParentDashboard        weekly summary with ideas for the week
```

The daily session is greeting (1 min) -> new words (2) -> say-it-with-me
(2) -> conversation (2) -> picture story (2) -> stars (1). Jarvis never
says "wrong": every result starts with praise or "good try". Short
sentences are echoed back bigger ("Dog running!" -> "The dog is running in
the park!"), and mixing languages is treated as normal.

**Pronunciation, honestly.** A local `openai/whisper-base` model hears the
child's attempt (audio stays on the computer, is never saved). Tested on
synthesised words, it passes through substitutions like "wabbit" (r -> w)
and dropped syllables ("nana" for banana), but it silently "corrects"
mispronunciations that sound like another real word ("thun" -> "sun"), and
young children are harder for it than adults. So one miss is never
counted against a sound: unclear audio is ignored, and a sound only
appears as "keep playing with" after repeated attempts.

Conversation practice itself goes through Gemini like every Jarvis
conversation; what the child said is stored as text, locally, for the
parent dashboard. **The Dutch and Hindi word lists and story sentences were
written for this module - please check them against your family's usage**
(they're plain data in `vocabulary.py` / `storytelling.py` /
`conversation.py`).

Voice examples: "add Zunaira, she's 4, Dutch English and Hindi, mostly
Dutch", "start Zunaira's speech practice", "next", "teach her animal
words in Dutch", "practise the word rabbit", "story time", "how is
Zunaira doing this week?".

### System controls

`system_control.py` is Jarvis's hands on the machine itself - all local,
nothing over the network.

```
SystemController
  volume_status / set_volume / change_volume / mute      pycaw
  brightness_status / set_brightness / change_brightness screen-brightness-control
  create_folder / list_folder / move / delete            pathlib + shutil + send2trash
  power("shutdown"|"restart"|"lock"|"sleep"|             shutdown.exe, rundll32
        "hibernate"|"cancel")
  media("playpause"|"next"|"previous")                   pyautogui media keys
  battery / running_apps / open_app / force_close_app    psutil
  clipboard_read / clipboard_write                       pyperclip
  status()                                               battery + volume + brightness
```

Voice: "awaaz kam karo", "volume 30 par rakho", "screen thodi kam karo",
"D drive par Jarvis_Logs folder banao", "us folder me kya hai?", "battery
kitni hai?", "gaana rok do", "notepad kholo", "computer lock karo",
"shutdown karo" (then "cancel shutdown" while it counts down).

**What it refuses.** Windows's own folders and Program Files, a whole
drive, your main folders, and Jarvis's own data and code - the places a
misheard word could not be undone. It won't kill the processes Windows
needs, or the Jarvis process it runs in.

**What it asks about first.** Deleting, shutting down, restarting,
hibernating and force-closing an app return `needs_confirmation` with a
`confirm_token`. Jarvis reads out what would happen and only calls again
with that token after you clearly say yes. A token is tied to that exact
action, so a token for one file can't delete another, and a "restart in 60"
token can't shut the machine down.

**Deleting goes to the Recycle Bin** (send2trash). Without that library
Jarvis says so and deletes nothing, rather than removing a file for good.

**Child Mode has none of this** (see the profiles section): the tools are
not in the child's session at all.

```bash
python system_control.py system_status
python system_control.py control_volume action=set percent=30
python examples/system_intents_demo.py --run   # spoken line -> which tool
```

Volume goes through the default playback device, so it follows your
headset. Brightness needs a monitor that answers DDC/CI: a laptop screen
always does, an external monitor usually needs it enabled in its own menu,
and one that refuses is named in the answer instead of failing the call.

### User profiles - Adult Mode and Child Mode

Jarvis talks to two very different people. `mode_manager.py` keeps one
`Profile` for each and rebuilds the session when it changes.

```
                      Adult Mode (Fayaz)     Child Mode (Zunaira, 4)
system prompt         ADULT_SYSTEM_PROMPT    CHILD_SYSTEM_PROMPT
speaking pace         ~180 wpm               ~120 wpm, 1-2 short sentences
end-of-turn silence   1.2 s                  4.0 s
recorder pause floor  1.5 s                  4.0 s
tools in the session  all 43                 13: coach + Quran + the way back
```

Say **"Jarvis, speak to my daughter"**, "Zunaira se baat karo" or "kids
mode" to switch; **"Jarvis, back to me"**, "mujhse baat karo" or "adult
mode" to switch back. The model calls the `set_mode` tool, and main.py
also matches the spoken phrase itself (`mode_manager.detect`), so a clear
phrase still works when the model misses the tool. Switching to the mode
already active does nothing, so the two paths never fight.

A switch reconnects the Live session with the new profile and **starts a
fresh conversation** - the adult talk isn't in the child's context - and
Jarvis opens with a greeting for whoever it is now talking to. Reconnecting
is necessary: the pause threshold and the tool list are fixed when a
session connects. It costs a second or two.

**The pause threshold is what matters most with a 4-year-old.** She stops
mid-sentence to think, and at the adult's 1.2 s Jarvis talks over her. In
Child Mode the Live API's `silence_duration_ms` is 4 s, end-of-speech
sensitivity is LOW, and word practice (`UtteranceRecorder`) never uses a
pause shorter than 4 s either.

**Child Mode is also a safety boundary**, not only a persona: the WhatsApp,
window, project and memory tools are not in the session at all, so nothing
she says can send a message or close a window. Tested: asked to send a
WhatsApp in Child Mode, Jarvis simply answers that it can't.

**Speaking rate is guidance, not a setting.** The Live API has no
speaking-rate parameter (`SpeechConfig` is voice and language), and the
audio arrives as finished speech. The "words per minute" in each profile
goes into the prompt - the model follows it loosely. Making it exact would
mean time-stretching the audio in the playback path, which isn't done here.

```bash
python examples/mode_switch_demo.py     # what each spoken line does, no API
```

### WhatsApp

`whatsapp_handler.py` drives WhatsApp Web with Playwright in a headless
Chrome that has its own profile (`%LOCALAPPDATA%\Jarvis\whatsapp\profile`),
so the QR code is scanned once and never again.

**One-time setup:** say **"WhatsApp link karo"**. Jarvis opens a Chrome
window with a QR code; scan it with WhatsApp on your phone (Settings >
Linked devices > Link a device). The window closes by itself, and from
then on Jarvis opens WhatsApp Web in the background whenever it starts.
It uses the Chrome (or Edge) already installed, so `playwright install`
isn't needed.

WhatsApp Web open in your own browser doesn't count: Jarvis has its own
linked session, separate from your browser's.

`python whatsapp_handler.py login` does the same from a terminal (stop
Jarvis first - a profile can only be open in one browser at a time).

**Troubleshooting: "WhatsApp isn't linked" although you linked it.** A
terminal opened from a packaged Windows app - the terminal inside the
Claude desktop app, for one - doesn't write to the real
`%LOCALAPPDATA%`: Windows silently redirects its writes to
`%LOCALAPPDATA%\Packages\<app>\LocalCache`. The login lands there, that
terminal happily reads it back, and the Jarvis you start from the Desktop
never sees it. `login` now detects this (`app_paths.redirected_home`) and
refuses; link by voice instead, or from Start menu > Terminal.

```
WhatsAppManager
  send_message(recipient, text)          contact name or +country-code number
  get_unread_messages(filter_mode)       "unread" | "today" | "recent" (hours=N)
  summarize_and_prompt_reply(mode)       spoken summary + the message list
voice tools (main.py)
  check_whatsapp                         -> summarize_and_prompt_reply
  send_whatsapp                          -> find chat, confirm, send_message
  link_whatsapp                          -> start_login (QR window, returns at once)
```

* **Reading never opens a chat**, so nothing is marked as read. It sees the
  chat list: the latest message of each chat, its time and unread count.
* **Sending always asks first.** The first `send_whatsapp` call only finds
  the chat and returns a `confirm_token`; Jarvis reads back the name and
  the exact text, and sends after you say yes. The compose box is read
  back before sending - if the text arrived changed (mangled Devanagari,
  lost characters), nothing is sent.
* Incoming messages are treated as text to read out, never as
  instructions to Jarvis.
* WhatsApp Web refuses headless Chrome's user agent ("database error ...
  relink your device"); the handler uses the normal Chrome user agent.
* Try it without Jarvis: `python whatsapp_handler.py status`,
  `read today`, `read recent --hours 3`, `send "Rahul" "Main aa raha hoon 👍"`
  (add `--show` to watch the browser).

WhatsApp changes its web page often. Each element has several fallback
selectors, but if reading or sending stops working after a WhatsApp
update, the selectors at the top of `whatsapp_handler.py` are the place
to look.

Voice examples: "WhatsApp pe kuch aaya?", "aaj ke WhatsApp messages
batao", "pichhle do ghante me kisne message kiya?", "Rahul ko WhatsApp
karo ke main 5 baje aaunga", "haan, Priya ko reply karo: draft dekh liya".

## Tests

```bash
python -m unittest discover tests
```

## Demo

Speak into your microphone and receive real-time voice responses from Gemini.

## License

MIT License
