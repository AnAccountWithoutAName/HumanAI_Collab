"""Local web server (standard library only). Run with: uv run iis-assignment"""

import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .game import ACTIONS, LAYOUTS, Game
from .keys import ROOT, api_key
from .planner import Teammate

PAGE = Path(__file__).parent / "static" / "index.html"
lock = threading.Lock()


def new_game(layout="open", seed=0, mode=None):
    global game, mate
    game = Game(layout, seed)
    log = ROOT / "logs" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    mate = Teammate(game, mode or ("jev" if api_key() else "rules"), lock, log)


def view():
    return {**game.snapshot(), "mate": mate.view()}


new_game()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/state":
            with lock:
                self._json(200, view())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            with lock:
                if self.path == "/new":
                    if body["layout"] not in LAYOUTS:
                        raise ValueError("unknown layout")
                    new_game(body["layout"], int(body["seed"]), mate.mode)
                elif self.path == "/step":
                    first, second = body["actions"]
                    if first not in ACTIONS or (second is not None and second not in ACTIONS):
                        raise ValueError("unknown action")
                    # None means "chef 2 is not human-controlled this tick"
                    game.step([first, mate.act() if second is None else second])
                    mate.tick()
                elif self.path == "/goal":  # None stops chef 2; "direct" skips the verdict (answers to chef 2's own question)
                    if body["goal"] is None or body.get("direct"):
                        mate.instruct(body["goal"])
                    else:
                        mate.propose(body["goal"])
                elif self.path == "/chat":
                    mate.talk.hear(body["text"])
                elif self.path == "/why":
                    mate.talk.why()
                elif self.path == "/make":  # picks one of several open orders
                    mate.make(body["recipe"])
                elif self.path == "/answer":  # an objection, warning or conflict
                    mate.answer(body["reply"])
                elif self.path == "/help":  # chef 2's request
                    mate.talk.answer_request(bool(body["ok"]))
                elif self.path == "/order/cancel":
                    mate.cancel_orders(body["id"])
                elif self.path == "/veto":
                    mate.veto(body["goal"])
                elif self.path == "/mode":
                    mate.set_mode(body["mode"])
                else:
                    return self._json(404, {"error": "not found"})
                self._json(200, view())
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
