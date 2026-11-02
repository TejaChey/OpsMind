"""Tiny artifact server for the pipeline's `fetch` step.

GET /artifact.json answers 200, EXCEPT while the flag file exists: the first request then
deletes the flag and answers 503 (a transient outage that clears by itself), so a re-run of the
same commit succeeds. Usage: python ci/flaky_server.py [--port P] [--flag PATH]
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def make_handler(flag):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if flag.exists():
                flag.unlink()
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"service unavailable")
                return
            body = b'{"artifact": "ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):   # keep the terminal quiet
            pass
    return Handler


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5077)
    p.add_argument("--flag", default=str(Path(__file__).parent / "state" / "flaky_once"))
    a = p.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(Path(a.flag))).serve_forever()
