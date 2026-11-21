"""The Ask page, served on your own computer (standard library only).

    python rag_server.py              # then open http://127.0.0.1:8765
    python rag_server.py --no-ai      # show the matching playbook text only, never call Groq
    python rag_server.py --port 9000 --open

Your Groq key stays on this computer: it is read from the GROQ_API_KEY environment variable of this process and is never
sent to the browser, never written to a file and never logged. Without the key the page still works and shows the matching
playbook text instead of a written answer.

Each question writes one line to stderr and nothing else is logged:
    [ask] kind=<kind> reason=<code> len=<characters> id=<first 8 hex of the SHA-256 of the question>
It never holds the question text or any key. The reason is empty for a normal answer.

The server listens on 127.0.0.1 only (not on your network). It accepts a request only when the Host header is this address,
a browser request only from this page's own origin, and only JSON, so another web page cannot make your browser call it.
"""
import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent
import rag

HOST = "127.0.0.1"
PORT = 8765
UI_PATH = Path(__file__).with_name("rag_ui.html")
REPORT_PATH = Path("executive-report.html")     # the daily brief, when it is in the folder the server was started from
MAX_BODY = 4096
RATE_LIMIT, RATE_WINDOW = 20, 60.0               # questions per minute: protects the free Groq quota
PAGE_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
REPORT_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.plot.ly; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
              "font-src https://fonts.gstatic.com; connect-src 'none'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "AskThePlaybook"
    sys_version = ""

    # ---- helpers
    def _origins(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, status, body, content_type="application/json; charset=utf-8", csp=PAGE_CSP):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _host_ok(self):
        if self.headers.get("Host", "") not in self._origins():
            self._json(403, {"error": "This server only answers requests for its own address."})
            return False
        return True

    def log_message(self, fmt, *args):      # no request lines: they could hold a question
        return

    # ---- routes
    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/sections":
            self._json(200, {"topics": rag.topics(self.server.index), "reference": rag.reference(), "ai": self.server.ai,
                             "model": agent.MODEL if self.server.ai else None, "report": REPORT_PATH.is_file()})
        elif path == "/report" and REPORT_PATH.is_file():
            self._send(200, REPORT_PATH.read_bytes(), "text/html; charset=utf-8", REPORT_CSP)
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self):
        if not self._host_ok():
            return
        if self.path.split("?", 1)[0] != "/api/ask":
            self._json(404, {"error": "Not found."})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.replace("http://", "", 1) not in self._origins():
            self._json(403, {"error": "Requests from other pages are not accepted."})
            return
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._json(415, {"error": "Send JSON."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": "The question is too long."})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            question = body.get("question") if isinstance(body, dict) else None
        except ValueError:
            question = None
        if not isinstance(question, str):
            self._json(400, {"error": "Send {\"question\": \"...\"}."})
            return
        if not self.server.allow():
            self._json(429, {"error": "Too many questions in a minute. Wait a little and try again."})
            return
        try:
            answer = rag.ask(question, self.server.index, llm=self.server.llm, ai=self.server.ai)
        except Exception:       # never show internals or the key in a reply, or in the log
            rag.log_ask({"question": rag.clean_question(question), "kind": "error", "reason": "server_error"})
            self._json(500, {"error": "Something went wrong while answering. Try again."})
            return
        rag.log_ask(answer)
        self._json(200, answer)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, index=None, ai=False, llm=None):
        super().__init__(address, Handler)
        self.index, self.ai, self.llm = index or rag.Index(), ai, llm
        self._asked, self._lock = [], threading.Lock()

    def allow(self):
        now = time.monotonic()
        with self._lock:
            self._asked = [t for t in self._asked if now - t < RATE_WINDOW]
            if len(self._asked) >= RATE_LIMIT:
                return False
            self._asked.append(now)
            return True


def make_server(port=PORT, ai=None, llm=None, index=None):
    """A server bound to 127.0.0.1. ai=None means: on when GROQ_API_KEY is set."""
    ai = bool(os.environ.get("GROQ_API_KEY")) if ai is None else ai
    return Server((HOST, port), index=index, ai=ai, llm=llm)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the Ask page on this computer.")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-ai", action="store_true", help="never call Groq; show the matching playbook text")
    parser.add_argument("--open", action="store_true", help="open the page in your browser")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.port, ai=False if args.no_ai else None)
    except OSError as error:
        print(f"Could not start on port {args.port}: {error}. Use --port to pick another.", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Ask the playbook is running at {url}  (Ctrl+C to stop)")
    print("AI answers: " + (f"on, model {agent.MODEL}" if server.ai else "off. Set GROQ_API_KEY for written answers; the page shows the playbook text without it."))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
