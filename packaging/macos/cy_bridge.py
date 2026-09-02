#!/usr/bin/env python3
"""CY-CLI local bridge.

Converts the CY CLI's Responses API calls (/v1/responses) into Chat Completions
requests for the CY server (cy.symbiotyc.workers.dev/v1), which only exposes
/v1/chat/completions. The bridge always asks the upstream for a non-streaming
Chat Completions response (the upstream does not support streaming), then
re-emits the answer to the CLI as a minimal Responses SSE stream so the CLI's
streaming response parser can pick it up.

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


def _sse(event, data):
    if data is None:
        return f"event: {event}\n\n".encode()
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _build_sse_stream(chat_resp, model):
    """Translate a non-streaming Chat Completions response into Responses SSE."""
    rid = chat_resp.get("id", "resp-unknown")
    created = chat_resp.get("created", int(time.time()))
    usage = chat_resp.get("usage", {}) or {}
    out_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }

    # response.created (response object shell)
    created_payload = {
        "type": "response.created",
        "response": {
            "id": rid,
            "object": "response",
            "created_at": created,
            "model": model,
            "status": "in_progress",
        },
    }
    yield _sse("response.created", created_payload)

    seq = 1
    for choice in chat_resp.get("choices", []) or []:
        content = (choice.get("message") or {}).get("content", "") or ""
        item_id = f"msg_{rid}_{seq}"
        # output_item.added (message skeleton)
        yield _sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": seq - 1,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        )
        # content_part.added
        yield _sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": seq - 1,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        )
        # output_text.delta (chunked so the CLI can stream UI updates)
        chunk_size = 64
        for i in range(0, len(content), chunk_size):
            chunk = content[i : i + chunk_size]
            yield _sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": seq - 1,
                    "content_index": 0,
                    "delta": chunk,
                },
            )
        # output_text.done
        yield _sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": item_id,
                "output_index": seq - 1,
                "content_index": 0,
                "text": content,
            },
        )
        # content_part.done
        yield _sse(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": item_id,
                "output_index": seq - 1,
                "content_index": 0,
                "part": {"type": "output_text", "text": content, "annotations": []},
            },
        )
        # output_item.done (the full message item, as the parser expects)
        yield _sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": seq - 1,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": content, "annotations": []}
                    ],
                },
            },
        )
        seq += 1

    # response.completed (response usage + status)
    completed_payload = {
        "type": "response.completed",
        "response": {
            "id": rid,
            "object": "response",
            "created_at": created,
            "model": model,
            "status": "completed",
            "usage": out_usage,
        },
    }
    yield _sse("response.completed", completed_payload)


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
                elif isinstance(item, dict) and item.get("type") == "function_call_output":
                    messages.append(
                        {
                            "role": "tool",
                            "content": item.get("output", ""),
                            "name": item.get("name", "tool"),
                        }
                    )
                elif isinstance(item, dict) and "role" in item:
                    c = item.get("content", "")
                    if isinstance(c, list):
                        text = " ".join(
                            p.get("text", "")
                            for p in c
                            if isinstance(p, dict) and "text" in p
                        )
                    else:
                        text = str(c)
                    messages.append({"role": item.get("role", "user"), "content": text})

        if not messages and isinstance(inp, list):
            for item in inp:
                if isinstance(item, dict):
                    role = item.get("role", "user")
                    c = item.get("content", "")
                    if isinstance(c, str):
                        messages.append({"role": role, "content": c})
                    elif isinstance(c, list):
                        for p in c:
                            if isinstance(p, dict) and p.get("type") == "input_text":
                                messages.append({"role": role, "content": p.get("text", "")})

        # Carry over the system/instructions as the first system message.
        instructions = req.get("instructions")
        if instructions:
            messages.insert(0, {"role": "system", "content": instructions})

        model = req.get("model", "cy/i1a")

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
                "model": model,
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

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            for chunk in _build_sse_stream(chat_resp, model):
                self.wfile.write(chunk)
                self.wfile.flush()
            # Force the response to terminate so HTTP/1.1 clients see the end
            # of the stream instead of waiting for keep-alive idle timeout.
            try:
                self.wfile.flush()
            except Exception:
                pass
        except Exception as e:
            self.send_error(502, str(e))


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
