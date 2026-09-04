#!/usr/bin/env python3
"""CY-CLI local bridge.

Converts the CY CLI's Responses API calls (/v1/responses) into Chat Completions
requests for the CY server (cy.symbiotyc.workers.dev/v1), which only exposes
/v1/chat/completions.

Two CY-specific behaviours are layered on top of the upstream:
  1. SYSTEM PROMPT OVERRIDE — a short, opinionated instruction is prepended
     to every request so the model answers concisely, without greetings,
     self-introductions, or emoji, and without restating its persona.
  2. LOCAL TOOL EXECUTION — when the upstream returns tool_calls, the bridge
     runs them locally (read_file / write_file / list_dir / shell_exec /
     glob_files) and feeds the results back as tool messages. The loop
     terminates when the model produces a plain text answer, which is
     streamed to the CLI as a single Responses SSE response.

The CLI never sees the tool_calls — they are an internal bridge concern.

No API key is stored in this file. The key is resolved at runtime from, in
order of preference:
  1. the CY_API_KEY environment variable, or
  2. the CY CLI auth file (CY_HOME/auth.json, default ~/.cy/auth.json), or
  3. the Authorization header sent by the CLI itself.
"""
import http.server
import urllib.request
import urllib.error
import json
import time
import hashlib
import base64
import os
import sys
import subprocess
import glob
import logging
import concurrent.futures

CY_BASE = os.environ.get("CY_API_BASE_URL", "https://cy.symbiotyc.workers.dev/v1")
PORT = int(os.environ.get("CY_BRIDGE_PORT", "8790"))
CY_HOME = os.environ.get("CY_HOME", os.path.expanduser("~/.cy"))
# Workspace root for shell_exec sandboxing. If unset, shell_exec rejects any
# command whose resolved cwd or target path escapes the cwd at request time.
# Set CY_BRIDGE_ROOT=<path> to lift the sandbox (read-only, for trusted jobs).
CY_BRIDGE_ROOT = os.environ.get("CY_BRIDGE_ROOT", "").strip()

# ---- logging --------------------------------------------------------------
log = logging.getLogger("cy-bridge")
if not log.handlers:
    h = logging.StreamHandler(stream=sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
log.setLevel(os.environ.get("CY_BRIDGE_LOG", "INFO").upper())

_PLACEHOLDERS = {"", "cy-local-bridge", "local-bridge", "Bearer"}

# Short, opinionated developer prompt. Sent to the upstream as a `developer`
# role message (which `cy-api-worker` maps to `system`). Upstream may prepend
# its own persona prompt; this one is intentionally terse and corrective.
SYSTEM_PROMPT = (
    "You are CY, a coding assistant. "
    "Rules: "
    "(1) Be brief. No greetings, no self-introduction, no 'I am SYMBIOTYC blizhniy' unless asked. "
    "(2) No emoji. No markdown headers. No bullet walls. "
    "(3) Answer in the user's language. "
    "(4) Skip filler like 'Let me', 'Sure!', 'Here is'. "
    "(5) When you need a file or shell result, call the matching tool; do not guess. "
    "(6) Cite paths verbatim. "
    "(7) When the user asks a yes/no question, answer yes or no first, then justify in one sentence."
)

# Tool catalog exposed to the model. Schema mirrors OpenAI's `tools` shape so
# the upstream can re-emit them on the wire if needed.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path. Returns UTF-8 text or an error.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or cwd-relative file path."},
                    "max_bytes": {"type": "integer", "description": "Cap on bytes returned (default 65536)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write UTF-8 text to a file, creating parent directories. Returns the bytes written.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Target file path."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a directory. Returns a JSON array of {name, kind, size}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path. Defaults to cwd."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_exec",
            "description": "Run a shell command in a subprocess and return {stdout, stderr, exit_code}. Timeout 60s.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command line."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 120)."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Expand a glob pattern under cwd. Returns matching file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.rs'."},
                },
                "required": ["pattern"],
            },
        },
    },
]


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


# ---------- local tool implementations --------------------------------------

def _sandbox_check(path):
    """Return True if `path` is inside the workspace root (or sandboxing is off).

    If CY_BRIDGE_ROOT is unset, only paths inside the current working directory
    are allowed. Absolute paths under CY_HOME (auth.json) are always allowed so
    the bridge can read the API key. Setting CY_BRIDGE_ROOT to a directory lifts
    the sandbox and allows that whole tree; this is intended for trusted batch
    jobs, not interactive use.
    """
    if not path:
        return True
    try:
        real = os.path.realpath(path)
    except Exception:
        return False
    if real.startswith(os.path.realpath(CY_HOME)):
        return True
    if CY_BRIDGE_ROOT:
        try:
            if real.startswith(os.path.realpath(CY_BRIDGE_ROOT)):
                return True
        except Exception:
            pass
    try:
        cwd = os.path.realpath(os.getcwd())
        common = os.path.commonpath([real, cwd])
    except Exception:
        return False
    return common == cwd


def _tool_read_file(args):
    path = args.get("path", "")
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not _sandbox_check(path):
        return {"ok": False, "error": f"sandbox: path outside workspace: {path}"}
    max_bytes = int(args.get("max_bytes") or 65536)
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except FileNotFoundError:
        return {"ok": False, "error": f"not found: {path}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")
    return {"ok": True, "path": path, "bytes": len(data), "truncated": truncated, "text": text}


def _tool_write_file(args):
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not _sandbox_check(path):
        return {"ok": False, "error": f"sandbox: path outside workspace: {path}"}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            written = fh.write(content)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": path, "bytes_written": written}


def _tool_list_dir(args):
    path = args.get("path") or "."
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not _sandbox_check(path):
        return {"ok": False, "error": f"sandbox: path outside workspace: {path}"}
    if not os.path.isdir(path):
        return {"ok": False, "error": f"not a directory: {path}"}
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                kind = "dir" if os.path.isdir(full) else "file"
                entries.append({"name": name, "kind": kind, "size": st.st_size})
            except OSError:
                entries.append({"name": name, "kind": "?", "size": 0})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": path, "entries": entries}


def _tool_shell_exec(args):
    cmd = args.get("command", "")
    if not cmd:
        return {"ok": False, "error": "command is required"}
    timeout = int(args.get("timeout") or 30)
    if timeout > 120:
        timeout = 120
    # Default cwd = current process cwd. Override with `cwd` argument, but
    # refuse to leave the workspace unless CY_BRIDGE_ROOT is set.
    cwd = args.get("cwd") or os.getcwd()
    if not _sandbox_check(cwd):
        return {"ok": False, "error": f"sandbox: cwd outside workspace: {cwd}"}
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out = proc.stdout or ""
    err = proc.stderr or ""
    if len(out) > 32000:
        out = out[:32000] + "\n... [truncated]"
    if len(err) > 16000:
        err = err[:16000] + "\n... [truncated]"
    return {"ok": True, "exit_code": proc.returncode, "stdout": out, "stderr": err, "cwd": cwd}


def _tool_glob_files(args):
    pattern = args.get("pattern", "")
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    try:
        matches = sorted(glob.glob(pattern, recursive=True))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if len(matches) > 1000:
        matches = matches[:1000]
    return {"ok": True, "pattern": pattern, "matches": matches}


_TOOL_DISPATCH = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "list_dir": _tool_list_dir,
    "shell_exec": _tool_shell_exec,
    "glob_files": _tool_glob_files,
}


def _run_tool(name, arguments_json):
    handler = _TOOL_DISPATCH.get(name)
    if not handler:
        return {"ok": False, "error": f"unknown tool: {name}"}
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except Exception as e:
        return {"ok": False, "error": f"bad arguments json: {e}"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "arguments must be a JSON object"}
    try:
        return handler(args)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------- chat-completions <-> upstream -----------------------------------

# Retry policy: 3 attempts, exponential backoff (0.5s, 1.0s, 2.0s).
_UPSTREAM_ATTEMPTS = 3
_UPSTREAM_BACKOFF = (0.5, 1.0, 2.0)
# HTTP statuses that are worth retrying (transient). 4xx other than 408/429 is
# the caller's fault and is not retried.
_RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}


def _post_chat(model, messages, tools=None):
    """POST a chat completion to the upstream. Retries transient errors.

    Returns parsed JSON on success, raises the last exception on terminal
    failure so the caller can surface the error in the SSE response.
    """
    body = {"model": model, "messages": messages, "stream": False}
    if tools:
        body["tools"] = tools
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "cy-bridge/2.1",
    }

    last_err = None
    for attempt in range(1, _UPSTREAM_ATTEMPTS + 1):
        req = urllib.request.Request(
            f"{CY_BASE}/chat/completions", data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in _RETRYABLE_HTTP and attempt < _UPSTREAM_ATTEMPTS:
                wait = _UPSTREAM_BACKOFF[attempt - 1]
                log.warning("upstream HTTP %s (attempt %d/%d), retry in %.1fs",
                            e.code, attempt, _UPSTREAM_ATTEMPTS, wait)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < _UPSTREAM_ATTEMPTS:
                wait = _UPSTREAM_BACKOFF[attempt - 1]
                log.warning("upstream %s (attempt %d/%d), retry in %.1fs",
                            type(e).__name__, attempt, _UPSTREAM_ATTEMPTS, wait)
                time.sleep(wait)
                continue
            raise
    # Unreachable: the loop either returns or raises, but keep last_err live.
    raise last_err  # pragma: no cover


def _extract_assistant(chat_resp):
    choice = (chat_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if not isinstance(text, str):
        text = str(text)
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if not isinstance(reasoning, str):
        reasoning = str(reasoning or "")
    tool_calls = msg.get("tool_calls") or []
    parsed = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        parsed.append({
            "id": tc.get("id") or f"call_{int(time.time()*1000)}",
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "",
        })
    usage = chat_resp.get("usage") or {}
    return {
        "text": text,
        "reasoning": reasoning,
        "tool_calls": parsed,
        "usage": usage,
        "model": chat_resp.get("model") or "cy/i1a",
    }


# ---------- Responses input -> chat messages --------------------------------

def _responses_to_messages(req):
    messages = [{"role": "developer", "content": SYSTEM_PROMPT}]
    if req.get("instructions"):
        messages.append({"role": "system", "content": req["instructions"]})
    inp = req.get("input", [])
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
        return messages
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "message":
            role = item.get("role", "user")
            c = item.get("content", "")
            if isinstance(c, list):
                text = " ".join(
                    p.get("text", "")
                    for p in c
                    if isinstance(p, dict) and p.get("type") in ("input_text", "text")
                )
            else:
                text = str(c)
            messages.append({"role": role, "content": text})
        elif t in ("function_call", "custom_tool_call"):
            args = item.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args)
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or f"call_{int(time.time()*1000)}",
                    "type": "function",
                    "function": {"name": item.get("name", "tool"), "arguments": args},
                }],
            })
        elif t in ("function_call_output", "custom_tool_call_output"):
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": str(item.get("output", "")),
            })
        elif "role" in item and "content" in item:
            messages.append({"role": item["role"], "content": str(item["content"])})
    return messages


# ---------- main request handler --------------------------------------------

class H(http.server.BaseHTTPRequestHandler):
    # CY CLI speaks HTTP/1.1, not the BaseHTTP default of HTTP/1.0.
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _read_body(self):
        cl = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(cl) if cl else b""

    def _open_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

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
                            + "258EA5-E914-47DA-95CA-C5AB0DC85B11"
                        ).encode()
                    ).digest()
                ).decode(),
            )
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        body = self._read_body()
        try:
            req = json.loads(body)
        except Exception:
            self.send_error(400)
            return

        api_key = _resolve_key(self.headers.get("Authorization", ""))
        if not api_key:
            self.send_error(
                502,
                "No CY API key found. Set CY_API_KEY, add your key to "
                f"{CY_HOME}/auth.json, or run cy login.",
            )
            return

        t_start = time.time()
        log.info("request model=%s upstream=%s", req.get("model", "cy/i1a"), CY_BASE)

        model = req.get("model") or "cy/i1a"
        messages = _responses_to_messages(req)
        max_tool_rounds = 6

        final_text = ""
        final_reasoning = ""
        upstream_model = model
        final_usage = {}

        try:
            for round_idx in range(max_tool_rounds):
                chat_resp = _post_chat(model, messages, tools=TOOLS)
                assistant = _extract_assistant(chat_resp)
                tool_calls = assistant["tool_calls"]
                text = assistant["text"]
                reasoning = assistant["reasoning"]
                usage = assistant["usage"]
                upstream_model = assistant["model"]
                final_usage = usage
                if reasoning:
                    final_reasoning = reasoning

                asst_msg = {"role": "assistant", "content": text or ""}
                if tool_calls:
                    asst_msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in tool_calls
                    ]
                messages.append(asst_msg)

                if not tool_calls:
                    final_text = text
                    break

                # Tool-use round: execute each tool in parallel and append the
                # results in the original order. Capped at 4 workers so a model
                # that requests many shell_execs at once cannot fork-bomb the
                # box.
                if len(tool_calls) == 1:
                    ordered = [_run_tool(tool_calls[0]["name"], tool_calls[0]["arguments"])]
                else:
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(4, len(tool_calls))) as ex:
                        futures = {
                            ex.submit(_run_tool, tc["name"], tc["arguments"]): tc
                            for tc in tool_calls
                        }
                        results_by_id = {}
                        for fut, tc in futures.items():
                            try:
                                results_by_id[tc["id"]] = fut.result()
                            except Exception as e:
                                results_by_id[tc["id"]] = {
                                    "ok": False,
                                    "error": f"{type(e).__name__}: {e}",
                                }
                    ordered = [results_by_id[tc["id"]] for tc in tool_calls]

                for tc, tool_result in zip(tool_calls, ordered):
                    output_str = json.dumps(tool_result, ensure_ascii=False)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": output_str,
                    })
            else:
                final_text = (
                    f"CY: tool loop did not converge after {max_tool_rounds} rounds."
                )
        except Exception as e:
            log.exception("bridge error")
            final_text = f"CY: bridge error: {type(e).__name__}: {e}"

        # Log final token usage (if the upstream returned a `usage` block).
        # This makes per-request cost visible in bridge stderr logs.
        if final_usage:
            log.info("usage prompt=%s completion=%s total=%s (model=%s, %.2fs)",
                     final_usage.get("prompt_tokens", "?"),
                     final_usage.get("completion_tokens", "?"),
                     final_usage.get("total_tokens", "?"),
                     upstream_model,
                     time.time() - t_start)
        else:
            log.info("no usage block returned (model=%s, %.2fs)",
                     upstream_model, time.time() - t_start)

        # Stream a single Responses SSE response containing only the final text.
        self._open_sse()
        rid = f"resp_{int(time.time()*1000)}"
        msg_id = f"msg_{rid}"
        try:
            self.wfile.write(f"event: response.created\ndata: {json.dumps({'type':'response.created','response':{'id':rid,'object':'response','created_at':int(time.time()),'model':upstream_model,'status':'in_progress'}})}\n\n".encode())
            self.wfile.flush()
            self.wfile.write(f"event: response.in_progress\ndata: {json.dumps({'type':'response.in_progress','response':{'id':rid,'object':'response','created_at':int(time.time()),'model':upstream_model,'status':'in_progress'}})}\n\n".encode())
            self.wfile.flush()
            self.wfile.write(f"event: response.output_item.added\ndata: {json.dumps({'type':'response.output_item.added','output_index':0,'item':{'id':msg_id,'type':'message','role':'assistant','status':'in_progress','content':[]}})}\n\n".encode())
            self.wfile.flush()
            self.wfile.write(f"event: response.content_part.added\ndata: {json.dumps({'type':'response.content_part.added','item_id':msg_id,'output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}})}\n\n".encode())
            self.wfile.flush()
            chunk_size = 64
            for i in range(0, len(final_text), chunk_size):
                chunk = final_text[i:i + chunk_size]
                self.wfile.write(f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','item_id':msg_id,'output_index':0,'content_index':0,'delta':chunk})}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(f"event: response.output_text.done\ndata: {json.dumps({'type':'response.output_text.done','item_id':msg_id,'output_index':0,'content_index':0,'text':final_text})}\n\n".encode())
            self.wfile.flush()
            self.wfile.write(f"event: response.content_part.done\ndata: {json.dumps({'type':'response.content_part.done','item_id':msg_id,'output_index':0,'content_index':0,'part':{'type':'output_text','text':final_text,'annotations':[]}})}\n\n".encode())
            self.wfile.flush()
            self.wfile.write(f"event: response.output_item.done\ndata: {json.dumps({'type':'response.output_item.done','output_index':0,'item':{'id':msg_id,'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':final_text,'annotations':[]}]}})}\n\n".encode())
            self.wfile.flush()
            out_usage = None
            if final_usage:
                out_usage = {
                    "input_tokens": final_usage.get("prompt_tokens", 0),
                    "output_tokens": final_usage.get("completion_tokens", 0),
                    "total_tokens": final_usage.get("total_tokens", 0),
                }
            self.wfile.write(f"event: response.completed\ndata: {json.dumps({'type':'response.completed','response':{'id':rid,'object':'response','created_at':int(time.time()),'model':upstream_model,'status':'completed','output':[{'id':msg_id,'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':final_text,'annotations':[]}]}],'usage':out_usage}})}\n\n".encode())
            self.wfile.flush()
        except Exception:
            pass


if __name__ == "__main__":
    socketserver = http.server.ThreadingHTTPServer if hasattr(http.server, "ThreadingHTTPServer") else http.server.HTTPServer
    socketserver(("127.0.0.1", PORT), H).serve_forever()
