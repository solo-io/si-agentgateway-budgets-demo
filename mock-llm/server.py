#!/usr/bin/env python3
"""Mock Anthropic Messages API for bulk demo traffic.

Answers POST /v1/messages with a short canned reply and a realistic `usage`
block, so the gateway meters tokens without any model cost. Standard library
only.

Token counts:
  - Input tokens come from the `x-mock-input-tokens` request header when the
    traffic generator sets it (to simulate a large prompt without sending one),
    otherwise from the prompt length (about 4 characters per token).
  - Output tokens come from `x-mock-output-tokens`, otherwise a random share of
    `max_tokens`.

Also answers POST /v1/messages/count_tokens and GET /healthz.
"""

import json
import os
import random
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
# Simulated model latency, seconds per 1,000 output tokens, plus a fixed floor.
LATENCY_PER_KTOK = float(os.environ.get("LATENCY_PER_KTOK", "0.4"))
LATENCY_FLOOR = float(os.environ.get("LATENCY_FLOOR", "0.05"))

REPLIES = [
    "Summary: the claim was filed within the policy window and the adjuster notes match the invoice. Recommend approval.",
    "Generated 12 test cases covering boundary values, null inputs and the retry path. Three need fixture data.",
    "Draft: five headline options and a 120-word teaser, in the house tone. The second headline tested best last quarter.",
    "The legacy COBOL paragraph maps to a single service method. Migration notes are attached as comments.",
    "Here is a concise answer based on the context you provided.",
]


def prompt_chars(body):
    total = 0
    system = body.get("system")
    if isinstance(system, str):
        total += len(system)
    elif isinstance(system, list):
        total += sum(len(b.get("text", "")) for b in system if isinstance(b, dict))
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    return total


def int_header(headers, name):
    try:
        return int(headers.get(name, ""))
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("MOCK_LLM_QUIET") != "1":
            super().log_message(fmt, *args)

    def send_json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/healthz", "/health"):
            self.send_json(200, {"ok": True})
        else:
            self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

    def do_POST(self):
        length = int(self.headers.get("content-length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad json"}})
            return

        path = self.path.split("?")[0]
        input_tokens = int_header(self.headers, "x-mock-input-tokens") or max(1, prompt_chars(body) // 4)

        if path.endswith("/v1/messages/count_tokens"):
            self.send_json(200, {"input_tokens": input_tokens})
            return
        if not path.endswith("/v1/messages"):
            self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})
            return

        max_tokens = int(body.get("max_tokens") or 1024)
        output_tokens = int_header(self.headers, "x-mock-output-tokens")
        if output_tokens is None:
            output_tokens = random.randint(max(1, max_tokens // 8), max(1, max_tokens // 2))
        output_tokens = min(output_tokens, max_tokens)

        time.sleep(LATENCY_FLOOR + LATENCY_PER_KTOK * output_tokens / 1000)

        self.send_json(200, {
            "id": "msg_mock_" + uuid.uuid4().hex[:24],
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-sonnet-4-5"),
            "content": [{"type": "text", "text": random.choice(REPLIES)}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        })


if __name__ == "__main__":
    print(f"mock-llm listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
