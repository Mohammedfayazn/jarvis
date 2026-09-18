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

UI_DIR = Path(__file__).parent / "ui"

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765
WS_PORT = 8766


class _QuietHandler(SimpleHTTPRequestHandler):
    """Har GET ko console par likhna transcripts ko doob deta hai."""

    def log_message(self, *args):
        pass


class EventBus:
    """Browser ko bhejne wale events.

    `publish` sync hai kyunki ise wahi event loop se call kiya jata hai jahan
    audio loops chal rahe hain - await karne se woh rasta dheema ho jata.
    """

    def __init__(self):
        self._clients = set()
        # Naya client turant sahi haalat dekhe, agle event ka intezaar na kare.
        self._last_state = {"type": "state", "value": "connecting"}

    async def serve_client(self, websocket):
        self._clients.add(websocket)
        try:
            await websocket.send(json.dumps(self._last_state))
            await websocket.wait_closed()
        finally:
            self._clients.discard(websocket)

    def publish(self, event: dict) -> None:
        if event.get("type") == "state":
            self._last_state = event

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
    print(f"HUD yahan khul raha hai: {url}")
    if open_browser:
        webbrowser.open(url)

    return server
