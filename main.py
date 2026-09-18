import array
import asyncio
import os
import queue
import sys
import threading

import pyaudio
from dotenv import load_dotenv
from google import genai
from google.genai import types
from websockets.exceptions import ConnectionClosed

import browser_tools
import ui_server
import window_manager
from memory import assistant as memory_assistant
from prompts import instruction

# Windows par stdout cp1252 hota hai, emoji crash karte hain - UTF-8 force karein
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

MODEL = "gemini-3.1-flash-live-preview"

# Audio Settings (Gemini ke requirements ke mutabik)
MIC_RATE   = 16_000   # Mic input rate (Gemini 16kHz standard PCM leta hai)
SPK_RATE   = 24_000   # Speaker output rate (Gemini 24kHz output deta hai)
CHUNK_SIZE = 320      # 20ms audio chunks (16000 * 0.02 = 320 frames)

# Queue limits. Bounded isliye ki agar ek taraf peeche reh jaye toh memory na
# badhe - purana audio drop karna behtar hai, kyunki realtime me woh waise bhi
# ab kisi kaam ka nahi raha.
MIC_QUEUE_MAX = 100   # ~2 second mic audio
SPK_QUEUE_MAX = 400   # Gemini ke chunks bade hote hain

# Reconnect backoff
RECONNECT_MAX_DELAY = 30
STABLE_SECONDS = 30   # Itni der chala toh backoff reset kar do

# HUD ko har 20ms wala level bhejna bekaar hai - har teesra chunk kaafi hai
LEVEL_EVERY = 3

# "So jao" ke baad alvida ka itna intezaar - model turn khatam na kare toh
# bhi Jarvis itne second me so jayega
SLEEP_FALLBACK = 15

# Sone ke baad itne mic chunks (20ms each) anasune - Jarvis ki apni alvida
# ki goonj se wake word na bhade. 50 = 1 second.
WAKE_GRACE_CHUNKS = 50

# Window tools ke saajhe parameters
_WINDOW_NAME = types.Schema(
    type=types.Type.STRING,
    description=(
        "App ya window ka naam jaisa user ne kaha: 'excel', 'outlook', "
        "'visual studio', 'vs code', 'chrome', 'pdf', 'teams', ya title ka "
        "hissa jaise 'budget report'."
    ),
)
_CONFIRM_TOKEN = types.Schema(
    type=types.Type.STRING,
    description=(
        "needs_confirmation wale nateeje ka token - sirf user ke saaf 'haan' "
        "ke baad bhejo."
    ),
)

# Jo tools Jarvis awaaz se chala sakta hai
TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="close_unused_browser_windows",
                description=(
                    "Har khuli browser window band karta hai, siwaye kaam "
                    "wali window aur Jarvis ke apne HUD ke. SIRF tab chalao "
                    "jab user ki abhi wali baat me saaf WINDOWS band karne ko "
                    "kaha ho: 'bekaar browser windows band kar do'. Tabs ki "
                    "baat ho toh close_browser_tabs. Dhyan rahe: poori window "
                    "jati hai, uske saare tabs ke saath. Chalane ke baad user "
                    "ko batao ki kaun si windows band ki."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                ),
            ),
            types.FunctionDeclaration(
                name="close_browser_tabs",
                description=(
                    "Kaam wali browser window ke tabs band karta hai. SIRF tab "
                    "chalao jab user ki abhi wali baat me saaf taur par TABS "
                    "band karne ko kaha ho - kisi aur baat (email, gaana, "
                    "sawaal) ke beech khud se kabhi nahi. Do tareeke, jo user "
                    "ne kaha usi hisaab se: `close` - sirf yeh band ('Gmail "
                    "aur WhatsApp band karo'); `keep` - yeh rakho, baaki band "
                    "('bas GitHub rakho'). Dono ek saath nahi. 'X band karo' "
                    "= close, 'X rakho' = keep - shak ho toh poochho. "
                    "`needs_choice` aaye toh poochho kaunsa rakhna hai. "
                    "`needs_confirmation` aaye toh KUCH BAND NAHI HUA: user "
                    "ko ginti aur kuch naam batao, poochho 'band karun?', aur "
                    "saaf 'haan' mile tabhi wahi close/keep aur "
                    "`confirm_token` ke saath dobara chalao. User ko nateeje "
                    "ka `message` batao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "close": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description=(
                                "Band karne wale tabs ke naam ke hisse, jaise "
                                "['Gmail', 'WhatsApp']. Baaki sab khule rahenge."
                            ),
                        ),
                        "keep": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description=(
                                "Rakhne wale tabs ke naam ke hisse, jaise "
                                "['GitHub']. Baaki sab band honge."
                            ),
                        ),
                        "confirm_token": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "needs_confirmation wale nateeje ka token - "
                                "sirf user ke saaf 'haan' ke baad bhejo."
                            ),
                        ),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="play_on_youtube",
                description=(
                    "YouTube par gaana ya video dhoondh kar browser me chala "
                    "deta hai. Tab chalao jab user kuch bajane ko kahe - "
                    "'Arijit ka tum hi ho laga do', 'koi lofi chala do', "
                    "'YouTube pe ... play karo'. Chalane ke baad user ko "
                    "chhote me batao kya laga diya."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "YouTube search - gaane ka naam aur artist, "
                                "jaise 'tum hi ho arijit singh'. User ki "
                                "baat ka matlab likho, uske poore shabd nahi "
                                "('bhai woh gaana laga do' nahi)."
                            ),
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="go_to_sleep",
                description=(
                    "Jarvis ko sula deta hai - baatcheet ruk jati hai aur "
                    "Jarvis sunna band kar deta hai, jab tak user 'Hey "
                    "Jarvis' bol kar na jagaye. Sirf tab chalao jab user saaf "
                    "taur par JARVIS ko rukne ya sone ko kahe: 'Jarvis so "
                    "jao', 'bas karo Jarvis', 'Jarvis stop', 'good night "
                    "Jarvis'. Gaane ke bol ya background awaaz me 'stop' aaye "
                    "toh MAT chalao. Ek chhota sa alvida bolo."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                ),
            ),
            # --- Window manager (window_manager.py) ---
            types.FunctionDeclaration(
                name="list_open_windows",
                description=(
                    "Computer par khuli saari windows batata hai - app aur "
                    "title. Tab chalao jab user poochhe kya khula hai, ya "
                    "kisi window ka sahi naam jaanna ho."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),
            types.FunctionDeclaration(
                name="close_window",
                description=(
                    "Naam se ek app ki window band karta hai: 'Excel band "
                    "karo', 'close Outlook', 'close visual studio', 'PDF band "
                    "karo'. 'current browser tab' do toh sirf saamne wala "
                    "browser tab band hota hai. SIRF tab chalao jab user ki "
                    "abhi wali baat me saaf band karne ko kaha ho. "
                    "`needs_confirmation` aaye toh KUCH BAND NAHI HUA - "
                    "`message` wala sawaal user se poochho, aur saaf 'haan' "
                    "mile tabhi wahi name aur `confirm_token` ke saath "
                    "dobara chalao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "name": _WINDOW_NAME,
                        "confirm_token": _CONFIRM_TOKEN,
                    },
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="close_all_windows",
                description=(
                    "Ek app ki SAARI windows band: 'saare Chrome band karo', "
                    "'close all Excel windows', 'close all browser windows'. "
                    "Sirf jab user ne 'saari/all' kaha ho. needs_confirmation "
                    "ka niyam close_window jaisa."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "app": types.Schema(
                            type=types.Type.STRING,
                            description="App ka naam: 'chrome', 'excel', 'outlook', 'browser'.",
                        ),
                        "confirm_token": _CONFIRM_TOKEN,
                    },
                    required=["app"],
                ),
            ),
            types.FunctionDeclaration(
                name="close_all_windows_except_current",
                description=(
                    "Current window chhod kar computer ki SAARI windows band. "
                    "Sirf jab user saaf yahi kahe: 'current ke alawa sab band "
                    "karo'. Yeh HAMESHA pehle needs_confirmation deta hai - "
                    "message padh kar sunao (usme likha hai kaunsi window "
                    "bachegi), aur saaf 'haan' ke baad hi confirm_token ke "
                    "saath dobara chalao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"confirm_token": _CONFIRM_TOKEN},
                ),
            ),
            types.FunctionDeclaration(
                name="focus_window",
                description=(
                    "App ki window aage laata hai: 'Outlook pe jao', 'switch "
                    "to VS Code', 'Chrome saamne lao'. Kuch band nahi karta."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"name": _WINDOW_NAME},
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="minimize_window",
                description="App ki window(s) minimize: 'Teams minimize karo'.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"name": _WINDOW_NAME},
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="maximize_window",
                description="App ki window maximize karke aage: 'browser maximize karo'.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"name": _WINDOW_NAME},
                    required=["name"],
                ),
            ),
            # --- Personal memory (memory/) ---
            types.FunctionDeclaration(
                name="remember_this",
                description=(
                    "Ek chhoti si baat hamesha ke liye yaad rakh leta hai - "
                    "sleep aur restart ke baad bhi rehti hai, hamari abhi ki "
                    "baatcheet ki tarah nahi bhoolti. Jab tak main khud 'yaad "
                    "rakho' ya 'remember' na kahoon, kabhi khud se mat "
                    "chalana. Jaise: 'yaad rakho meri beti ka school 8:30 "
                    "baje shuru hota hai', 'remember my wifi password is "
                    "XYZ', 'yaad rakho main Amsterdam me rehta hoon'. Agar "
                    "isi naam se pehle se kuch yaad hai, yeh use update kar "
                    "deta hai - dobara poochhne ki zaroorat nahi. Agar "
                    "baatcheet me tumhe khud koi yaad rakhne layak naya fact "
                    "dikhe (jaise 'mera favorite editor VS Code hai') jo "
                    "maine seedha yaad rakhne ko nahi kaha, pehle poochho "
                    "'yeh yaad rakh loon?' - saaf 'haan' milne ke baad hi "
                    "yeh tool chalao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "category": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Personal, Family, Work, Preferences, "
                                "Projects, ya Reminders me se sabse sahi. "
                                "Pata na ho toh Personal."
                            ),
                        ),
                        "key": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Chhota sa naam jisse baad me yeh baat "
                                "dhoondhi jaye, jaise 'beti ka school', "
                                "'office wifi password', 'favorite editor'."
                            ),
                        ),
                        "value": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Asli baat jo yaad rakhni hai, jaise '8:30' "
                                "ya 'VS Code'."
                            ),
                        ),
                    },
                    required=["key", "value"],
                ),
            ),
            types.FunctionDeclaration(
                name="recall_memory",
                description=(
                    "Pehle yaad rakhi hui baaton me se jawab dhoondhta hai. "
                    "Tab chalao jab main kuch aisa poochhoon jiska jawab "
                    "tumhe yaad rakhi hui kisi baat me mil sakta ho, jaise "
                    "'meri beti ka school kab shuru hota hai?' ya 'mera "
                    "wifi password kya hai?'. `found: false` aaye toh saaf "
                    "kaho 'mujhe yaad nahi', guess mat karo."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Jo poochha gaya uska matlab, jaise 'beti ka "
                                "school time'."
                            ),
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="forget_memory",
                description=(
                    "Ek yaad rakhi hui baat hamesha ke liye hata deta hai. "
                    "Sirf tab chalao jab main saaf kahoon 'yeh baat bhool "
                    "jao' ya 'ise yaad se hata do', naam ke saath."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "key": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Jo naam use hua tha yaad rakhte waqt, jaise "
                                "'office wifi password'."
                            ),
                        ),
                    },
                    required=["key"],
                ),
            ),
            types.FunctionDeclaration(
                name="list_memories",
                description=(
                    "Ab tak yaad rakhi saari (ya ek category ki) baatein "
                    "batata hai. Tab chalao jab main poochhoon 'tumhe mere "
                    "baare me kya kya yaad hai?' ya 'family wali baatein "
                    "batao'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "category": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional: Personal, Family, Work, "
                                "Preferences, Projects, ya Reminders me se "
                                "sirf ek. Khali chhodo toh sab dikhaye."
                            ),
                        ),
                    },
                ),
            ),
        ]
    )
]


def _list_windows_tool(_args):
    windows = window_manager.list_open_windows()
    return {"windows": windows,
            "message": window_manager.summarize_windows(windows)}


# Window tools ka ek hi rasta - sab blocking hain (Win32, COM), sab thread me
WINDOW_TOOLS = {
    "list_open_windows": _list_windows_tool,
    "close_window": lambda a: window_manager.close_window(
        a.get("name", ""), a.get("confirm_token")),
    "close_all_windows": lambda a: window_manager.close_all_windows(
        a.get("app", ""), a.get("confirm_token")),
    "close_all_windows_except_current":
        lambda a: window_manager.close_all_windows_except_current(
            a.get("confirm_token")),
    "focus_window": lambda a: window_manager.focus_window(a.get("name", "")),
    "minimize_window": lambda a: window_manager.minimize_window(a.get("name", "")),
    "maximize_window": lambda a: window_manager.maximize_window(a.get("name", "")),
}

# Personal memory tools - SQLite-backed, survives sleep/restart (memory/)
MEMORY_TOOLS = {
    "remember_this": lambda a: memory_assistant.remember_this(
        a.get("category", ""), a.get("key", ""), a.get("value", "")),
    "recall_memory": lambda a: memory_assistant.recall_memory(a.get("query", "")),
    "forget_memory": lambda a: memory_assistant.forget_memory(a.get("key", "")),
    "list_memories": lambda a: memory_assistant.list_memories(a.get("category") or None),
}


class Playback:
    """Audio threads aur HUD ke beech ka saajha haal.

    `playing` send_loop echo rokne ko padhta hai. `connected` isliye hai ki
    speaker thread reconnect ke beech me galti se "listening" na dikha de.
    """

    def __init__(self):
        self.playing = False
        self.connected = False


def _offer(q, item):
    """Queue full ho toh sabse purana chunk hata kar naya daalein."""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(item)


def mic_worker(loop, stream, mic_queue, bus, stop_event):
    """Ek hi lamba-chalne wala thread mic padhta hai.

    Pehle har 20ms chunk par `asyncio.to_thread` call hota tha - 50 dispatch
    prati second. Ek thread + queue se event loop khali rehta hai, aur HUD ka
    level bhi yahin nikal aata hai (RMS isi thread me, loop par bojh nahi).
    """
    counter = 0

    while not stop_event.is_set():
        try:
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        except Exception:
            break

        counter += 1
        level = None
        if counter % LEVEL_EVERY == 0:
            samples = array.array("h")
            samples.frombytes(data)
            if samples:
                level = max(abs(s) for s in samples) / 32768

        try:
            loop.call_soon_threadsafe(_offer, mic_queue, data)
            if level is not None:
                loop.call_soon_threadsafe(bus.level, level)
        except RuntimeError:
            break   # Event loop band ho chuka hai


def speaker_worker(loop, stream, spk_queue, playback, bus, stop_event):
    """Speaker writes apne thread me - event loop kabhi block nahi hota."""

    def announce(state):
        if playback.connected:
            try:
                loop.call_soon_threadsafe(bus.state, state)
            except RuntimeError:
                pass

    while not stop_event.is_set():
        chunk = spk_queue.get()
        if chunk is None:
            break

        if not playback.playing:
            playback.playing = True
            announce("speaking")

        try:
            stream.write(chunk)
        except Exception:
            break
        finally:
            if spk_queue.empty():
                playback.playing = False
                announce("listening")


async def send_loop(session, mic_queue, playback):
    """Mic queue se audio uthakar Gemini ko bhejta hai."""
    while True:
        data = await mic_queue.get()
        # Agar speaker active hai toh mic data ignore karein (echo rokne ke liye)
        if playback.playing:
            continue
        await session.send_realtime_input(
            audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
        )


async def handle_tool_call(session, tool_call, bus, sleep_event):
    """Jarvis ne koi tool maanga hai - chalao aur nateeja wapas bhejo.

    True lautata hai agar Jarvis ko sone ko kaha gaya - receive_loop phir
    alvida poora hone deta hai aur tab session band karta hai.
    """
    responses = []
    sleep_requested = False

    for call in tool_call.function_calls or []:
        if call.name == "go_to_sleep":
            sleep_requested = True
            result = {"ok": True, "message": "Jarvis ab so jayega."}
            print("\U0001f4a4 Jarvis so raha hai...")
            bus.transcript("system", "Jarvis so raha hai")
            # Model turn_complete na bheje toh bhi atke nahi
            asyncio.get_running_loop().call_later(SLEEP_FALLBACK, sleep_event.set)
        elif call.name == "close_unused_browser_windows":
            # Win32 calls blocking hain - loop ko rokne ki zaroorat nahi
            result = await asyncio.to_thread(
                browser_tools.close_unused_browser_windows
            )
            note = result.get("message") or result.get("error", "")
            print(f"\U0001f9f9 {note}")
            bus.transcript("system", note)
        elif call.name == "close_browser_tabs":
            args = call.args or {}
            # PowerShell + UI Automation - kuch second lagte hain, loop se bahar
            result = await asyncio.to_thread(
                browser_tools.close_browser_tabs,
                args.get("keep"),
                args.get("close"),
                args.get("confirm_token"),
            )
            note = result.get("message") or result.get("error", "")
            print(f"\U0001f5c2️ {note}")
            bus.transcript("system", note)
        elif call.name == "play_on_youtube":
            query = (call.args or {}).get("query", "")
            # Network + browser kholna - dono blocking, loop se bahar
            result = await asyncio.to_thread(browser_tools.play_on_youtube, query)
            note = result.get("message", "YouTube nahi khul paya")
            print(f"\U0001f3b5 {note}")
            bus.transcript("system", note)
        elif call.name in WINDOW_TOOLS:
            result = await asyncio.to_thread(WINDOW_TOOLS[call.name], call.args or {})
            note = result.get("message", "")
            print(f"\U0001fa9f {note}")
            # Model ko poora nateeja jata hai; HUD par ek line kaafi
            bus.transcript("system", note if len(note) <= 160 else note[:157] + "...")
        elif call.name in MEMORY_TOOLS:
            # SQLite hai - blocking, isliye loop se bahar
            result = await asyncio.to_thread(MEMORY_TOOLS[call.name], call.args or {})
            note = result.get("message", "")
            print(f"\U0001f9e0 {note}")
            bus.transcript("system", note if len(note) <= 160 else note[:157] + "...")
        else:
            result = {"error": f"'{call.name}' naam ka koi tool nahi hai."}

        responses.append(
            types.FunctionResponse(id=call.id, name=call.name, response=result)
        )

    if responses:
        await session.send_tool_response(function_responses=responses)

    return sleep_requested


async def wait_for_playback(spk_queue, playback, timeout=SLEEP_FALLBACK):
    """Jo bol raha hai use poora bolne dein - alvida beech me na kate."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (playback.playing or not spk_queue.empty()) and loop.time() < deadline:
        await asyncio.sleep(0.1)
    # stream.write buffer me daal kar lautta hai, speaker tak pahunchne me
    # thoda aur lagta hai
    await asyncio.sleep(0.4)


async def receive_loop(session, spk_queue, playback, bus, sleep_event):
    """Gemini se audio aur transcripts leta hai - har turn, lagataar.

    `session.receive()` sirf EK model turn deta hai aur turn_complete par ruk
    jata hai. Pehle yahan bas ek `async for` tha, toh pehle jawab ke baad
    connection koi padhta hi nahi tha: Jarvis behra ho jata tha. Upar se
    na-padhe messages jama hote-hote websockets ne socket padhna hi band kar
    diya, keepalive pong nahi dikha, aur ~20s baad connection 1011 se tut
    jata tha. Isliye bahar `while True`.
    """
    user_buf, model_buf = [], []
    sleep_pending = False

    while True:
        async for response in session.receive():
            # Tool call alag rasta hai - iske saath server_content nahi aata
            if response.tool_call:
                if await handle_tool_call(
                    session, response.tool_call, bus, sleep_event
                ):
                    sleep_pending = True
                continue

            sc = response.server_content
            if not sc:
                continue

            # Audio model response speaker queue me daalein
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data:
                        try:
                            spk_queue.put_nowait(part.inline_data.data)
                        except queue.Full:
                            # Playback itna peeche hai ki catch-up nahi hoga -
                            # purana chunk chhod dein taaki latency na badhe.
                            try:
                                spk_queue.get_nowait()
                                spk_queue.put_nowait(part.inline_data.data)
                            except (queue.Empty, queue.Full):
                                pass

            # Transcriptions collect karein
            if sc.input_transcription:
                t = sc.input_transcription.text.strip()
                if t:
                    user_buf.append(t)
                    # Aapki baat pakad li - ab jawab ban raha hai. Yahi woh lamha
                    # hai jab user ko rukna chahiye, isliye HUD par alag dikhta hai.
                    if not playback.playing:
                        bus.state("thinking")

            if sc.output_transcription:
                t = sc.output_transcription.text.strip()
                if t: model_buf.append(t)

            # Turn complete hone par console aur HUD dono par bhejein
            if sc.turn_complete:
                if user_buf:
                    text = " ".join(user_buf)
                    print(f"\U0001f464 User: {text}")
                    bus.transcript("user", text)
                    user_buf.clear()
                if model_buf:
                    text = " ".join(model_buf)
                    print(f"\U0001f916 Jarvis: {text}")
                    bus.transcript("jarvis", text)
                    model_buf.clear()

                # Jarvis ko sone ko kaha tha - alvida poora sun lein, phir
                # session band. Aage ka faisla session_once karega.
                if sleep_pending:
                    await wait_for_playback(spk_queue, playback)
                    sleep_event.set()
                    return


def load_wake_word():
    """"Hey Jarvis" sunne wala model - na mile toh Jarvis bina iske chale.

    Pehli baar model download hota hai; tab internet na ho, ya openwakeword
    install hi na ho, toh sone ka matlab purana wala rahega: program band.
    """
    try:
        from wake_word import WakeWordListener
        return WakeWordListener()
    except Exception as exc:   # ImportError, network, model file - sab
        print(
            f"⚠️  'Hey Jarvis' wake word load nahi hua ({exc.__class__.__name__}: "
            f"{exc}). Sone par Jarvis band ho jayega."
        )
        return None


async def wait_for_wake_word(mic_queue, wake):
    """Jarvis so raha hai - sirf "Hey Jarvis" ka intezaar.

    Gemini se connection is dauraan band hai; mic ka audio sirf local model
    tak jata hai. Model har 80ms par ~2ms leta hai, isliye thread ki zaroorat
    nahi - seedha loop par.
    """
    wake.reset()
    # Neend se pehle ka mic audio - Jarvis ka apna alvida bhi - phenk dein,
    # warna usi me "Jarvis" sun kar turant jaag jata. Alvida me aksar "Hey
    # Jarvis bol ke jagana" hota hai, aur speaker ki awaaz mic tak thodi der
    # se pahunchti hai - isliye shuru ka ek second bhi anasuna.
    while not mic_queue.empty():
        mic_queue.get_nowait()
    for _ in range(WAKE_GRACE_CHUNKS):
        await mic_queue.get()

    while True:
        data = await mic_queue.get()
        if wake.heard(data):
            return


async def session_once(
    client, config, mic_queue, spk_queue, playback, bus, sleep_event
):
    """Ek Gemini Live session - connect se lekar disconnect tak.

    True lautata hai agar Jarvis ko sula diya gaya (dobara connect nahi
    karna), warna False - connection gaya, reconnect karo.
    """
    print("\nJarvis (Gemini Live) se connect ho raha hai...")
    bus.state("connecting")

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("Connected! Bolna shuru karein (Ctrl+C se band karein)\n")
        playback.connected = True
        bus.state("listening")

        # Purana audio phenk dein - reconnect ke baad stale chunks bhejne ka
        # matlab hai purani baat dobara sunana.
        while not mic_queue.empty():
            mic_queue.get_nowait()
        while not spk_queue.empty():
            try:
                spk_queue.get_nowait()
            except queue.Empty:
                break

        sleeper = asyncio.create_task(sleep_event.wait())
        tasks = [
            asyncio.create_task(send_loop(session, mic_queue, playback)),
            asyncio.create_task(
                receive_loop(session, spk_queue, playback, bus, sleep_event)
            ),
            sleeper,
        ]
        try:
            # FIRST_COMPLETED, FIRST_EXCEPTION nahi: koi bhi loop ruke - chahe
            # gir kar, chahe chupchaap - session aadha-zinda nahi chhodna.
            # Pehle receive_loop chupchaap khatam ho jata tha aur session
            # bina sune latka rehta tha.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Ek loop ruka toh baaki bhi rokne hain - warna woh mari hui
            # connection par likhte rahenge.
            playback.connected = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if sleep_event.is_set():
            return True

        # Jo pehle gira, uska asli exception yahan dobara uthega
        for task in done:
            if task is not sleeper:
                task.result()
        return False


async def run():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: .env file mein GOOGLE_API_KEY set karein!")
        return

    # PyAudio ko initialize karein aur streams open karein. Streams reconnect
    # ke aar-paar zinda rehte hain - device baar-baar kholna bekaar risk hai.
    p = pyaudio.PyAudio()
    # Windows ka default mic chupke se badal jata hai (headset lagaya, hataya)
    # - aur muted headset par Jarvis ko sirf khamoshi milti hai. Kaunsa mic
    # hai, shuru me hi dikh jaye.
    try:
        print(f"\U0001f3a4 Mic: {p.get_default_input_device_info()['name']}")
    except OSError:
        print("⚠️  Koi mic nahi mila!")
    mic_stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=MIC_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE
    )
    spk_stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SPK_RATE,
        output=True
    )

    loop = asyncio.get_running_loop()
    mic_queue = asyncio.Queue(maxsize=MIC_QUEUE_MAX)
    spk_queue = queue.Queue(maxsize=SPK_QUEUE_MAX)
    playback = Playback()
    stop_event = threading.Event()

    # HUD pehle uthta hai taaki connect hone ka intezaar screen par dikhe
    bus = ui_server.EventBus()
    await ui_server.start(bus)

    mic_thread = threading.Thread(
        target=mic_worker,
        args=(loop, mic_stream, mic_queue, bus, stop_event),
        name="mic",
        daemon=True,
    )
    speaker_thread = threading.Thread(
        target=speaker_worker,
        args=(loop, spk_stream, spk_queue, playback, bus, stop_event),
        name="speaker",
        daemon=True,
    )
    mic_thread.start()
    speaker_thread.start()

    # Gemini client aur configurations
    client = genai.Client(api_key=api_key)
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Laomedeia")
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=instruction)]),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=TOOLS,
    )

    wake = load_wake_word()

    delay = 1
    try:
        # Connection tutna normal hai - keepalive timeout, wifi, ya server side
        # se. Crash karne ke bajaye dobara connect karein. Jarvis so jaye toh
        # "Hey Jarvis" ka intezaar, phir naya session. Band sirf Ctrl+C se.
        while True:
            # Har session ka apna event. go_to_sleep ka 15s wala fallback
            # timer purane event ko pakde rehta hai - agar woh naye session
            # me bajta, toh jagte hi Jarvis phir so jata.
            sleep_event = asyncio.Event()
            started = loop.time()
            try:
                if await session_once(
                    client, config, mic_queue, spk_queue, playback, bus,
                    sleep_event,
                ):
                    if wake is None:
                        print("\n\U0001f4a4 Jarvis so gaya. Alvida!")
                        break

                    print("\n\U0001f4a4 Jarvis so gaya. Jagane ke liye bolo: 'Hey Jarvis'")
                    bus.state("sleeping")
                    await wait_for_wake_word(mic_queue, wake)
                    print("\n\U0001f44b 'Hey Jarvis' suna - jaag raha hoon!")
                    delay = 1
                    continue
                print("\nSession khatam ho gaya.")
            except ConnectionClosed as exc:
                print(f"\nConnection tut gaya: {exc.__class__.__name__}")
            except (TimeoutError, OSError) as exc:
                print(f"\nNetwork problem: {exc.__class__.__name__}: {exc}")

            # Session theek-thaak chala toh backoff reset - baar-baar aane wale
            # drops aur ek lambe session ke baad ke drop me farq hai.
            if loop.time() - started > STABLE_SECONDS:
                delay = 1

            playback.playing = False
            playback.connected = False
            bus.state("reconnecting")

            print(f"{delay}s me dobara connect kar rahe hain... (Ctrl+C se band karein)")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    finally:
        # Cleanup: threads rokein, phir streams aur PyAudio band karein
        print("\nCleaning up audio resources...")
        bus.state("offline")
        stop_event.set()
        try:
            spk_queue.put_nowait(None)   # Speaker thread ko get() se jagayein
        except queue.Full:
            pass

        # Pehle threads ruken, TAB streams band hon. Mic thread `stream.read`
        # ke andar PortAudio me baitha hota hai - us waqt stream close kiya
        # toh woh aazaad ki hui memory padhta hai aur poora process
        # segfault (exit 139) se mar jata hai. read() ~20ms me lautta hai,
        # phir stop_event dekh kar thread nikal jata hai.
        mic_thread.join(timeout=2)
        speaker_thread.join(timeout=2)

        # Koi thread phir bhi atka ho toh uska stream mat chhedo - daemon
        # thread hai, process ke saath khatam ho jayega. Crash se behtar.
        if not mic_thread.is_alive():
            mic_stream.stop_stream()
            mic_stream.close()
        if not speaker_thread.is_alive():
            spk_stream.stop_stream()
            spk_stream.close()
        if not (mic_thread.is_alive() or speaker_thread.is_alive()):
            p.terminate()
        print("Done!")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nJarvis Stopped. Alvida!")
