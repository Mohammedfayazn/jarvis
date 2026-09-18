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
# bhi Jarvis itne second me band ho jayega
SLEEP_FALLBACK = 15

# Jo tools Jarvis awaaz se chala sakta hai
TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="close_unused_browser_windows",
                description=(
                    "Har khuli browser window band karta hai, siwaye us ek ke "
                    "jo abhi saamne hai aur Jarvis ke apne HUD ke. Tab chalao "
                    "jab user kahe jaise 'bekaar browser band kar do', 'extra "
                    "browser windows close karo'. Dhyan rahe: poori window "
                    "jati hai, uske saare tabs ke saath. Chalane ke baad user "
                    "ko batao ki kaun si windows band ki."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                ),
            ),
            types.FunctionDeclaration(
                name="close_unused_browser_tabs",
                description=(
                    "Kaam wali browser window ke faltu TABS band karta hai - "
                    "kaam wala tab aur Jarvis ki apni screen chhod kar. Tab "
                    "chalao jab user tabs band karne ko kahe: 'faltu tabs "
                    "band karo', 'bas yeh wala tab rakho', 'baaki tabs "
                    "close karo'. Agar user ne bataya kaunsa rakhna hai, "
                    "`keep` me do. Nateeje me `needs_choice` aaye toh kuch "
                    "band nahi hua - `tabs` me se kuch naam padh kar user se "
                    "poochho kaunsa rakhna hai, phir `keep` ke saath dobara "
                    "chalao. User ko nateeje ka `message` batao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "keep": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Jis tab ko rakhna hai uske title ka hissa, "
                                "jaise 'GitHub', 'YouTube', 'Gmail'. Sirf "
                                "tab do jab user ne khud bataya ho."
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
                    "Jarvis ko sula deta hai - baatcheet khatam aur program "
                    "band. Sirf tab chalao jab user saaf taur par JARVIS ko "
                    "rukne ya sone ko kahe: 'Jarvis so jao', 'bas karo "
                    "Jarvis', 'Jarvis stop', 'good night Jarvis'. Gaane ke "
                    "bol ya background awaaz me 'stop' aaye toh MAT chalao. "
                    "Ek chhota sa alvida bolo."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                ),
            ),
        ]
    )
]


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
        elif call.name == "close_unused_browser_tabs":
            keep = (call.args or {}).get("keep")
            # PowerShell + UI Automation - kuch second lagte hain, loop se bahar
            result = await asyncio.to_thread(
                browser_tools.close_unused_browser_tabs, keep
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

    sleep_event = asyncio.Event()
    slept = False

    delay = 1
    try:
        # Connection tutna normal hai - keepalive timeout, wifi, ya server side
        # se. Crash karne ke bajaye dobara connect karein. Band sirf tab, jab
        # aap Jarvis ko sone ko kahein (ya Ctrl+C).
        while True:
            started = loop.time()
            try:
                if await session_once(
                    client, config, mic_queue, spk_queue, playback, bus,
                    sleep_event,
                ):
                    slept = True
                    print("\n\U0001f4a4 Jarvis so gaya. Alvida!")
                    break
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
        bus.state("sleeping" if slept else "offline")
        if slept:
            # Process khatam hone se pehle HUD tak "sleeping" pahunch jaye
            try:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                pass
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
