# cy_bridge.py

Local Python bridge for the CY-CLI macOS .app. It sits between the bundled
`cy` binary (which speaks the OpenAI **Responses** API on
`http://127.0.0.1:8790/v1`) and the CY server
(`https://cy.symbiotyc.workers.dev/v1`, which only exposes the **Chat
Completions** API).

## What it does

1. Accepts POST `/v1/responses` from the CLI and translates the request into
   a `/v1/chat/completions` call against the configured upstream.
2. Synthesises a Responses-compatible SSE stream from the upstream's plain
   JSON response, so the existing CLI binary can consume it without changes.
3. Injects a short `developer`-role system prompt that overrides the
   upstream's persona and tells the model to be brief, no-greeting,
   no-emoji, and to use tools when it needs file or shell data.
4. Runs a local tool loop. When the upstream emits `tool_calls`, the bridge
   executes them in parallel via `ThreadPoolExecutor` (max 4 workers) and
   feeds the results back as `tool` messages. The CLI never sees the tool
   calls — they are an internal bridge concern.
5. Retries transient upstream failures (HTTP 408/425/429/5xx and
   connection errors) up to 3 times with exponential backoff (0.5s, 1s, 2s).
6. Logs per-request usage (`prompt_tokens`, `completion_tokens`,
   `total_tokens`) and latency to stderr.

## Tools exposed

| name          | purpose                                                |
|---------------|--------------------------------------------------------|
| `read_file`   | Read a UTF-8 file (capped at 65 KiB).                  |
| `write_file`  | Write UTF-8 text to a file, creating parent dirs.      |
| `list_dir`    | List a directory's entries as JSON.                    |
| `shell_exec`  | Run a shell command. Timeout 30s, capped at 120s.      |
| `glob_files`  | Expand a glob pattern (max 1000 matches).              |

## Workspace sandboxing

By default, `read_file`, `write_file`, `list_dir`, and `shell_exec` reject
any path that resolves outside the bridge's current working directory
(typical: `~/`, the user's home). Paths under `~/.cy/` (e.g. `auth.json`)
are always allowed. To lift the sandbox for a single batch job, set:

```
CY_BRIDGE_ROOT=/path/to/workspace
```

`shell_exec` also accepts an explicit `cwd` argument, but the value must
pass the same sandbox check. If the model asks to `rm -rf /` or
`cat /etc/shadow`, the bridge refuses with a `sandbox:` error.

## Configuration

| env var               | default                                    | purpose                                |
|-----------------------|--------------------------------------------|----------------------------------------|
| `CY_API_BASE_URL`     | `https://cy.symbiotyc.workers.dev/v1`      | Upstream base URL.                     |
| `CY_BRIDGE_PORT`      | `8790`                                     | Local port the CLI connects to.        |
| `CY_HOME`             | `~/.cy`                                    | Where `auth.json` lives.               |
| `CY_API_KEY`          | —                                          | Override the API key.                  |
| `CY_BRIDGE_ROOT`      | —                                          | Lift sandbox to this directory.        |
| `CY_BRIDGE_LOG`       | `INFO`                                     | `DEBUG`/`INFO`/`WARNING`/`ERROR`.      |

The API key is resolved, in order, from `CY_API_KEY`, then
`$CY_HOME/auth.json` (fields: `openai_api_key`, `OPENAI_API_KEY`,
`api_key`, `API_KEY`), then the `Authorization` header sent by the CLI.

## Running standalone

```
python3 cy_bridge.py
```

The bridge binds to `127.0.0.1` only and serves until killed. It also
exposes a minimal `GET /` 404 (no UI).

## Versioning

`cy-bridge/2.1` is bundled in CY-CLI v0.2.7+. Older releases use `2.0`
(no retry, no sandbox, no usage logging, no parallel tool execution).
