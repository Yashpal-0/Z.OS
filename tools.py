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
