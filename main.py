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


async def receive_loop(session, spk_queue, bus):
    """Gemini se audio aur transcripts leta hai."""
    user_buf, model_buf = [], []
    async for response in session.receive():
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
            if t: user_buf.append(t)

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


async def session_once(client, config, mic_queue, spk_queue, playback, bus):
    """Ek Gemini Live session - connect se lekar disconnect tak."""
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

        tasks = [
            asyncio.create_task(send_loop(session, mic_queue, playback)),
            asyncio.create_task(receive_loop(session, spk_queue, bus)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            # Ek loop gira toh doosre ko bhi rokna hai - warna woh mari hui
            # connection par likhta rahega.
            playback.connected = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # Jo pehle gira, uska asli exception yahan dobara uthega
        for task in done:
            task.result()


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
    )

    delay = 1
    try:
        # Connection tutna normal hai - keepalive timeout, wifi, ya server side
        # se. Crash karne ke bajaye dobara connect karein.
        while True:
            started = loop.time()
            try:
                await session_once(
                    client, config, mic_queue, spk_queue, playback, bus
                )
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
        speaker_thread.join(timeout=2)

        mic_stream.stop_stream()
        mic_stream.close()
        spk_stream.stop_stream()
        spk_stream.close()
        p.terminate()
        print("Done!")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nJarvis Stopped. Alvida!")
