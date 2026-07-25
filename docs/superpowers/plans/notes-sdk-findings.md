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

## U1 RESOLVED from primary docs (2026-07-25)

The deny probe run died on the session limit, so `deny` was not confirmed by execution.
It is confirmed by the SDK's own permission documentation
(`code.claude.com/docs/en/agent-sdk/permissions`), verbatim:

> **Hooks.** Run hooks first. A hook can deny the call outright or pass it on. A hook
> that returns `allow` does not skip the deny and ask rules below; those are evaluated
> regardless of the hook result.

> For checks that must run on every tool call, use a `PreToolUse` hook: hooks run before
> every other step, and **a hook deny applies even in `bypassPermissions` mode**.

And the documented six-step order, which is the reason `can_use_tool` was the wrong hook
point — five steps can approve a call before it is reached:

```
1. hooks          <- Z.OS gate; deny here always wins
2. deny rules     (disallowed_tools, settings.json)
3. ask rules      (settings.json)
4. permission mode
5. allow rules    (allowed_tools, settings.json)   <- keep empty
6. can_use_tool   <- last resort; MCP tools in practice
```

Also documented, and the direct explanation of Q4:

> **Auto-approved tools never reach `canUseTool`.** A tool call approved at any earlier
> step, by `acceptEdits` or `bypassPermissions`, or by an allow rule, skips your
> `canUseTool` callback, so permission checks you put there are silently bypassed for
> that tool.

**Still worth one empirical check when quota allows** (cheap, not load-bearing for the
design): re-run `/tmp/zos-hook-probe.py` run B and confirm the denied `echo hi` does not
execute. The design no longer depends on the outcome — the docs are explicit — but a
green run closes the loop. Task 4's `test_hook_denies_bash_when_the_prompt_fails` tests
the daemon's half of this (the hook returns `deny`) without any API call.

### Why `Bash` was pre-approved here — unexplained, and mitigated anyway

`can_use_tool` should have been reached for `Bash` in `default` mode with
`allowed_tools=[]`. It was not. Checked and ruled out: no `/etc/claude-code/managed-settings.json`,
no bypass-related env vars, and the only user allow-rule is `Bash(node .claude/*)`, which
`echo hi` does not match. The probe ran nested inside a Claude Code session
(`CLAUDECODE=1`, `CLAUDE_CODE_ENTRYPOINT` set), which is the likeliest source of an
inherited approval path. Unresolved, and deliberately not chased: the fix does not depend
on the cause. Hooks run at step 1, before anything that could have approved it, and
`setting_sources=[]` removes the inherited-settings vector regardless.

## Unverified — session limit hit

The probe's remaining two runs died on `You've hit your session limit · resets 1:20pm
(Asia/Kolkata)`. Their output is void, not negative:

- **U1 — can a `PreToolUse` hook actually DENY?** Resolved from primary docs above. One
  empirical confirmation still pending, no longer blocking.
- **U2 — does `setting_sources=[]` exclude settings-file allow rules?** Documented:
  *"These rules are read when the `project` setting source is enabled... If you set
  `setting_sources` explicitly, include `"project"` for them to apply."* So an explicit
  empty list excludes them. Not independently probed. Adopted regardless — it can only
  remove permission sources, never add one.

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
