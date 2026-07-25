#!/usr/bin/env python3
"""Z.OS daemon. One socket, one persistent agent session, one permission gate."""
import asyncio
import json
import os
import pathlib

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
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

# Task 1 proved a bare allowed_tools entry auto-approves that tool BEFORE the gate
# runs. This list must stay empty forever. Unlisted tools are still available to the
# agent, so empty costs nothing. Adding an entry silently disables the gate for it.
ALLOWED_TOOLS: list[str] = []


class Daemon:
    def __init__(self):
        self.auto = False          # startup mode is always guarded
        self.current_source = "user"
        self.current_intent = ""
        self.badge_id = None
        self.lock = asyncio.Lock()
        self.client = None
        self._last_verdict = (None, None)   # (tool_use_id, verdict) — see _resolve

    # Both gate entry points are permissive stubs here; Task 4 replaces them.
    async def pre_tool_use(self, input_data, tool_use_id, context):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "allow"}}

    async def can_use_tool(self, tool_name, input_data, context):
        return PermissionResultAllow()

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
            allowed_tools=ALLOWED_TOOLS,      # always [] — see the constant
            setting_sources=[],               # inherit no user/project allow-rules
            permission_mode="default",
            hooks={"PreToolUse": [HookMatcher(matcher=None,
                                              hooks=[self.pre_tool_use])]},
            can_use_tool=self.can_use_tool,   # second layer, MCP tools
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
