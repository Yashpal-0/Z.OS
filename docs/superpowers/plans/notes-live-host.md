# Live host end-to-end — results

Task 4 of `2026-07-25-zos-operator.md`, run 2026-07-27 against `gemini-3.6-flash`
on the real desktop. Model `gemini-3.6-flash`, guarded mode unless stated.

## Verdict

Tool *choice* is correct on every intent tried, and the gate holds. Delegation
chose the right tool but did not execute — see Task 8 findings. **Three defects were
found, all in the paths that run when something goes wrong** — none of them in
the routing or the allow-list. One of them is a spec-level finding that killed
the designed prompt mechanism.

## Routing

| Intent | Tool chosen | Gate verdict | Right call? |
|---|---|---|---|
| "how much disk space is free?" | `run_shell` `df -h /` | `readonly allowlist` | yes, no prompt |
| "what is the hostname?" | `run_shell` `hostname` | `readonly allowlist` | yes, no prompt |
| "remember the number 41" | `notify` only | `safe class` | yes |
| "add one to the number you were told" | `notify` `The result is 42.` | `safe class` | yes — history works |
| "create an empty file at /tmp/…" | `run_shell` `touch …` | prompted | yes |
| "add a --verbose flag to the CLI in …/stage0 and run its tests" | `delegate` to `codex` | prompted | tool choice yes — coding work delegated, not shelled. **Execution failed**, see Task 8 findings |

No intent routed wrongly, so `SYSTEM` needed no changes. `delegate` was chosen for
the coding task without hesitation, which was the open architectural question when
this design replaced the SDK one.

Session persistence confirmed: "remember 41" then a separate invocation asking to
add one returned **42**. A fresh process per request could not do that.

Mode isolation confirmed: `auto` then `guarded` produced exactly two `_mode` audit
lines and no model call between them. (The plan's suggestion to check `zosd.log`
for absent HTTP traffic proves nothing — the daemon logs no HTTP either way. The
audit-line count is what carries this, and `test_mode_command_never_reaches_the_model`
pins it properly by spying on `route`.)

## Defect 1 — the daemon could fail completely silently

`handle()` swallowed every exception from `route()` into a notification, and
`route()`'s return value was discarded. A failure before the first tool call
therefore wrote:

- no audit line (those record gate verdicts, and no gate had run yet),
- nothing to `zosd.log` (0 bytes),
- nothing the operator could read afterwards.

A dead request was indistinguishable from one that never arrived. It cost a full
live debugging session to find, which is the whole argument for fixing it.

Fixed in `6d27f78`: the traceback is printed and flushed, and `handle()` tracks
whether `notify` actually fired — if `route()` returns text and it did not, the
daemon sends that text itself. A model that answers in prose instead of calling
`notify` is no longer a silent no-op.

## Defect 2 — a failing prompt raised instead of returning

```
File "zosd.py", line 105, in prompt_user
    proc.kill()
PermissionError: [Errno 13] Permission denied
```

`prompt_user`'s timeout handler called `proc.kill()` inside its own `except`
block. When that raised, the exception escaped `prompt_user` entirely, so `_gate`
never produced a verdict and `route()` died. Fail-closed by accident — nothing
ran — but nothing was recorded either.

The `EPERM` itself was an artefact of the sandbox this bring-up ran in, which
denies signals to processes outside it; under systemd the kill would have
succeeded. That is exactly why it must not be relied on. `prompt_user`'s contract
is that it returns `"allow"`, `"deny"` or `"fail"` and never raises.

Fixed in `1f83ad6`. `PROMPT_TIMEOUT` became a module constant so the timeout path
is testable without a 60-second wait.

## Defect 3 — notification action buttons do not exist on GNOME 49 (spec-level)

The design specified a prompt with no window: `notify-send -u critical -A
allow=Allow -A deny=Deny -w`. On this desktop that **accepts the actions over
D-Bus, exits successfully, and never renders a button.**

Evidence: three prompts sat in process state `Sl` waiting for a D-Bus reply that
no UI existed to send. `notify-send 0.8.8`, `show-banners: true`, so Do Not
Disturb was not involved. The user confirmed seeing the notifications *with no
buttons on them*. Every prompt in guarded mode therefore timed out — guarded mode
was unanswerable.

This is the same class of failure as the Agent SDK's `can_use_tool`: an API that
accepts input, reports success, and silently does not do the thing. Only an
end-to-end human click proves a permission prompt works.

Fixed in `45a405c` by moving prompts to `zenity --question` with `--ok-label
Allow --cancel-label Deny`. zenity was already a dependency (the `zos` client uses
`zenity --entry`), and a dialog is a real window that cannot be silently dropped.
Verified by a human click returning `exit=0`.

**Exit-code mapping is now the security boundary.** zenity returns 1 for both Deny
and a closed window, 5 for its own `--timeout`, and other codes for other states.
Only 0 may mean allow. `test_only_exit_zero_is_an_allow` pins all six.

After the fix, live Step 3 passed correctly: one dialog, Deny clicked,
`"verdict": "deny", "reason": "user denied"`, file not created, and the model
reported the block rather than retrying.

## Finding — the model sometimes escalates around a denial

Observed once, in an in-process reproduction with an empty history. One intent,
one denial, three escalating attempts:

```
PROMPT FIRED for: touch /tmp/zos-gate-check
PROMPT FIRED for: python3 -c "open('/tmp/zos-gate-check', 'w').close()"
PROMPT FIRED for: job_start {"name": "create_file", "cmd": "touch /tmp/zos-gate-check"}
```

The gate held — all three were denied and nothing ran. But two things follow:

1. **A Deny the model can retry around is not a Deny.** It converts one decision
   into a queue of dialogs, and prompt fatigue eventually produces an Allow.
2. **`job_start` is a shell-command path that bypasses `judge_shell`.** The third
   attempt used it deliberately. It is gated (`job_start` is not in `SAFE`, so it
   prompts), but it means `judge_shell` is not the only way a command reaches a
   shell.

Not reproduced on the live run, where the model accepted the denial and notified
instead — so this is non-deterministic, not guaranteed.

**Not fixed here.** Task 4's scope is verification of Tasks 1-3; making a denial
terminal for the rest of a request is a change to the permission model and gets
its own commit and its own test. Intended shape: a per-request flag set when
`_gate` sees `why == "user denied"`, after which only `self.safe` tools are
permitted. Gate on `"user denied"` only, not `"prompt failed"` — the existing
`test_blocked_tool_never_executes_and_the_model_is_told` drives its denial through
a missing prompt binary and so produces `"prompt failed"`. `notify` must stay
reachable, or a Deny becomes silent, which is worse than the escalation.

## Findings for Task 8 (delegation)

Step 7 was meant to be denied; Allow was clicked instead, so delegation ran for
real. Nothing was damaged — `codex` never executed anything — but it exposed two
defects that Task 8 must handle:

1. **`delegate` never sets a working directory.** The worker inherits the daemon's
   cwd (`Z.OS`), not the directory the task is about. `tools.h_delegate` needs a
   `cwd` argument passed through to `tmux new-session -c`.
2. **`--dangerously-bypass-approvals-and-sandbox` does not make `codex`
   non-interactive.** It still asks `Do you trust the contents of this directory?`
   on first run and waits. In a detached tmux session nobody is watching, the
   worker hangs there forever and Z.OS reports success.

The second is the concrete form of a risk already noted when acknowledging the
commit security review: a worker that re-prompts inside a detached session does
not add safety, it adds a silent hang. Z.OS is the gate; the worker must be
genuinely non-interactive, and `delegate` should verify the session is still alive
and progressing rather than trusting that `tmux new-session` returning 0 means
work is happening.

## VM tier, live (Task 6 step 5)

Both checks pass against the running guest.

`what is on the sandbox VM screen right now?` — one `vm_see`, audited as
`"tier": "vm"`, `"reason": "safe class"`, no prompt. The model read the frame
correctly: an `Ubuntu 24.04.4 LTS zos-guest tty1` login prompt, which is what a
headless cloud image shows. This was the first live exercise of the
`__ZOS_IMAGE__` branch — the QMP PPM dump, the ffmpeg conversion and the
multimodal `image_url` message shape had all been untested against the real API.

`in the sandbox VM, create a file /tmp/from-zos containing the date, then show me
it worked` — one `vm_shell` running `date > /tmp/from-zos && cat /tmp/from-zos`,
again Safe with no prompt. Confirmed independently over SSH: the guest file holds
`Sun Jul 26 20:42:07 UTC 2026`, byte-for-byte what the daemon reported.

Two defects surfaced while verifying, both fixed:

1. **The converted PNG was left world-readable in `/tmp`.** The PPM qemu writes is
   0600 because the unit sets `UMask=0077`, but ffmpeg ran under the daemon's own
   umask, so the PNG landed 0664 — a picture of the guest screen readable by any
   local user. The file was only ever read back to base64 it, so it does not need to
   exist: it now goes down a pipe, which removes the file, the permission mismatch
   and the chmod race together.
2. **The test suite wrote to the real audit log.** Every test reaching `_gate`
   appended to `~/.local/share/zos/audit.log`. A trail the tests forge is not an
   audit trail, and because the two audit assertions count lines, a live daemon
   appending at the same time could break them. `AUDIT` is now redirected once for
   the whole run, guarded by `test_the_suite_never_writes_the_real_audit_log`.

One operational note, not a defect: the daemon reads `GEMINI_API_KEY`, while the
existing key lives in `stage0/.env` as `ZEROOS_API_KEY`. Starting the daemon by
sourcing that file alone yields `httpx.LocalProtocolError: Illegal header value
b'Bearer '` on the first request. The plan already specifies the rename into
`~/.config/zos/env`; Task 7 is what makes it automatic.

## Test suite

41 assertions, all offline, `.venv/bin/python test_zos.py`. Three were added
during the host run, each pinning a defect above: `test_a_tool_less_reply_still_reaches_the_user`,
`test_prompt_timeout_returns_fail_and_never_raises`, `test_only_exit_zero_is_an_allow`.
`test_auto_mode_narrates_the_command_it_ran` was also added, because the audit line
proves only the verdict — the narration is a separate fire-and-forget subprocess
and needed its own check rather than a question to the user.

## Task 7 — unit, askpass, hotkey

The unit works: `active (running)`, the key arrives via `EnvironmentFile=`, and a live
request routed and notified. `graphical-session.target` is active and pulled by
`gnome-session.target`, so login autostart will work. Two defects in the planned unit,
both fixed and both verified empirically rather than by reading:

1. **`ConditionPathExists` was in `[Service]`, where systemd ignores it.** The journal
   said `Unknown key 'ConditionPathExists' in section [Service], ignoring` once at start
   and the unit ran happily without the guard it was supposed to have. Moved to `[Unit]`.
   Proven with a drop-in pointing at a missing path: the unit is now
   `skipped, unmet condition check` and goes **inactive, not failed**, which is what
   actually stops the `Restart=always` loop when the external drive is not mounted.
2. **Jobs did not survive a restart.** `tmux ls` returned `no server running` and a
   running job was destroyed. The plan assumed "tmux and QEMU own their own lifetimes";
   that holds for QEMU, which has its own unit, but not for tmux. tmux double-forks and
   so escapes the *process tree*, but cgroup membership is inherited at `fork()` and is
   unaffected by reparenting — systemd's own listing showed the tmux server inside
   `zos.service`'s cgroup, where the default `KillMode=control-group` killed it. Fixed
   with `KillMode=process`; verified the job now survives a restart and keeps ticking.

Restart invariants hold. After `./zos "auto"` and a restart, the next request audited as
`"mode": "guarded"` with `"reason": "user allowed"` — a prompt appeared, so auto did not
survive. Had it survived, the reason would read `"auto mode"`.

### A consequence worth a decision

`Restart=always` plus "startup mode is ALWAYS guarded" means an unnoticed crash now
silently flips auto back to guarded mid-session. Before this task a restart was always
deliberate. That is in tension with the standing "auto sticky until I say guarded", so
it is recorded here rather than changed — narrowing either rule is the user's call, not
something to quietly pick.

### Hotkey: unresolved

The gsettings configuration is correct and persists across processes, GNOME Shell 50.1
on Wayland, `gsd-media-keys` running — but a synthetic `<Super>space` (and `<Super>F9`,
to rule out a conflict) does not launch the client.

Two owners of `<Super>space` were found and freed, one of which the plan missed entirely:
GNOME's `switch-input-source` **and** `org.freedesktop.ibus.general.hotkey triggers`.
Freeing both did not make it fire, so the ibus grab was a real defect in the install
steps but not the cause here.

What is proven: a synthetic `<Super>` press *does* open the overview
(`OverviewActive` false→true), so synthetic input reaches mutter's internal keybindings.
That says nothing about custom keybindings, which `gsd-media-keys` grabs over
`org.gnome.Shell.GrabAccelerators` — a different registration path, and the one still
untested. `org.gnome.SettingsDaemon.MediaKeys.service` refuses manual start/stop, so it
cannot be restarted by hand to re-register.

Awaiting a physical press to discriminate: binding never fires, versus binding fires and
the spawned command fails. The bound command is temporarily wrapped to log to
`/tmp/zos-hotkey.log` so one press answers it. Steps 8 and 9 both need a human at the
keyboard anyway.
