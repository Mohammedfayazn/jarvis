"""Jarvis ka local HUD - page serve karta hai aur live state bhejta hai.

Audio Python me hi rehta hai (PyAudio wala rasta jo kaam karta hai). Browser
sirf dikhata hai ki abhi ho kya raha hai: sun raha hai, bol raha hai, ya
dobara connect ho raha hai.

Koi nayi dependency nahi - `websockets` pehle se google-genai ke saath aata
hai, aur page stdlib ke http.server se serve hota hai.
"""

import asyncio
import json
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed

UI_DIR = Path(__file__).parent / "ui"

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765
WS_PORT = 8766

# Khule HUD tab ko wapas judne ka kitna waqt dena hai
REUSE_WAIT = 2.5


class _QuietHandler(SimpleHTTPRequestHandler):
    """Har GET ko console par likhna transcripts ko doob deta hai."""

    def log_message(self, *args):
        pass


class EventBus:
    """Browser ko bhejne wale events.

    `publish` sync hai kyunki ise wahi event loop se call kiya jata hai jahan
    audio loops chal rahe hain - await karne se woh rasta dheema ho jata.
    """

    # Browser se aane wale commands - sirf yehi naam maane jaate hain
    COMMANDS = {"stop_audio", "cancel_recitation"}

    def __init__(self):
        self._clients = set()
        # Naya client turant sahi haalat dekhe, agle event ka intezaar na kare.
        self._last_state = {"type": "state", "value": "connecting"}
        # Lesson panel (Quran tutor, speech coach) khula ho toh naya/reconnect
        # hua tab bhi use dekhe
        self._last_lesson = None
        self.on_command = None

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    async def serve_client(self, websocket):
        self._clients.add(websocket)
        try:
            await websocket.send(json.dumps(self._last_state))
            if self._last_lesson:
                await websocket.send(json.dumps(self._last_lesson))
            # HUD ke Stop button jaise commands. Recitation ke dauran mic
            # Gemini tak nahi jata, isliye awaaz se rokna mumkin nahi.
            async for message in websocket:
                self._command(message)
        except ConnectionClosed:
            pass            # tab band hua / sleep - normal hai, traceback nahi
        finally:
            self._clients.discard(websocket)

    def _command(self, message) -> None:
        try:
            name = json.loads(message).get("command")
        except (TypeError, ValueError, AttributeError):
            return
        if name in self.COMMANDS and self.on_command:
            self.on_command(name)

    def publish(self, event: dict) -> None:
        if event.get("type") == "state":
            self._last_state = event
        elif event.get("type") == "panel":
            self._last_lesson = None if event.get("payload", {}).get("mode") == "close" else event

        if not self._clients:
            return

        payload = json.dumps(event)
        for websocket in list(self._clients):
            asyncio.create_task(self._send(websocket, payload))

    async def _send(self, websocket, payload: str) -> None:
        try:
            await websocket.send(payload)
        except Exception:
            # Band ho chuka client - agli baar list me nahi aayega.
            self._clients.discard(websocket)

    # Convenience wrappers - call sites padhne me aasan rehte hain.

    def state(self, value: str) -> None:
        # Wahi state dobara bhejne ka koi matlab nahi - receive_loop har
        # transcript par "thinking" maarta hai, woh sab yahin ruk jate hain.
        if self._last_state.get("value") == value:
            return
        self.publish({"type": "state", "value": value})

    def transcript(self, role: str, text: str) -> None:
        self.publish({"type": "transcript", "role": role, "text": text})

    def level(self, value: float) -> None:
        self.publish({"type": "level", "value": value})


def _serve_page() -> None:
    """Static page apne daemon thread me - http.server blocking hai."""
    handler = partial(_QuietHandler, directory=str(UI_DIR))
    httpd = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), handler)
    httpd.serve_forever()


async def start(bus: EventBus, open_browser: bool = True):
    """HUD chalu karein. Wahi event loop use hota hai jo audio chalata hai."""
    threading.Thread(target=_serve_page, name="ui-http", daemon=True).start()

    server = await websockets.serve(bus.serve_client, HTTP_HOST, WS_PORT)

    url = f"http://{HTTP_HOST}:{HTTP_PORT}/"

    # Pehle se khula HUD tab khud wapas jud jata hai (page har 1.2s try karta
    # hai). Usko thoda waqt dein - warna har restart par ek naya tab khulta
    # jayega aur purane bekaar pade rahenge.
    if open_browser:
        for _ in range(int(REUSE_WAIT / 0.2)):
            if bus.has_clients:
                break
            await asyncio.sleep(0.2)

    if bus.has_clients:
        print(f"HUD pehle se khula hai: {url}")
    else:
        print(f"HUD yahan khul raha hai: {url}")
        if open_browser:
            webbrowser.open(url)

    return server
