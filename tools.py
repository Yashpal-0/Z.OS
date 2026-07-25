"""Z.OS host tools. Plain dicts and async callables — no SDK, no decorators.
Anything that is a one-line shell command (clipboard, gtk-launch, git) is left to
run_shell rather than wrapped."""
import asyncio
import json
import shlex

MAX_OUT = 4000        # truncate tool output so one `find /` cannot blow the context


async def sh(*argv: str) -> str:
    p = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return out.decode(errors="replace").strip()[:MAX_OUT] or f"exit {p.returncode}"


async def shell(cmd: str, timeout: int = 60) -> str:
    """Uses a real shell: pipes and redirection are most of a one-liner's value.
    That is exactly why the gate forces a prompt on any metacharacter."""
    p = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.kill()
        return f"timed out after {timeout}s (killed)"
    return out.decode(errors="replace").strip()[:MAX_OUT] or f"exit {p.returncode}"


# ---- handlers -------------------------------------------------------------

async def h_notify(a):
    await sh("notify-send", "Z.OS", a["text"])
    return "notified"


async def h_run_shell(a):
    return await shell(a["command"])


async def h_type(a):
    return await sh("ydotool", "type", "--", a["text"])


async def h_key(a):
    return await sh("ydotool", "key", *a["keys"].split())


async def h_click(a):
    btn = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}[a.get("button", "left")]
    await sh("ydotool", "mousemove", "-a", "-x", str(a["x"]), "-y", str(a["y"]))
    return await sh("ydotool", "click", btn)


async def h_job_start(a):
    return await sh("tmux", "new-session", "-d", "-s", a["name"], a["cmd"])


async def h_job_list(a):
    return await sh("tmux", "ls")


async def h_job_show(a):
    return await sh("gnome-terminal", "--", "tmux", "attach", "-t", a["name"])


async def h_job_kill(a):
    return await sh("tmux", "kill-session", "-t", a["name"])


# Verified 2026-07-25 against each CLI's --help. Do not guess these.
AGENT_FLAGS = {
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "aider": "--yes-always",
    "agy": "--dangerously-skip-permissions",
}


async def h_delegate(a):
    agent = a["agent"]
    if agent not in AGENT_FLAGS:
        return f"unknown agent {agent!r}; choose one of {sorted(AGENT_FLAGS)}"
    name = a.get("session") or f"zos-w-{agent}"
    cmd = f"{agent} {AGENT_FLAGS[agent]} {shlex.quote(a['task'])}"
    out = await sh("tmux", "new-session", "-d", "-s", name, cmd)
    return f"started tmux session {name!r} running {agent}: {out}"


HANDLERS = {
    "notify": h_notify, "run_shell": h_run_shell, "type": h_type, "key": h_key,
    "click": h_click, "job_start": h_job_start, "job_list": h_job_list,
    "job_show": h_job_show, "job_kill": h_job_kill, "delegate": h_delegate,
}

# Only these auto-allow. run_shell is conditional (see judge_shell in zosd.py) and is
# deliberately absent. Adding a name here disables the prompt for it — be sure.
SAFE = {"notify", "job_list"}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


SCHEMAS = [
    _fn("notify", "Show a desktop notification. This is the ONLY way the user sees "
                  "anything — always finish a request with it.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("run_shell", "Run one shell command on the host and return its output. For "
                     "quick system questions and one-liners, not multi-step work.",
        {"command": {"type": "string"}}, ["command"]),
    _fn("type", "Type text on the host keyboard, into whatever window has focus.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("key", "Press a host key combination, e.g. 'ctrl+alt+t' or 'super'.",
        {"keys": {"type": "string"}}, ["keys"]),
    _fn("click", "Click at absolute host screen coordinates.",
        {"x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]}},
        ["x", "y"]),
    _fn("job_start", "Run a long command in a detached tmux session. Returns "
                     "immediately; never blocks.",
        {"name": {"type": "string"}, "cmd": {"type": "string"}}, ["name", "cmd"]),
    _fn("job_list", "List running jobs and workers (tmux sessions).", {}, []),
    _fn("job_show", "Open a terminal attached to a running job so the user can watch.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("job_kill", "Stop a running job or worker.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("delegate", "Hand a multi-step CODING task to a worker CLI agent in a tmux "
                    "session. Use for anything involving editing files, writing code, "
                    "or running tests. Returns immediately.",
        {"agent": {"type": "string", "enum": ["codex", "aider", "agy"]},
         "task": {"type": "string", "description": "Full task description"},
         "session": {"type": "string", "description": "Optional tmux session name"}},
        ["agent", "task"]),
]


def render(name: str, args: dict) -> str:
    """One-line human description. Shown in the permission prompt and the audit log,
    so it must never be truncated for the tools where the detail IS the decision."""
    if name == "run_shell":
        return args.get("command", "")
    if name == "type":
        return f"type: {args.get('text', '')!r}"
    if name == "key":
        return f"press: {args.get('keys', '')}"
    if name == "click":
        return f"click {args.get('button', 'left')} at {args.get('x')},{args.get('y')}"
    if name == "delegate":
        return f"delegate to {args.get('agent')}: {args.get('task', '')}"
    return f"{name} {json.dumps(args, default=str)[:300]}"
