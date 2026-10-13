"""Tiny OpenAI-compatible mock: first request returns 429 (retry-after 3s), then fake Bangla."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

calls = 0


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        global calls
        calls += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if calls == 1:
            self.send_response(429)
            self.send_header("retry-after", "3")
            self.end_headers()
            self.wfile.write(b'{"error":"rate limited"}')
            return
        user = body["messages"][-1]["content"].split("<passage>\n", 1)[1].rsplit("\n</passage>", 1)[0]
        out = "\n\n".join(" ".join("অনুবাদ" for _ in p.split()) for p in user.split("\n\n"))
        resp = {
            "choices": [{"message": {"content": out}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": len(out) // 3},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), H).serve_forever()
