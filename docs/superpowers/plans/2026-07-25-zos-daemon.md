# Z.OS Headless Desktop Agent Daemon — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `zosd`, a persistent headless Claude Agent SDK daemon on Ubuntu/GNOME Wayland that accepts plain-English intents over a Unix socket, gates every tool call through a fail-closed permission callback, and runs long work as detached tmux sessions.

**Architecture:** One `systemd --user` Python process owns a Unix socket and a single long-lived `ClaudeSDKClient`. Dumb clients (`zenity --entry` piped to `socat`) push `{"source","text"}` JSON. Mode commands are matched and consumed by the daemon *before* the agent sees the text, so the agent has no code path to widen its own permissions. All tool calls route through `can_use_tool`, which auto-allows a Safe set, auto-allows metacharacter-free read-only Bash, and otherwise blocks on a `notify-send` action-button prompt whose every failure path resolves to deny. Long work becomes a tmux session, which supplies persistence, logging, status, viewer, and stop for free.

**Tech Stack:** Python 3.14 (fallback: `uv`-pinned 3.12), `claude-agent-sdk` (Python), `asyncio` unix server, `tmux`, `libnotify` 0.8.8 (`notify-send -A ... -w`), `zenity`, `socat`, `systemd --user`, `gsettings`.

## Global Constraints

- **Model:** `claude-opus-5`. Exact string, no date suffix.
- **Repo root (absolute, used in the systemd unit and keybinding):** `/run/media/yash/External/Zerostic/Z.OS`
- **Socket:** `$XDG_RUNTIME_DIR/zos.sock`, mode `0600`. Never TCP, not even loopback.
- **Audit log:** `~/.local/share/zos/audit.log`, one JSON object per line, appended in **every** mode for **every** tool call and verdict.
- **Startup mode is always `guarded`.** No persistence of mode across process lifetime.
- **Gate polarity is allow-list.** The code must read `if tool_name in SAFE_TOOLS: allow ... else: prompt`. Never `if tool_name in GUARDED: prompt else: allow` — the second form fails *open* on any unanticipated tool name.
- **Every failure path in the prompt returns deny.** Timeout, dismissal, missing `notify-send`, dead notification daemon, any exception.
- **Custom tool names are namespaced by the SDK.** `create_sdk_mcp_server(name="zos", ...)` exposes tools as `mcp__zos__<toolname>`. Built-ins stay bare. The gate matches a mixed namespace. Canonical literals used everywhere in this plan:
  - Safe: `Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, `TodoWrite`, `mcp__zos__job_list`, `mcp__zos__notify`
  - Guarded: `Bash`, `Write`, `Edit`, `mcp__zos__job_start`, `mcp__zos__job_show`, `mcp__zos__job_kill`, and anything not in Safe
- **Nothing blocks the event loop.** All subprocess work uses `asyncio.create_subprocess_exec`; the 60s prompt uses `asyncio.wait_for`. A blocking `subprocess.run` inside `can_use_tool` would stall the socket accept loop for the whole prompt.
- **`can_use_tool(tool_name, input_data, context)` has no `source` parameter.** The daemon sets `self.current_source` / `self.current_intent` before `await client.query(...)` and the callback reads them. Safe only because requests are serialized under one `asyncio.Lock`. Do not invent a parallel request path.
- **Tests:** one file, `test_zos.py`, plain `assert`, no framework. Run with `python3 test_zos.py`.
- **Spec reconciliation:** the spec's Guarded table lists `app_launch`, but its custom-tools table does not and its prose says app launching is plain `Bash` + `gtk-launch`. This plan builds **no `app_launch` tool**; the table's mention is dropped.

## File Structure

```
zosd.py        daemon: socket loop, one ClaudeSDKClient, mode matcher, permission gate, audit log
tools.py       five in-process SDK MCP tools: notify, job_start, job_list, job_show, job_kill
zos            client shim: zenity --entry (or argv) -> JSON -> socat -> socket
zos-askpass    SUDO_ASKPASS helper: zenity --password
zos.service    systemd --user unit
test_zos.py    assert-based tests for gate, mode matcher, prompt failure, job round-trip
docs/superpowers/plans/notes-sdk-findings.md   Task 1's recorded empirical answers
```

`zosd.py` holds the gate and mode matcher rather than a separate module: together they are ~90 lines, they change together, and the spec fixes the file count at four.

---

### Task 1: Environment verification and recorded SDK findings

The two flagged unknowns from the spec are settled here, empirically, and written down. Later tasks read the recorded answers instead of guessing.

**Files:**
- Create: `docs/superpowers/plans/notes-sdk-findings.md`
- Create: `/tmp/zos-smoke.py` (throwaway, not committed)

**Interfaces:**
- Produces: `notes-sdk-findings.md` containing, verbatim, the answers to Q1–Q4 below. Task 2 reads Q1/Q2 to choose the `python3` vs `uv run` invocation. Task 2 and Task 4 read Q3/Q4 to set `allowed_tools`.

- [ ] **Step 1: Install the SDK on system Python 3.14**

```bash
cd /run/media/yash/External/Zerostic/Z.OS
python3 -m venv .venv
.venv/bin/pip install claude-agent-sdk
.venv/bin/python -c "import claude_agent_sdk as s; print(s.__file__)"
```

Expected: an import with no `anyio` / `typing` traceback. If it raises, that is **Q1 = broken** — do Step 2. If it imports, record **Q1 = works** and skip Step 2.

- [ ] **Step 2: Only if Step 1 failed — pin 3.12 via uv**

```bash
cd /run/media/yash/External/Zerostic/Z.OS
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python claude-agent-sdk
.venv/bin/python -c "import claude_agent_sdk; print('ok')"
```

If `uv` is not installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

- [ ] **Step 3: Write the smoke test that answers the gate questions**

The critical unknown: `allowed_tools` is a pre-approval list. The SDK docs' example lists `mcp__calc__add` there. If listing is *required for availability* **and** listing *pre-approves*, then "nothing routes around the gate" is false. This script settles it.

```python
# /tmp/zos-smoke.py
import asyncio, sys
from claude_agent_sdk import (
    ClaudeAgentOptions, ClaudeSDKClient, tool, create_sdk_mcp_server,
    PermissionResultAllow, PermissionResultDeny,
)

seen = []

@tool("ping", "Return the string pong", {})
async def ping(args):
    return {"content": [{"type": "text", "text": "pong"}]}

server = create_sdk_mcp_server("zos", "1.0.0", [ping])

async def gate(tool_name, input_data, context):
    seen.append(tool_name)
    print("GATE FIRED:", tool_name, file=sys.stderr)
    return PermissionResultAllow()

async def run(allowed):
    seen.clear()
    # Bash matters as much as the MCP tools: `tools` and `allowed_tools` are
    # separate axes, and Bash is the gate's primary consumer.
    opts = ClaudeAgentOptions(
        model="claude-opus-5",
        mcp_servers={"zos": server},
        allowed_tools=allowed,
        permission_mode="default",
        can_use_tool=gate,
    )
    text = []
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(
            "Run `echo hi` using Bash, then call the ping tool exactly once, "
            "then tell me what each returned."
        )
        async for msg in client.receive_response():
            text.append(str(msg))
    joined = "\n".join(text)
    print(f"--- allowed_tools={allowed!r}")
    print("  gate fired for:", seen)
    print("  tool reachable:", "pong" in joined)

asyncio.run(run([]))
asyncio.run(run(["mcp__zos__ping"]))
```

- [ ] **Step 4: Run it and read the four answers off the output**

Run: `.venv/bin/python /tmp/zos-smoke.py`

- **Q1** Does `claude-agent-sdk` import on this Python? (from Step 1/2)
- **Q2** Which interpreter path works — `.venv/bin/python` on 3.14, or the uv-pinned 3.12?
- **Q3** With `allowed_tools=[]`, did the gate fire for `mcp__zos__ping`, and was the tool reachable (`pong` present)?
- **Q4** With `allowed_tools=["mcp__zos__ping"]`, did the gate still fire, or was it bypassed?
- **Q5** Did the gate fire for `Bash` in either run? (built-ins are a separate axis from MCP tools)

- [ ] **Step 5: Record the answers**

Write `docs/superpowers/plans/notes-sdk-findings.md`:

```markdown
# Task 1 findings — recorded 2026-07-25

- **Q1 SDK imports:** <yes | no, traceback: ...>
- **Q2 interpreter:** <.venv/bin/python (3.14) | .venv/bin/python (uv-pinned 3.12)>
- **Q3 allowed_tools=[]:** gate fired: <yes|no>; mcp tool reachable: <yes|no>
- **Q4 allowed_tools listed:** gate fired: <yes|no> (no = listing pre-approves and bypasses the gate)
- **Q5 Bash:** gate fired for `Bash` with allowed_tools=[]: <yes|no>. If no, STOP —
  the whole permission model rests on the callback seeing every `Bash` call. Do not
  proceed to Task 2; report it.

## Decision for Task 2

- If Q3 is "gate fired: yes, reachable: yes" -> **Branch A**: `allowed_tools=[]`,
  the callback is the sole gate. Preferred.
- If Q3 is "reachable: no" and Q4 is "gate fired: yes" -> **Branch B**: list all
  `mcp__zos__*` names in `allowed_tools`; the callback still gates them.
- If Q3 is "reachable: no" and Q4 is "gate fired: no" -> **Branch C**: listing is
  required for availability AND pre-approves, so MCP tools cannot be guarded.
  Keep only the two Safe tools (`mcp__zos__notify`, `mcp__zos__job_list`) as MCP
  tools and list those. Delete `job_start`/`job_show`/`job_kill` from tools.py;
  the agent runs `tmux new-session -d -s <name> <cmd>` etc. through plain `Bash`,
  which is already Guarded. Task 4 has this branch spelled out.
```

- [ ] **Step 6: Commit**

```bash
cd /run/media/yash/External/Zerostic/Z.OS
printf '.venv/\n__pycache__/\n' >> .gitignore
git add .gitignore docs/superpowers/plans/notes-sdk-findings.md
git commit -m "chore: record Agent SDK environment and permission-gate findings"
```

---

### Task 2: Custom tools module

**Files:**
- Create: `tools.py`

**Interfaces:**
- Consumes: Task 1's Q2 (interpreter) and the Branch A/B/C decision.
- Produces: `zos_server` (an `McpSdkServerConfig` from `create_sdk_mcp_server("zos", "1.0.0", ...)`), and the module-level tool objects `notify`, `job_start`, `job_list`, `job_show`, `job_kill`, each an `SdkMcpTool` with an awaitable `.handler(args: dict) -> dict` attribute. `test_zos.py` calls `.handler` directly. `zosd.py` imports only `zos_server`.
- Produces: `async def sh(*argv) -> str` — combined stdout/stderr, stripped; returns `f"exit {code}"` when output is empty.

- [ ] **Step 1: Write `tools.py`**

```python
"""Z.OS custom tools. Thin wrappers only — anything that is a plain one-line
shell command (clipboard, screenshots, gtk-launch, git) is left to Bash."""
import asyncio

from claude_agent_sdk import create_sdk_mcp_server, tool


async def sh(*argv: str) -> str:
    p = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await p.communicate()
    return out.decode(errors="replace").strip() or f"exit {p.returncode}"


def ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@tool("notify", "Show a desktop notification to the user", {"text": str})
async def notify(args):
    await sh("notify-send", "Z.OS", args["text"])
    return ok("notified")


@tool("job_start", "Run a long-running command in a detached tmux session. "
                   "Returns immediately; never blocks.", {"name": str, "cmd": str})
async def job_start(args):
    return ok(await sh("tmux", "new-session", "-d", "-s", args["name"], args["cmd"]))


@tool("job_list", "List running Z.OS jobs (tmux sessions)", {})
async def job_list(args):
    return ok(await sh("tmux", "ls"))


@tool("job_show", "Open a terminal window attached to a running job", {"name": str})
async def job_show(args):
    return ok(await sh("gnome-terminal", "--", "tmux", "attach", "-t", args["name"]))


@tool("job_kill", "Stop a running job", {"name": str})
async def job_kill(args):
    return ok(await sh("tmux", "kill-session", "-t", args["name"]))


zos_server = create_sdk_mcp_server(
    "zos", "1.0.0", [notify, job_start, job_list, job_show, job_kill]
)
```

If Task 1 recorded **Branch C**, delete `job_start`, `job_show`, `job_kill` and their entries in the `create_sdk_mcp_server` list, leaving `notify` and `job_list`.

- [ ] **Step 2: Verify the empty input schema and the handler attribute**

The `{}` schema on `job_list` is the one shape not shown in the SDK docs. Check it constructs, and confirm `.handler` is reachable.

Run:
```bash
.venv/bin/python -c "
import asyncio, tools
print(type(tools.job_list).__name__, tools.job_list.name, tools.job_list.input_schema)
print(asyncio.run(tools.notify.handler({'text': 'Z.OS tools.py alive'})))
"
```
Expected: `SdkMcpTool job_list {}`, a `{'content': [...]}` dict, and a visible desktop notification. If `{}` raises, change the schema to `{"unused": str}` and note it in `notes-sdk-findings.md`.

- [ ] **Step 3: Commit**

```bash
cd /run/media/yash/External/Zerostic/Z.OS
git add tools.py
git commit -m "feat: add Z.OS custom tools (notify, tmux job control)"
```

---

### Task 3: Daemon skeleton — socket loop, one agent session, no gate yet

Prove the whole loop end-to-end before adding security, so a failure is unambiguous: socket, or agent, or notification.

**Files:**
- Create: `zosd.py`
- Create: `zos`

**Interfaces:**
- Consumes: `tools.zos_server`; Task 1's Branch decision for `allowed_tools`.
- Produces: `class Daemon` with `__init__(self)` taking no arguments and setting only in-memory state (so tests can construct it without connecting), `async def run(self)`, `async def handle(self, reader, writer)`. Attributes `self.auto: bool`, `self.current_source: str`, `self.current_intent: str`, `self.lock: asyncio.Lock`, `self.badge_id: str | None`, `self.client: ClaudeSDKClient | None`.
- Produces: module constants `SOCK: pathlib.Path`, `AUDIT: pathlib.Path`, `NOTIFY: str = "notify-send"`.
- Produces: the client shim `zos`, which sends `{"source": "user", "text": <str>}` as one JSON object and closes the connection.

- [ ] **Step 1: Write `zosd.py` (skeleton — gate is a permissive stub, replaced in Task 4)**

```python
#!/usr/bin/env python3
"""Z.OS daemon. One socket, one persistent agent session, one permission gate."""
import asyncio
import json
import os
import pathlib

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
)

from tools import zos_server

SOCK = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "zos.sock"
AUDIT = pathlib.Path.home() / ".local/share/zos/audit.log"
NOTIFY = "notify-send"

SYSTEM_APPEND = """You are Z.OS, a headless agent on this user's Ubuntu GNOME
(Wayland) desktop. You have no chat window; the user sees nothing unless you use
the notify tool. Always finish by calling mcp__zos__notify with a one-line result.
Never block: anything that could take more than a few seconds goes to
mcp__zos__job_start as a tmux session, and you say so and return.
For root, run `sudo -A <cmd>` so the OS's own password dialog appears.
xdotool does not work on native Wayland windows; do not use it."""

# ponytail: allowed_tools=[] per Task 1 Branch A — the can_use_tool callback is
# the sole gate. Switch to the Branch B/C list only if notes-sdk-findings.md says so.
ALLOWED_TOOLS: list[str] = []


class Daemon:
    def __init__(self):
        self.auto = False          # startup mode is always guarded
        self.current_source = "user"
        self.current_intent = ""
        self.badge_id = None
        self.lock = asyncio.Lock()
        self.client = None

    async def can_use_tool(self, tool_name, input_data, context):
        return PermissionResultAllow()   # replaced in Task 4

    async def handle(self, reader, writer):
        raw = await reader.read()   # to EOF — a bounded read can split the JSON
        writer.close()
        try:
            msg = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return
        text = str(msg.get("text", "")).strip()
        if not text:
            return
        source = str(msg.get("source", "user"))
        async with self.lock:
            self.current_intent, self.current_source = text, source
            await self.client.query(text)
            async for _ in self.client.receive_response():
                pass

    async def run(self):
        opts = ClaudeAgentOptions(
            model="claude-opus-5",
            mcp_servers={"zos": zos_server},
            allowed_tools=ALLOWED_TOOLS,
            permission_mode="default",
            can_use_tool=self.can_use_tool,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": SYSTEM_APPEND},
        )
        async with ClaudeSDKClient(options=opts) as client:
            self.client = client
            SOCK.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(self.handle, path=SOCK)
            # ponytail: chmod after bind leaves a sub-millisecond wider window, but
            # $XDG_RUNTIME_DIR is already 0700 and user-owned, so it is unreachable.
            SOCK.chmod(0o600)
            async with server:
                await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(Daemon().run())
```

- [ ] **Step 2: Write the client shim `zos`**

```bash
#!/usr/bin/env bash
# Z.OS client. No parsing, ever — the daemon's agent decides what the text means.
set -euo pipefail

if [ "$#" -gt 0 ]; then
  text="$*"
else
  text="$(zenity --entry --title="Z.OS" --text="" 2>/dev/null || true)"
fi
[ -n "$text" ] || exit 0

python3 -c 'import json,sys; sys.stdout.write(json.dumps({"source":"user","text":sys.argv[1]}))' "$text" \
  | socat - "UNIX-CONNECT:$XDG_RUNTIME_DIR/zos.sock"
```

```bash
chmod +x /run/media/yash/External/Zerostic/Z.OS/zos
```

- [ ] **Step 3: Prove the loop end-to-end by hand**

Terminal A:
```bash
cd /run/media/yash/External/Zerostic/Z.OS && .venv/bin/python zosd.py
```
Terminal B:
```bash
cd /run/media/yash/External/Zerostic/Z.OS && ./zos "notify me that the loop works"
```
Expected: a desktop notification reading roughly "the loop works". Then run `./zos` with no arguments and confirm a `zenity` entry box appears and does the same thing.

- [ ] **Step 4: Prove the session is persistent**

```bash
./zos "remember the number 41"
./zos "add one to the number you were told and notify me the result"
```
Expected: a notification containing `42`. This is the whole point of the daemon — a fresh process per invocation could not do it.

- [ ] **Step 5: Verify the socket permissions**

Run: `stat -c '%a %U' "$XDG_RUNTIME_DIR/zos.sock"`
Expected: `600 yash`

- [ ] **Step 6: Commit**

```bash
cd /run/media/yash/External/Zerostic/Z.OS
git add zosd.py zos
git commit -m "feat: zosd socket loop with persistent agent session and zenity client"
```

---

### Task 4: Permission gate, mode matcher, audit log, tests

**Files:**
- Modify: `zosd.py` (replace the `can_use_tool` stub; add the gate helpers, mode matcher, badge, audit)
- Create: `test_zos.py`

**Interfaces:**
- Consumes: `Daemon` from Task 3, `tools.job_start` / `job_list` / `job_kill` handlers from Task 2.
- Produces: `def judge_bash(cmd: str) -> bool` — `True` means auto-allow.
- Produces: `def match_mode(text: str) -> bool | None` — `True` for `auto`, `False` for `guarded`, `None` for anything else (not a mode command).
- Produces: `async def prompt_user(intent: str, detail: str) -> str` — blocking action-button prompt returning `"allow"`, `"deny"` (user clicked Deny), or `"fail"` (timeout, dismissal, missing `notify-send`, dead notification daemon). Both non-`"allow"` values block; the distinction only selects `interrupt`.
- Produces: `def audit(**fields) -> None`.
- Produces: `Daemon._decide_fast(self, tool_name, input_data) -> tuple[bool | None, str]` — `(True, why)` allow, `(False, why)` deny, `(None, why)` must prompt. This is the pure, synchronous, fully testable core of the gate.
- Produces: `Daemon.set_mode(self, auto: bool, source: str)` (async), `Daemon._narrate(self, detail: str)` (async, non-blocking).
- Produces: module constants `SAFE_TOOLS: set[str]`, `METACHARS: set[str]`, `SAFE_PREFIXES: list[tuple[str, ...]]`.

- [ ] **Step 1: Write the failing tests**

```python
#!/usr/bin/env python3
"""Z.OS tests. Plain asserts, no framework. Run: python3 test_zos.py"""
import asyncio

import tools
import zosd


def test_metachar_check_rejects_compound_commands():
    assert zosd.judge_bash("ls; rm -rf ~") is False
    assert zosd.judge_bash("cat /etc/passwd > /tmp/leak") is False
    assert zosd.judge_bash("ls $(whoami)") is False
    assert zosd.judge_bash("ls && rm -rf /") is False
    assert zosd.judge_bash("ls\nrm -rf /") is False


def test_allowlist_allows_readonly_and_nothing_else():
    assert zosd.judge_bash("ls -la /tmp") is True
    assert zosd.judge_bash("git status") is True
    assert zosd.judge_bash("tmux ls") is True
    assert zosd.judge_bash("git push") is False
    assert zosd.judge_bash("rm -rf /") is False
    assert zosd.judge_bash("sudo -A apt update") is False


def test_gate_fails_closed_on_unknown_tool():
    # Allow-list polarity: a tool name nobody predicted must prompt, not run.
    verdict, _ = zosd.Daemon()._decide_fast("SomeToolInventedNextYear", {})
    assert verdict is None


def test_safe_tools_auto_allow():
    d = zosd.Daemon()
    for name in ("Read", "Grep", "mcp__zos__notify", "mcp__zos__job_list"):
        assert d._decide_fast(name, {})[0] is True


def test_guarded_tools_prompt_in_guarded_mode():
    d = zosd.Daemon()
    assert d._decide_fast("Write", {"file_path": "/tmp/x"})[0] is None
    assert d._decide_fast("mcp__zos__job_start", {"name": "x", "cmd": "sleep 1"})[0] is None
    assert d._decide_fast("Bash", {"command": "rm -rf /tmp/x"})[0] is None


def test_fresh_daemon_state_is_guarded():
    # The "reverts on restart" property: a newly constructed daemon is guarded.
    assert zosd.Daemon().auto is False


def test_auto_mode_is_sticky_and_never_expires():
    d = zosd.Daemon()
    d.auto = True
    d.current_source = "user"
    for _ in range(200):
        assert d._decide_fast("Bash", {"command": "rm -rf /tmp/x"})[0] is True
    assert d.auto is True


def test_auto_mode_only_applies_to_the_human_at_the_keyboard():
    d = zosd.Daemon()
    d.auto = True
    d.current_source = "cron"
    assert d._decide_fast("Bash", {"command": "rm -rf /tmp/x"})[0] is None


def test_mode_matcher_is_strict():
    assert zosd.match_mode("auto") is True
    assert zosd.match_mode("  Guarded  ") is False
    assert zosd.match_mode("go full auto") is None
    assert zosd.match_mode("don't go full auto") is None
    assert zosd.match_mode("auto for 30m") is None
    assert zosd.match_mode("list my files") is None


def test_prompt_denies_when_the_prompt_mechanism_is_broken():
    saved = zosd.NOTIFY
    zosd.NOTIFY = "zos-no-such-binary"
    try:
        # "fail", not "deny": a broken prompt must never be reported as a user choice.
        assert asyncio.run(zosd.prompt_user("do a thing", "rm -rf /")) == "fail"
    finally:
        zosd.NOTIFY = saved


def test_job_start_creates_a_real_tmux_session_and_job_kill_removes_it():
    name = "zos-selftest"
    listing = lambda: asyncio.run(tools.job_list.handler({}))["content"][0]["text"]
    asyncio.run(tools.job_kill.handler({"name": name}))      # clean slate
    asyncio.run(tools.job_start.handler({"name": name, "cmd": "sleep 60"}))
    assert name in listing()
    asyncio.run(tools.job_kill.handler({"name": name}))
    assert name not in listing()


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
            print("ok", _name)
    print("all passed")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /run/media/yash/External/Zerostic/Z.OS && .venv/bin/python test_zos.py`
Expected: FAIL with `AttributeError: module 'zosd' has no attribute 'judge_bash'`

- [ ] **Step 3: Add the gate helpers, mode matcher, badge, and audit to `zosd.py`**

First confirm the import block at the top of `zosd.py` names **both** results — the
stub only needed `PermissionResultAllow`, and the gate below returns `PermissionResultDeny`:

```python
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
)
```

Then insert after the `NOTIFY` constant:

```python
import shlex
import time

SAFE_TOOLS = {
    "Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite",
    "mcp__zos__job_list", "mcp__zos__notify",
}

METACHARS = set(";&|`$()><\n")

# Grows from real usage, never speculation. Matched as a token prefix, so
# ("git", "status") allows `git status --short` but not `git push`.
SAFE_PREFIXES: list[tuple[str, ...]] = [
    ("ls",), ("cat",), ("head",), ("tail",), ("wc",), ("pwd",), ("whoami",),
    ("date",), ("df",), ("du",), ("ps",), ("free",), ("uptime",), ("uname",),
    ("which",), ("id",), ("hostname",),
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "branch"),
    ("tmux", "ls"), ("tmux", "list-sessions"),
    ("systemctl", "--user", "status"),
]

MODE_WORDS = {"auto": True, "guarded": False}


def judge_bash(cmd: str) -> bool:
    """True = auto-allow. Deliberately paranoid; no shell parsing is attempted."""
    if any(c in METACHARS for c in cmd):
        return False
    try:
        toks = tuple(shlex.split(cmd))
    except ValueError:
        return False
    return any(toks[: len(p)] == p for p in SAFE_PREFIXES)


def match_mode(text: str):
    """True=auto, False=guarded, None=not a mode command. Strict by design: a
    missed 'go full auto' is a harmless retype; a fuzzy match that fires on
    'don't go full auto' is not."""
    return MODE_WORDS.get(text.strip().lower())


def audit(**fields) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as f:
        f.write(json.dumps({"ts": time.time(), **fields}, default=str) + "\n")


async def prompt_user(intent: str, detail: str) -> str:
    """Blocking allow/deny prompt with no window. Returns "allow", "deny" (the user
    clicked Deny) or "fail" (timeout, dismissal, missing notify-send, dead
    notification daemon). Only "allow" permits the call — the failure mode of the
    prompt system must be 'nothing happens'."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            NOTIFY, "-u", "critical", "-A", "allow=Allow", "-A", "deny=Deny", "-w",
            "Z.OS", f"you said: {intent}\nwants to run: {detail}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        answer = out.decode(errors="replace").strip()
        return answer if answer in ("allow", "deny") else "fail"
    except Exception:
        if proc is not None and proc.returncode is None:
            proc.kill()
        return "fail"
```

Replace the `can_use_tool` stub and add the rest of the `Daemon` methods:

```python
    def _decide_fast(self, tool_name, input_data):
        """(True|False|None, why). None means 'must prompt'. Allow-list polarity:
        an unrecognised tool name lands on the final return and prompts."""
        if tool_name in SAFE_TOOLS:
            return True, "safe class"
        if tool_name == "Bash" and judge_bash(str(input_data.get("command", ""))):
            # ponytail: read-only allowlist is effectively the Safe class, so it
            # applies to every source, not just the human.
            return True, "readonly allowlist"
        if self.auto and self.current_source == "user":
            return True, "auto mode"
        return None, "prompt"

    @staticmethod
    def _render(tool_name, input_data):
        if tool_name == "Bash":
            return str(input_data.get("command", ""))
        return f"{tool_name} {json.dumps(input_data, default=str)[:300]}"

    async def _narrate(self, detail: str):
        """Auto mode trades the veto, not the visibility. No prompt fired, so this
        after-the-fact notification is the only real-time signal that this ran."""
        await asyncio.create_subprocess_exec(
            NOTIFY, "Z.OS (auto)", detail[:200],
            stderr=asyncio.subprocess.DEVNULL)

    async def can_use_tool(self, tool_name, input_data, context):
        allow, why = self._decide_fast(tool_name, input_data)
        detail = self._render(tool_name, input_data)
        interrupt = False
        if allow is None:
            answer = await prompt_user(self.current_intent, detail)
            allow = answer == "allow"
            why = {"allow": "user allowed", "deny": "user denied",
                   "fail": "prompt failed"}[answer]
            # An explicit Deny stops the turn; a bare deny just hands the message
            # back and the model retries a variant of the same command.
            interrupt = answer == "deny"
        audit(tool=tool_name, detail=detail, verdict="allow" if allow else "deny",
              reason=why, mode="auto" if self.auto else "guarded",
              source=self.current_source, intent=self.current_intent)
        if allow:
            if why == "auto mode":
                await self._narrate(detail)
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"Z.OS gate: {why}", interrupt=interrupt)

    async def _badge_on(self):
        if self.badge_id:
            return
        p = await asyncio.create_subprocess_exec(
            NOTIFY, "-u", "critical", "-t", "0", "-p",
            "Z.OS", "AUTO MODE — every action runs without asking",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        self.badge_id = out.decode(errors="replace").strip() or None

    async def _badge_off(self):
        if not self.badge_id:
            return
        p = await asyncio.create_subprocess_exec(
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.CloseNotification",
            self.badge_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()
        self.badge_id = None

    async def set_mode(self, auto: bool, source: str):
        if source != "user":
            audit(tool="_mode", detail=f"auto={auto}", verdict="deny",
                  reason="mode change from non-user source", source=source)
            return
        self.auto = auto
        audit(tool="_mode", detail=f"auto={auto}", verdict="allow",
              reason="user mode command", source=source)
        if auto:
            await self._badge_on()
        else:
            await self._badge_off()
            await asyncio.create_subprocess_exec(
                NOTIFY, "Z.OS", "guarded mode — asking before anything risky",
                stderr=asyncio.subprocess.DEVNULL)
```

Add the mode short-circuit inside `handle`, immediately after `source` is read and **before** the lock body touches the agent:

```python
        source = str(msg.get("source", "user"))
        wanted = match_mode(text)
        if wanted is not None:
            # Consumed here. The agent never sees this text, so no prompt-injected
            # page or file can talk it into widening its own permissions.
            await self.set_mode(wanted, source)
            return
        async with self.lock:
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /run/media/yash/External/Zerostic/Z.OS && .venv/bin/python test_zos.py`
Expected: `ok test_...` for all eleven tests, then `all passed`.

- [ ] **Step 5: Verify the prompt and the audit log by hand**

Restart the daemon, then:
```bash
./zos "create an empty file at /tmp/zos-gate-check"
```
Expected: a critical notification showing both `you said: create an empty file...` and the actual command, with Allow/Deny buttons. Click **Deny**; the file must not exist:
```bash
test ! -e /tmp/zos-gate-check && echo "correctly denied"
tail -2 ~/.local/share/zos/audit.log
```
Expected: a JSON line with `"verdict": "deny"`. Repeat and click **Allow**; confirm the file appears and the log says `allow`.

- [ ] **Step 6: Verify auto mode and its badge by hand**

```bash
./zos "auto"     # resident critical badge appears; no agent turn happens
./zos "create an empty file at /tmp/zos-auto-check"   # runs with no prompt
./zos "guarded"  # badge closes, confirmation notification appears
```

During the middle command, expect **two** notifications: a `Z.OS (auto)` line showing
the actual command that just ran, and the agent's own result. The first is the
narration — in sticky auto with no prompts and no expiry it is the only real-time
signal that something happened, so its absence is a bug, not cosmetic.

```bash
grep -c '"reason": "auto mode"' ~/.local/share/zos/audit.log
```
Expected: at least 1.

- [ ] **Step 7: Commit**

```bash
cd /run/media/yash/External/Zerostic/Z.OS
git add zosd.py test_zos.py
git commit -m "feat: fail-closed permission gate, daemon-side mode matcher, audit log"
```

---

### Task 5: systemd unit, sudo askpass, and the Super+Space keybinding

Folded into one task: all three are configuration whose only meaningful test is "the hotkey works from a cold boot," which needs all three present.

**Files:**
- Create: `zos.service`
- Create: `zos-askpass`
- Modify: `zosd.py` (nothing — the askpass path is injected by the unit's `Environment=`)

**Interfaces:**
- Consumes: Task 1's Q2 for the interpreter path inside `ExecStart`.
- Produces: a `zos.service` user unit whose `Environment=SUDO_ASKPASS=` is what makes the `sudo -A` instruction in `SYSTEM_APPEND` work.

- [ ] **Step 1: Write `zos-askpass`**

```bash
#!/usr/bin/env bash
# SUDO_ASKPASS helper. Z.OS cannot pass sudo's own gate, and should not — this
# hands the decision back to the OS's password prompt, which is unskippable.
exec zenity --password --title="sudo password (requested by Z.OS)"
```

```bash
chmod +x /run/media/yash/External/Zerostic/Z.OS/zos-askpass
```

- [ ] **Step 2: Write `zos.service`**

```ini
[Unit]
Description=Z.OS headless desktop agent
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=/run/media/yash/External/Zerostic/Z.OS
ExecStart=/run/media/yash/External/Zerostic/Z.OS/.venv/bin/python /run/media/yash/External/Zerostic/Z.OS/zosd.py
Environment=PYTHONUNBUFFERED=1
Environment=SUDO_ASKPASS=/run/media/yash/External/Zerostic/Z.OS/zos-askpass
Restart=always
RestartSec=2

[Install]
WantedBy=graphical-session.target
```

- [ ] **Step 3: Install and start the service**

```bash
mkdir -p ~/.config/systemd/user
ln -sf /run/media/yash/External/Zerostic/Z.OS/zos.service ~/.config/systemd/user/zos.service
systemctl --user daemon-reload
systemctl --user enable --now zos.service
systemctl --user status zos.service --no-pager
```
Expected: `active (running)`.

If the external drive is not mounted at login, systemd will restart-loop. In that case add `ConditionPathExists=/run/media/yash/External/Zerostic/Z.OS/zosd.py` under `[Unit]` and note it.

- [ ] **Step 4: Verify the restart-resets-to-guarded invariant on the live service**

```bash
./zos "auto"                       # badge appears
systemctl --user restart zos.service
sleep 2
./zos "create an empty file at /tmp/zos-restart-check"
```
Expected: a **prompt** appears — auto did not survive the restart. Deny it.

- [ ] **Step 5: Verify jobs survive a daemon restart**

```bash
./zos "start a background job named heartbeat that runs: while true; do date; sleep 5; done"
systemctl --user restart zos.service
sleep 2
tmux ls
```
Expected: the `heartbeat` session is still listed. tmux, not the daemon, owns job lifetime.

- [ ] **Step 6: Free Super+Space, then bind it**

GNOME ships `<Super>space` bound to input-source switching, which silently wins over a custom binding.

```bash
gsettings set org.gnome.desktop.wm.keybindings switch-input-source "[]"
gsettings set org.gnome.desktop.wm.keybindings switch-input-source-backward "[]"

K=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/zos/
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$K']"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$K" name 'Z.OS'
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$K" command '/run/media/yash/External/Zerostic/Z.OS/zos'
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$K" binding '<Super>space'
```

- [ ] **Step 7: Verify the hotkey end-to-end**

Press `Super+Space`. Expected: a `zenity` entry box. Type `notify me that the hotkey works` and press Enter. Expected: a notification. This is the full product path: hotkey, zenity, socket, warm agent, notification, with nothing else on screen.

- [ ] **Step 8: Verify sudo askpass**

```bash
./zos "check whether any apt updates are available, using sudo"
```
Expected: first the gate prompt for the command, then — after Allow — a `zenity` password box. Enter the password; expect a notification with the result. Confirm the audit log has both lines.

- [ ] **Step 9: Commit**

```bash
cd /run/media/yash/External/Zerostic/Z.OS
git add zos.service zos-askpass
git commit -m "feat: systemd user unit, sudo askpass helper, Super+Space keybinding"
```

- [ ] **Step 10: Record the install steps in the spec**

Append a short `## Install` section to `docs/superpowers/specs/2026-07-25-zos-daemon-design.md` containing the Step 3 and Step 6 command blocks verbatim, so the machine can be rebuilt without re-deriving them. Commit as `docs: record Z.OS install and keybinding commands`.

---

## Self-Review

**1. Spec coverage.**

| Spec requirement | Task |
|---|---|
| Not an OS; rides on Ubuntu GNOME | (no code — framing) |
| Python 3.14 risk, uv-3.12 fallback | 1 |
| Persistent `ClaudeSDKClient`, one session | 3 (Step 4 proves it) |
| Dumb stateless client, `{"source","text"}` | 3 |
| Client never parses | 3 (`zos` sends raw text) |
| `can_use_tool` is the entire gate | 1 (verified), 4 |
| Safe vs Guarded tool classes | 4 (`SAFE_TOOLS`, allow-list polarity) |
| No hard never-list | 4 (`sudo`/`rm -rf` prompt, allowed in auto) |
| Metacharacter check | 4 (`judge_bash`, `METACHARS`) |
| Read-only first-word allowlist | 4 (`SAFE_PREFIXES`, token-prefix match) |
| Guarded default mode | 3/4 (`self.auto = False`) |
| Auto sticky, never expires | 4 (`test_auto_mode_is_sticky_and_never_expires`) |
| Auto reverts on restart | 4 (fresh-state test), 5 (Step 4, live) |
| Resident critical badge while auto | 4 (`_badge_on`/`_badge_off`) |
| Audited in every mode | 4 (`audit` called on every verdict) |
| Auto narrates after the fact ("trades the veto, not the visibility") | 4 (`_narrate`, fired when `why == "auto mode"`; Step 6 verifies) |
| Explicit Deny means "nothing happens", not a retried variant | 4 (`prompt_user` returns `"deny"` vs `"fail"`; `interrupt=True` only on `"deny"`) |
| Mode matched by daemon, consumed, never forwarded | 4 (`handle` short-circuit before the lock) |
| Strict literal mode match | 4 (`test_mode_matcher_is_strict`) |
| `source` field gates auto mode | 4 (`test_auto_mode_only_applies_to_the_human_at_the_keyboard`) |
| Prompt shows intent + command | 4 (`prompt_user` two-line body) |
| Every failure path denies | 4 (`prompt_user` bare `except`, timeout kill) |
| sudo via `SUDO_ASKPASS` + zenity | 5 |
| NOPASSWD documented as opt-in | spec only — deliberately not automated |
| Socket `0600`, never TCP | 3 (Step 5 verifies) |
| Audit log at `~/.local/share/zos/audit.log` | 4 |
| Five custom tools | 2 |
| Clipboard/screenshot/launch via plain Bash | 3 (`SYSTEM_APPEND`), no wrapper tools |
| Daemon dies, systemd restarts, jobs survive | 5 (Step 5 verifies) |
| Second request queued, no concurrency layer | 3 (`asyncio.Lock`) |
| Prove hotkey end-to-end | 5 (Step 7) |
| All five spec tests | 4 (Step 1) |

No gaps. Two deliberate non-implementations, both stated: the NOPASSWD sudoers path stays documentation-only, and `app_launch` is dropped per the reconciliation note in Global Constraints.

**2. Placeholder scan.** Every code step carries runnable code. The only intentional fill-in-the-blank is `notes-sdk-findings.md`, whose angle brackets are the recorded *output* of Step 4, not deferred work — and Tasks 2/4 name the exact branch each answer selects. No "add error handling," no "similar to Task N," no "write tests for the above."

**3. Type and name consistency.** `judge_bash`, `match_mode`, `prompt_user`, `audit`, `_decide_fast`, `_render`, `set_mode`, `_narrate`, `_badge_on`, `_badge_off`, `NOTIFY`, `SOCK`, `AUDIT`, `SAFE_TOOLS`, `METACHARS`, `SAFE_PREFIXES`, `MODE_WORDS`, `ALLOWED_TOOLS`, `SYSTEM_APPEND`, `zos_server`, `sh`, `ok` are each defined once and spelled identically in every later reference and in `test_zos.py`. `_decide_fast` returns `(bool | None, str)` everywhere, and the tests index `[0]`. `prompt_user` returns `str` (`"allow"`/`"deny"`/`"fail"`) in its Interfaces block, its implementation, its single call site in `can_use_tool`, and its test — not `bool`. Custom tool names are `mcp__zos__*` in `SAFE_TOOLS`, `SYSTEM_APPEND`, and the tests, and bare in `tools.py`'s `@tool(...)` decorators — which is correct, since the SDK adds the prefix.
