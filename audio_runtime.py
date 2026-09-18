"""Live-session audio shared by the tutors: playing audio, and recording
one learner utterance (a recitation, a practice word).

AudioPlayer plays MP3s through Windows' built-in MCI player (no extra
dependency) on its own thread, repeating each file as asked, and waits for
Jarvis to finish speaking before it starts. While it plays, `busy` is True
and main.py stops sending the microphone to Gemini, so Jarvis doesn't
"answer" the recording.

UtteranceRecorder captures one utterance from the same mic stream Jarvis
already reads. It starts on the first sound, stops after a pause, and hands
the audio to a callback. The audio is analysed locally and then discarded -
never saved, never sent to Gemini.
"""
from __future__ import annotations

import array
import ctypes
import logging
import math
import queue
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("jarvis.audio")

CHUNK_SECONDS = 0.02          # main.py reads 320 frames at 16 kHz


def _rms(pcm: bytes) -> float:
    samples = array.array("h", pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class AudioPlayer:
    WAIT_FOR_JARVIS = 20.0        # seconds to wait for Jarvis to stop talking

    def __init__(self, is_jarvis_speaking=lambda: False):
        self._is_speaking = is_jarvis_speaking
        self._jobs: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.playing = False
        self._pending = False     # a job taken off the queue, waiting for Jarvis
        self.available = sys.platform == "win32"
        if self.available:
            self._mci = ctypes.windll.winmm.mciSendStringW
            self._mci.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            threading.Thread(target=self._run, name="quran-audio", daemon=True).start()

    @property
    def busy(self) -> bool:
        return self.playing or self._pending or not self._jobs.empty()

    def play(self, files: list[Path], repeat: int = 1, gap: float = 0.6) -> bool:
        if not self.available or not files:
            return False
        self.stop()
        # Let the previous job see the stop before clearing it - otherwise it
        # plays on and the new recitation queues behind it.
        deadline = time.monotonic() + 2.0
        while (self.playing or self._pending) and time.monotonic() < deadline:
            time.sleep(0.02)
        self._stop.clear()
        self._jobs.put((list(files), max(1, min(int(repeat), 10)), gap))
        return True

    def stop(self) -> None:
        self._stop.set()
        while not self._jobs.empty():
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break

    def _cmd(self, command: str) -> str:
        buf = ctypes.create_unicode_buffer(128)
        self._mci(command, buf, 128, None)
        return buf.value

    def _run(self) -> None:
        while True:
            files, repeat, gap = self._jobs.get()
            self._pending = True
            deadline = time.monotonic() + self.WAIT_FOR_JARVIS
            while self._is_speaking() and time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.1)
            self.playing = True
            try:
                for path in files:
                    for _ in range(repeat):
                        if self._stop.is_set():
                            break
                        self._play_one(path)
                        time.sleep(gap)
            finally:
                self.playing = False
                self._pending = False

    def _play_one(self, path: Path) -> None:
        # MCI takes a quoted path; our cache paths never contain quotes.
        self._cmd(f'open "{path}" type mpegvideo alias jarvisq')
        try:
            self._cmd("play jarvisq")
            while not self._stop.is_set():
                if self._cmd("status jarvisq mode") != "playing":
                    break
                time.sleep(0.05)
            self._cmd("stop jarvisq")
        finally:
            self._cmd("close jarvisq")


class UtteranceRecorder:
    """Voice-activity recorder fed 20 ms chunks by main.py's send loop.

    pause: silence that ends the utterance (a recitation needs 3-4 s, since
    people stop to recall the next ayah; a single word ~1.5 s).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.active = False
        self._on_done = None

    def start(self, on_done, *, pause: float, max_seconds: float, no_speech: float = 15.0) -> None:
        with self._lock:
            self.active = True
            self._on_done = on_done
            self._chunks: list[bytes] = []
            self._preroll: list[bytes] = []
            self._noise: list[float] = []
            self._started = False
            self._speech_chunks = 0
            self._silence = 0.0
            self._waited = 0.0
            self._pause = pause
            self._max = max_seconds
            self._no_speech_limit = no_speech

    def cancel(self) -> None:
        with self._lock:
            self.active = False
            self._on_done = None

    def feed(self, pcm: bytes) -> None:
        finished = None
        with self._lock:
            if not self.active:
                return
            level = _rms(pcm)
            # First 300 ms calibrate the room; speech must clearly beat it.
            if len(self._noise) < 15 and not self._started:
                self._noise.append(level)
                return
            floor = sorted(self._noise)[len(self._noise) // 2] if self._noise else 100.0
            threshold = max(350.0, floor * 3.0)
            loud = level > threshold
            if not self._started:
                self._waited += CHUNK_SECONDS
                self._speech_chunks = self._speech_chunks + 1 if loud else 0
                self._preroll = (self._preroll + [pcm])[-15:]
                if self._speech_chunks >= 3:          # 60 ms of sound: begin
                    self._started = True
                    # keep the 300 ms before onset - the first letter of
                    # "qul" is short and would otherwise be clipped
                    self._chunks.extend(self._preroll)
                elif self._waited >= self._no_speech_limit:
                    finished = ("no_speech", b"")
            else:
                self._chunks.append(pcm)
                self._silence = 0.0 if loud else self._silence + CHUNK_SECONDS
                duration = len(self._chunks) * CHUNK_SECONDS
                if self._silence >= self._pause or duration >= self._max:
                    # trim the trailing silence before analysis
                    keep = len(self._chunks) - int(self._silence / CHUNK_SECONDS) + 10
                    finished = ("ok", b"".join(self._chunks[:max(1, keep)]))
            if finished:
                self.active = False
                callback, self._on_done = self._on_done, None
        if finished and callback:
            callback(*finished)
