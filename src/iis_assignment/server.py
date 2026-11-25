"""Local web server (standard library only). Run with: uv run iis-assignment"""

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .game import ACTIONS, LAYOUTS, Game

PAGE = Path(__file__).parent / "static" / "index.html"
lock = threading.Lock()
game = Game()


def partner_action(game):
    """Seam for the AI teammate (chef 2). Planned: it idles for now."""
    return "stay"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/state":
            with lock:
                self._json(200, game.snapshot())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        global game
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            with lock:
                if self.path == "/new":
                    if body["layout"] not in LAYOUTS:
                        raise ValueError("unknown layout")
                    game = Game(body["layout"], int(body["seed"]))
                elif self.path == "/step":
                    first, second = body["actions"]
                    if first not in ACTIONS or (second is not None and second not in ACTIONS):
                        raise ValueError("unknown action")
                    # None means "chef 2 is not human-controlled this tick"
                    game.step([first, partner_action(game) if second is None else second])
                else:
                    return self._json(404, {"error": "not found"})
                self._json(200, game.snapshot())
        except (ValueError, KeyError, TypeError) as error:
            self._json(400, {"error": str(error)})

    def _json(self, status, data):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main():
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8000)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Playing at {url}  (Ctrl+C to stop)", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
