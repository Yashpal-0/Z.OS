"""Z.OS host tools. Plain dicts and async callables — no SDK, no decorators.
Anything that is a one-line shell command (clipboard, gtk-launch, git) is left to
run_shell rather than wrapped."""
import asyncio
import json
import os.path
import shlex

MAX_OUT = 80000       # truncate tool output so one `find /` cannot blow the context
SEND_SETTLE = 0.25    # pause between typing text and pressing a key; see h_job_send


async def sh(*argv: str) -> str:
    p = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return out.decode(errors="replace").strip()[:MAX_OUT] or f"exit {p.returncode}"


async def sh_rc(*argv: str) -> tuple[int, str]:
    """Like sh, but for the callers that must branch on success rather than show output.
    sh folds a silent success into the string "exit 0", which is truthy — so
    `if await sh(...)` reads a command that worked as a command that failed."""
    p = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace").strip()[:MAX_OUT]


async def shell(cmd: str, timeout: int = 1500) -> str:
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


async def h_job_read(a):
    """The pane as text, for the model. job_show opens a terminal for the human instead,
    which is no use when it is Z.OS that has to notice a worker asking a question."""
    return await sh("tmux", "capture-pane", "-p", "-t", a["name"])


async def h_job_send(a):
    """Drive a live session's input. `text` is sent literally; `keys` are tmux key names
    (Enter, Down, Escape, C-c, Tab). Both, in that order, so typing and submitting is one
    call — but Enter is never implied, because a TUI prompt answered with a bare `y` or an
    arrow key is broken by an extra newline, and Ctrl-C is not text at all."""
    name, sent = a["name"], []
    if text := a.get("text"):
        rc, err = await sh_rc("tmux", "send-keys", "-t", name, "--", text)
        if rc:
            return f"could not send text to {name}: {err or f'exit {rc}'}"
        sent.append(repr(text))
        # A TUI ingesting a paste has not committed it yet, and a submit key that arrives
        # mid-paste is swallowed. Observed with codex: the reply appeared on its input line
        # and simply sat there unsent, while the same Enter sent a moment later submitted
        # it. Wait like a human would between typing and pressing return.
        # ponytail: tune SEND_SETTLE up if a slower TUI still drops the key.
        if a.get("keys"):
            await asyncio.sleep(SEND_SETTLE)
    if keys := a.get("keys"):
        rc, err = await sh_rc("tmux", "send-keys", "-t", name, *keys.split())
        if rc:
            return f"could not send keys to {name}: {err or f'exit {rc}'}"
        sent.append(keys)
    return f"sent {' then '.join(sent)} to {name}" if sent else "nothing to send"


# Verified against each CLI's --help. Do not guess these.
#
# Workers run in their INTERACTIVE mode, deliberately. Z.OS drives a coding agent the way
# the user would: watch it, answer it, keep going. One-shot mode (`codex exec`, `aider -m`,
# `agy -p`) makes Z.OS a batch launcher, and turns every question the worker asks into a
# silent hang — including codex's "Do you trust the contents of this directory?", which is
# unanswerable in a detached session. Read the pane with job_read, reply with job_send.
#
# NO auto-approve flags (--dangerously-bypass-approvals-and-sandbox, --yes-always,
# --dangerously-skip-permissions). They were here when delegation was fire-and-forget and
# nobody could answer the worker. A live session can answer it, so bypassing the worker's
# own approval prompts would throw away the second checkpoint for nothing. And job_send is
# Guarded, so by default each answer is a decision the user sees.
#
# The bool is whether the task can be seeded on the command line. aider's positional
# arguments are FILENAMES, so passing the task there would hand it over as a file to open;
# it has to be sent with job_send once the session is up.
AGENT_FLAGS = {
    "codex": ("", True),
    "aider": ("", False),
    "agy": ("-i", True),          # --prompt-interactive: seed a prompt, keep the session
}


async def h_delegate(a):
    agent = a["agent"]
    if agent not in AGENT_FLAGS:
        return f"unknown agent {agent!r}; choose one of {sorted(AGENT_FLAGS)}"
    # Without -c the worker inherits the daemon's cwd — Z.OS's own repo — rather than the
    # directory the task is about. Refuse instead of defaulting: a coding agent with
    # write access pointed at the wrong tree is the expensive kind of mistake.
    cwd = a.get("cwd") or ""
    if not os.path.isdir(cwd):
        return f"delegate needs an existing directory to work in; got {cwd!r}"
    flags, seeds_task = AGENT_FLAGS[agent]
    name = a.get("session") or f"zos-w-{agent}"
    cmd = " ".join(
        [agent] + ([flags] if flags else [])
        + ([shlex.quote(a["task"])] if seeds_task else []))
    out = await sh("tmux", "new-session", "-d", "-s", name, "-c", cwd, cmd)
    started = f"started {agent} live in tmux session {name!r}, working in {cwd}: {out}"
    if not seeds_task:
        return (f"{started}\n{agent} takes no task on the command line — read the pane "
                f"with job_read and send the task with job_send once it is ready.")
    return (f"{started}\nIt is live: read the pane with job_read to see what it is doing "
            f"or asking, and answer with job_send.")


HANDLERS = {
    "notify": h_notify, "run_shell": h_run_shell, "type": h_type, "key": h_key,
    "click": h_click, "job_start": h_job_start, "job_list": h_job_list,
    "job_show": h_job_show, "job_kill": h_job_kill, "delegate": h_delegate,
    "job_read": h_job_read, "job_send": h_job_send,
}

# Only these auto-allow. run_shell is conditional (see judge_shell in zosd.py) and is
# deliberately absent. Adding a name here disables the prompt for it — be sure.
#
# job_read is here because reading a pane changes nothing. job_send is NOT, for the same
# reason `type` and `key` never are: keystrokes into a live session holding a coding agent
# with write access can do anything that agent can do, and "answer the worker's question"
# is exactly where a wrong answer costs the most.
SAFE = {"notify", "job_list", "job_read"}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


SCHEMAS = [
    _fn("notify", "Show a desktop notification. This is the ONLY way the user sees "
                  "anything — always finish a request with it.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("run_shell", "Run one shell command on the HOST machine and return its output. "
                     "Use only for tasks that need host-only state (host services, host "
                     "filesystem, host processes). Prefer vm_shell for everything else.",
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
         "cwd": {"type": "string", "description":
                 "Absolute path of the directory the worker should work in"},
         "session": {"type": "string", "description": "Optional tmux session name"}},
        ["agent", "task", "cwd"]),
    _fn("job_read", "Read what a live session currently shows, as text. Use this to see "
                    "what a worker is doing, or what question it is waiting on.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("job_send", "Send input to a live session — this is how you answer a worker or "
                    "drive any interactive terminal program. 'text' is typed literally; "
                    "'keys' are tmux key names such as Enter, Down, Escape, Tab, C-c. "
                    "Enter is never added for you, so pass keys='Enter' to submit. Read "
                    "the pane with job_read first to see what is being asked.",
        {"name": {"type": "string"},
         "text": {"type": "string", "description": "Literal text to type"},
         "keys": {"type": "string", "description":
                  "Space-separated tmux key names, e.g. 'Enter' or 'Down Down Enter'"}},
        ["name"]),
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
        # The directory is half the decision: the same task aimed at the wrong tree is a
        # different act entirely, and the prompt is the only place that is visible.
        return (f"delegate to {args.get('agent')} in {args.get('cwd')}: "
                f"{args.get('task', '')}")
    if name == "job_send":
        bits = [x for x in (args.get("text"), args.get("keys")) if x]
        return f"send to session {args.get('name')}: {' + '.join(bits)}"
    return f"{name} {json.dumps(args, default=str)[:300]}"
