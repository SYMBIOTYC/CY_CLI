#!/usr/bin/env python3
"""CY-CLI local bridge.

Converts the CY CLI's Responses API calls (/v1/responses) into Chat Completions
requests for the CY server (cy.symbiotyc.workers.dev/v1), which only exposes
/v1/chat/completions. This lets the release binary work with zero server-side
changes.

No API key is stored in this file. The key is resolved at runtime from, in
order of preference:
  1. the CY_API_KEY environment variable, or
  2. the CY CLI auth file (CY_HOME/auth.json, default ~/.cy/auth.json), or
  3. the Authorization header sent by the CLI itself.
"""
import http.server
import urllib.request
import json
import time
import hashlib
import base64
import os

CY_BASE = os.environ.get("CY_API_BASE_URL", "https://cy.symbiotyc.workers.dev/v1")
PORT = int(os.environ.get("CY_BRIDGE_PORT", "8790"))
CY_HOME = os.environ.get("CY_HOME", os.path.expanduser("~/.cy"))

_PLACEHOLDERS = {"", "cy-local-bridge", "local-bridge", "Bearer"}


def _read_env_key():
    return os.environ.get("CY_API_KEY", "").strip()


def _read_auth_file_key():
    try:
        with open(os.path.join(CY_HOME, "auth.json")) as fh:
            data = json.load(fh)
    except Exception:
        return ""
    for field in ("openai_api_key", "OPENAI_API_KEY", "api_key", "API_KEY"):
        val = data.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _resolve_key(_incoming):
    env = _read_env_key()
    if env:
        return env
    auth = _read_auth_file_key()
    if auth:
        return auth
    if isinstance(_incoming, str):
        bearer = _incoming[len("Bearer "):].strip() if _incoming.startswith("Bearer ") else _incoming.strip()
        if bearer and bearer not in _PLACEHOLDERS:
            return bearer
    return ""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _read_body(self):
        cl = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(cl) if cl else b""

    def do_GET(self):
        if "websocket" in self.headers.get("Upgrade", "").lower() or "Upgrade" in self.headers:
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header(
                "Sec-WebSocket-Accept",
                base64.b64encode(
                    hashlib.sha1(
                        (
                            self.headers.get("Sec-WebSocket-Key", "")
                            + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                        ).encode()
                    ).digest()
                ).decode(),
            )
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self):
        body = self._read_body()
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        try:
            req = json.loads(body)
        except Exception:
            self.send_error(400)
            return

        messages = []
        inp = req.get("input", [])
        if isinstance(inp, str):
            messages = [{"role": "user", "content": inp}]
        else:
            for item in inp:
                if isinstance(item, dict) and item.get("type") == "message":
                    c = item.get("content", "")
                    if isinstance(c, list):
                        text = " ".join(
                            p.get("text", "")
                            for p in c
                            if isinstance(p, dict) and p.get("type") == "input_text"
                        )
                    else:
                        text = str(c)
                    messages.append({"role": item.get("role", "user"), "content": text})

        api_key = _resolve_key(self.headers.get("Authorization", ""))
        if not api_key:
            self.send_error(
                502,
                "No CY API key found. Set CY_API_KEY, add your key to "
                f"{CY_HOME}/auth.json, or run cy login.",
            )
            return

        chat_req = json.dumps(
            {
                "model": req.get("model", "cy/i1a"),
                "messages": messages,
                "stream": False,
            }
        ).encode()
        try:
            fr = urllib.request.Request(
                f"{CY_BASE}/chat/completions",
                data=chat_req,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "python-urllib/3.14",
                },
            )
            with urllib.request.urlopen(fr, timeout=120) as resp:
                chat_resp = json.loads(resp.read())
                output = []
                for choice in chat_resp.get("choices", []):
                    content = choice.get("message", {}).get("content", "")
                    if content:
                        output.append(
                            {
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": content,
                                        "annotations": [],
                                    }
                                ],
                            }
                        )
                usage = chat_resp.get("usage", {})
                out = {
                    "id": chat_resp.get("id", "resp-unknown"),
                    "object": "response",
                    "created_at": chat_resp.get("created", int(time.time())),
                    "model": chat_resp.get("model", "cy/i1a"),
                    "output": output,
                    "usage": {
                        "input_tokens": usage.get("prompt_tokens", 0),
                        "output_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                    "status": "completed",
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())
        except Exception as e:
            self.send_error(502, str(e))


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()