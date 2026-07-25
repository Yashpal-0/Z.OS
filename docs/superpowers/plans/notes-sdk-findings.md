# Task 1 findings — recorded 2026-07-25

Probed with `/tmp/zos-smoke.py`, `/tmp/zos-bash-probe.py`, `/tmp/zos-hook-probe.py`
(throwaway, not committed). Environment: Ubuntu GNOME Wayland, system Python 3.14.4,
`claude-agent-sdk` in `.venv/`.

## Answers

- **Q1 SDK imports:** yes. `import claude_agent_sdk` succeeds on Python **3.14.4**.
  The spec's flagged anyio/3.14 risk did not materialise.
- **Q2 interpreter:** `.venv/bin/python` (3.14.4). No `uv`, no 3.12 pin. Plan Task 1
  Step 2 is moot.
- **Q3 `allowed_tools=[]`:** gate fired: **yes** for `mcp__zos__ping`; MCP tool
  reachable: **yes**. So in-process MCP tools are available without being listed, and
  the callback sees them. This is the plan's **Branch A** for MCP tools.
- **Q4 `allowed_tools=["mcp__zos__ping"]`:** gate fired: **no**. Listing a tool
  auto-approves it *before* the callback is consulted. The SDK says so explicitly:

  ```
  CanUseToolShadowedWarning: can_use_tool will not be invoked for: mcp__zos__ping.
  An allowed_tools entry that allows a whole tool auto-approves it before the callback
  is consulted. To gate every tool call, use a PreToolUse hook; or narrow the entry so
  calls fall through to can_use_tool. Allow rules from settings files can also shadow
  the callback but are not visible here.
  ```

  Consequence: `allowed_tools` must stay **empty**. It is not a convenience list; it is
  a hole in the gate.
- **Q5 `Bash` with `allowed_tools=[]`:** gate fired: **NO**. This is the plan's
  hard-stop condition. `echo hi` executed and returned `hi` with `can_use_tool` never
  invoked. Reproduced twice: with default tools, and with explicit `tools=["Bash"]`.
  No `allowed_tools` entry, no warning emitted — the callback is simply not on the path
  for this built-in.

## Consequence: the spec's permission model is wrong as written

The spec claims *"the `can_use_tool` callback is the entire gate — the only path to any
tool, so nothing routes around it."* **That is false for built-in tools.** `Bash` — the
single most dangerous tool and the gate's primary consumer — bypasses it entirely.

Verified replacement: a **`PreToolUse` hook** does see `Bash`.

```
HOOK FIRED: Bash {'command': 'echo hi', 'description': 'Echo hi'}
=== A hook=allow
  hook fired for:     ['Bash']
  callback fired for: []
```

Hook shape (from `HookMatcher` signature and `PreToolUseHookInput`):

```python
async def pre_tool_use(input_data, tool_use_id, context):
    input_data["tool_name"]   # "Bash"
    input_data["tool_input"]  # {"command": "echo hi", ...}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" | "deny" | "ask",
        "permissionDecisionReason": "...",
    }}

ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_tool_use])]})
```

`HookMatcher(matcher=...)` takes a tool-name pattern; `matcher=None` matches every tool.

## Unverified — session limit hit

The probe's remaining two runs died on `You've hit your session limit · resets 1:20pm
(Asia/Kolkata)`. Their output is void, not negative:

- **U1 — can a `PreToolUse` hook actually DENY?** The hook demonstrably *fires* and
  *sees* the command. Whether `permissionDecision: "deny"` blocks execution is
  **NOT VERIFIED**. This is load-bearing: the whole design rests on it.
- **U2 — does `setting_sources=[]` stop settings-file allow rules from shadowing the
  callback?** Not verified. Relevant because `~/.claude/settings.json` on this machine
  carries `allow: ["Bash(node .claude/*)"]`, and a daemon inheriting user settings
  inherits that hole. Belt-and-braces regardless: set `setting_sources=[]` so `zosd`
  never reads user/project/local settings.

Re-run `/tmp/zos-hook-probe.py` after the limit resets to settle U1 and U2 before
Task 3 wires the real gate.

## Decision for Task 2

Q3 is "gate fired: yes, reachable: yes" for MCP tools, so **Branch A** holds for
`tools.py`: keep all five tools, `allowed_tools=[]`. No tools are dropped, no tmux
work moves to plain `Bash`.

## Decision for Tasks 3 and 4 — plan amendment required

1. **`allowed_tools=[]`, always.** Never list a tool. Listing = pre-approval = bypass.
2. **`setting_sources=[]`.** Do not inherit user/project/local allow rules.
3. **The gate is a `PreToolUse` hook with `matcher=None`**, not `can_use_tool`.
   The hook sees every tool call including `Bash`; the callback does not.
4. **Keep `can_use_tool` as a second layer** for MCP tools (verified working). Defence
   in depth: if a future SDK version changes hook dispatch, the callback still covers
   the custom tools.
5. **The fail-closed default becomes `"deny"`**, returned from the hook, and the
   audit/prompt/mode logic moves behind the hook. `_decide_fast`, `judge_bash`,
   `match_mode`, `prompt_user` and `audit` are unaffected — only the SDK entry point
   they hang off changes.
6. **Gate polarity constraint still holds and matters more:** `matcher=None` plus an
   allow-list of Safe tool names means an unrecognised tool name lands on deny.
