# Z.OS — Two-Tier Desktop Operator

**Date:** 2026-07-25
**Status:** Design approved
**Supersedes:** `2026-07-25-zos-daemon-design.md` (Claude-Agent-SDK design)

## What this is (and is not)

Z.OS is **not an operating system** and **not a coding agent**. It is a persistent,
headless **operator** — a background user service that does what you would do at a
machine. It types, clicks, runs commands, reads the screen, and hands real coding work to
whichever CLI agent you already have.

It operates **two machines**:

- **Host tier** — your live Ubuntu GNOME desktop. Gated, because a mistake here is
  unrecoverable.
- **VM tier** — a QEMU/KVM Ubuntu guest that Z.OS owns completely: it reads the guest
  framebuffer directly, injects input the guest cannot refuse, and snapshots before
  anything risky. Ungated by default, because it is disposable.

Core thesis: **one persistent operator, reachable in ~200ms, that uses a machine the way
you do — cautiously on the one you care about, freely on the one it owns.**

The routing brain is Gemini 3.6 Flash. Z.OS **decides and operates**; it does not author.
A one-shot answer (`df -h`) it does itself. A multi-step coding task it delegates to
`agy`, `codex`, or `aider` in a tmux session, then reports.

## Why two tiers

The tier split is not a preference — it falls out of a hard technical wall, verified on
this machine 2026-07-25.

**On the host, reading the screen directly from the buffer is impossible, and privilege is
not the lever.**

- `/dev/fb0` is `root:video` and holds no composited desktop anyway — modern GNOME renders
  through DRM/KMS with GPU-composited planes; there is no legacy framebuffer to read.
- The real pixels live in GPU memory owned by whoever holds **DRM master** — GNOME Shell.
  A second process cannot read another's DRM master framebuffer *even as root*. That is
  kernel-enforced ownership, not a permission bit.
- `org.gnome.Shell.Screenshot` and `Introspect.GetWindows` both return `AccessDenied`;
  GNOME 49 restricts them to sanctioned callers. Running as root is **worse** — root has a
  different session bus and cannot reach `org.gnome.Shell` at all.

**In a VM, the same capability is trivial**, because Z.OS owns the hypervisor. Verified by
running a real guest and issuing the calls:

| QMP call | Result |
|---|---|
| `screendump` | **OK** — wrote a 720×400 rawbits pixmap, 864 KB, straight from the emulated framebuffer |
| `input-send-event` | **OK** — key injection the guest cannot refuse |
| `snapshot-save` / `snapshot-load` | present — mistakes revertible in seconds |
| `human-monitor-command` | present — escape hatch for anything QMP lacks |

`/dev/kvm` is usable with **no sudo** (user is in the `kvm` group); `qemu-system-x86_64` is
10.2.1.

The privilege inversion comes from changing *which machine is the target*, not from
escalating on this one. It also removes the security tension: the gate exists because host
mistakes are permanent, and snapshots mean VM mistakes are not.

## Why the previous design died

The first spec put a `claude-agent-sdk` session at the centre. Task 1 probing killed it on
two counts, recorded in `docs/superpowers/plans/notes-sdk-findings.md`:

1. **The gate was unsound.** `can_use_tool` is the *last* of six permission-evaluation
   steps and never fires for `Bash` at all — `echo hi` executed with the callback blind to
   it. A `PreToolUse` hook patches it, but only by layering a workaround on a permission
   model we don't own.
2. **The wrong brain.** The Agent SDK *is* Claude Code: it spawns the `claude` CLI as its
   transport, so it cannot be pointed at another provider, and every routing decision
   burns subscription quota on the most expensive model available. We exhausted the
   session limit during Task 1.

Both dissolve in one move. Z.OS owns its own tool loop, so **the gate is unbypassable by
construction** — every call passes through one function because we wrote the loop. And
routing runs on Flash, cheap enough to hit dozens of times a day.

Routing verified with real tool output (not stubs — an earlier probe fed `"ok"` back and
produced garbage, which was the probe's bug):

| Intent | Routed to | Correct |
|---|---|---|
| "how much disk space is free?" | `run_shell` (`df -h /`) → `notify` | ✅ |
| "add a --verbose flag to the CLI in …/stage0 and run its tests" | **`delegate(codex, …)`** → `notify` | ✅ |
| "tell me the time and the hostname" | one `run_shell` → `notify` | ✅ |

It also chose the `notify` *tool* over shelling `notify-send`, keeping guarded and safe
calls distinguishable. First try, no prompt tuning.

## Target environment (probed 2026-07-25)

- Ubuntu GNOME, Wayland, GNOME Shell 49. Python **3.14.4**. `httpx` 0.28.1 installed —
  the only HTTP dep needed.
- Present: `tmux`, `notify-send` (libnotify 0.8.8 — text works; **`-A` action buttons are
  accepted and never rendered on GNOME 49**, so prompts use `zenity --question`), `zenity`,
  `wl-copy`/`wl-paste`, `gtk-launch`, `socat`, `systemctl`, `ffmpeg`, `gdbus`, `wmctrl`,
  `qemu-system-x86_64` 10.2.1, `docker`.
- **`ydotool` + `ydotoold` working.** `ydotoold` running, user in `input` group,
  `/dev/uinput` writable with **no sudo** — real keyboard and pointer injection on native
  Wayland, which the previous spec wrote off as out of scope.
- **`/dev/kvm` usable without sudo.** Hardware virtualization available.
- Worker CLIs: `agy`, `codex`, `claude`, `cursor-agent`, `aider`.
- Missing, and staying missing: `virsh`/`libvirt` (bare QEMU + QMP instead), `wtype`,
  `grim`/`slurp`, `fuzzel`/`wofi`/`rofi` (hence `zenity --entry`), Python D-Bus bindings.
- **Blocked on the host regardless of privilege:** screenshot, window list, focus query.
  See "Why two tiers."

### The capability split

Not privileged vs unprivileged. **Whose machine it is.**

| Capability | Host | VM |
|---|---|---|
| Type / key / click | ✅ `ydotool` → `/dev/uinput` (kernel) | ✅ QMP `input-send-event` |
| Shell, files, processes | ✅ user | ✅ via SSH into guest |
| Read the screen | ❌ compositor-owned, impossible | ✅ QMP `screendump`, direct framebuffer |
| Window list / focus | ❌ `AccessDenied`, no portal | ✅ implicit in the frame |
| Undo a mistake | ❌ nothing | ✅ `snapshot-load` |
| `sudo` | `SUDO_ASKPASS` → zenity | free — root in the guest |

`ydotool` is invisible to GNOME's policy layer: at `/dev/uinput` level it *is* a keyboard,
indistinguishable from the user's. That is why the hands work on the host while the eyes
do not.

## Architecture

```
 Super+Space ──> zenity --entry ──┐
                                   ├──> $XDG_RUNTIME_DIR/zos.sock (0600)
 later: voice, cron, inotify ─────┘              │
                                                 v
                                        zosd  (systemd --user)
                                  router loop: Gemini 3.6 Flash, tool calling
                                                 │
                                        ┌────────┴────────┐
                                        v                 v
                                  permission gate     audit log
                                        │
              ┌─────────────────────────┴──────────────────────┐
              v                                                v
     ── HOST TIER (gated) ──                        ── VM TIER (owned) ──
   run_shell   type/key/click                     vm_see    (screendump)
   job_*       delegate(agy/codex) ──┐            vm_type   vm_key  vm_click
   notify      job_read / job_send <─┘  live loop  vm_shell  (ssh)
                                                   vm_snapshot / vm_restore
```

### Four moving parts

**1. `zosd`** — Python daemon, systemd `--user`. Owns the socket, the conversation
history, and **its own tool loop**. Every tool call is dispatched by our code. Draws
nothing.

**2. Clients** — dumb, stateless, one line each. `zenity --entry | socat - $SOCK`, pushing
`{"source": "user", "text": "..."}`. Voice and cron clients later write to the same
socket. **Clients never parse the user's text.**

**3. Jobs and workers = tmux sessions.** The operator never blocks. `job_start` and
`delegate` spawn detached sessions and return immediately. tmux supplies — free, zero code
— persistence across daemon restarts, scrollback-as-log, `tmux ls` as status, `attach` as
viewer, `kill-session` as stop. It also supplies the two halves of driving a *live* session:
`capture-pane` reads it (`job_read`) and `send-keys` types into it (`job_send`), which is
how Z.OS answers a worker's questions instead of deadlocking on them.

**4. The VM** — one long-lived QEMU/KVM guest with a QMP Unix socket. Started by its own
systemd unit so it survives daemon restarts, like tmux jobs do.

### Message flow

hotkey → zenity → socket → daemon → Gemini decides which tools realize the intent → gate →
tools fire → `notify` with the result. Long work → tmux session, return, notify on
completion.

### Input is plain English

The client sends raw text and never parses it. The **model** decides which tools realize
the intent, including *which tier* — "check my disk space" is host, "try this installer
somewhere safe" is VM. One exception: mode commands (below), matched by the daemon *before*
the model sees the text.

## The router loop

~60 lines. OpenAI-compatible `chat/completions` against
`https://generativelanguage.googleapis.com/v1beta/openai`, model `gemini-3.6-flash`, via
`httpx`. Loop: post messages + tool schemas → model returns `tool_calls` → **gate each
one** → execute → append `{"role": "tool", "tool_call_id": ..., "content": ...}` → repeat
until a tool-less response, capped at 12 iterations.

Key and model URL come from the environment, injected by the systemd unit, never
committed. `ZeroOS/stage0/.env` is the existing precedent and holds a working
`GEMINI_API_KEY`.

**Conversation persistence** is what makes it one entity rather than a fresh shell each
time: the message list lives in the daemon across invocations, trimmed to the last N
exchanges. Resets on restart, like the mode.

**Vision is on demand.** Flash is multimodal, so a captured frame goes in as an image
part — but only when the model calls `vm_see`. An unconditional screenshot per request
would cost latency and tokens for the ~90% of intents that are pure shell.

**The cap is not a formality — it is load-bearing.** Observed live: after finishing a VM
task correctly at step 4 (create a file, snapshot, confirm), the model did not stop. It
spent its remaining steps on unrelated *host* introspection — `ps aux`, `tmux ls`,
`ls -l /proc/<pid>/cwd`, and a recursive `grep` through **Z.OS's own repository** — until
`MAX_STEPS` ended it. Nothing was destructive and the gate prompted for each, but note what
this means by mode:

- **guarded** — a run of consecutive prompts for commands the user never asked for. Annoying,
  and worse, it trains the reflex of clicking Allow.
- **auto** — every one of those runs unprompted. Wandering is not a hypothetical risk there;
  it is the observed default behaviour of the model on a finished task.

**The repeat guard.** `MAX_STEPS` was the only bound, and 12 steps of unrequested host
commands is a poor one. Ending the loop on a `notify` was the obvious idea and is wrong
twice: the observed wander never called `notify` at all, and a task like *"tell me X, then
do Y"* would be silently truncated half-done.

What the trace does show is a structural marker — the model re-issued
`vm_snapshot {"name": "checkpoint"}`, byte-identical to a call it had already made, and
every wandering command came after it. A model repeating a state-changing call with
identical arguments is no longer making progress. So: **an identical `(tool, arguments)`
pair within one request ends the loop.**

`POLLABLE = {job_read, vm_see, vm_status, job_list}` is exempt, because repeating those
*is* the work — `job_read` polling one pane is the documented delegation loop, and a guard
that broke it would be worse than the wander.

The stop happens **after the current batch finishes, never mid-batch.** Returning early
would leave an assistant message carrying `tool_calls` with no matching `tool` results, and
that malformed pair lands in `self.history` and corrupts the *next* request — a bug that
would surface one request later than its cause. A test asserts every `tool_call_id` left in
history has an answer.

**The same corruption had a second door: the history trim.** Guarding the batch boundary
does nothing about `MAX_HISTORY`, and `self.history = msgs[1:][-MAX_HISTORY:]` cut wherever
the count happened to land. Because the model batches several calls per step — and a
`vm_see` inserts an extra `user` image message mid-batch — the cut could fall *between* an
assistant's `tool_calls` and the `tool` messages answering them, leaving a history that
opens on a result whose call is gone. It is the mirror image of the mid-batch bug: that one
truncates the head of a group, this one truncates its tail from the other side.

Confirmed rather than reasoned about. A randomised sweep of realistic shapes (1-3 calls per
step over 6-12 steps) orphaned a result in **~3%** of histories, and one real API call with
a hand-built orphan returned **`400 INVALID_ARGUMENT`,
`function_response.name: Name cannot be empty`** — so the endpoint does reject it rather
than tolerating it.

Two properties made this worth fixing ahead of anything else outstanding:

- **Delayed.** The bad slice is written at the end of a long request that *succeeds*. The
  next request is the one that fails, so the symptom points at innocent work.
- **Permanent.** `route()` raises inside `_call_model`, before any `self.history = ...`
  assignment, so the poison is never overwritten. Every later request rebuilds the same
  messages and fails identically — Z.OS goes deaf until the daemon is restarted, with
  `model HTTP 400` as the only clue.

**Dropping leading `tool` messages is not sufficient, and believing it was is the more
useful half of this finding.** `vm_see` appends its screenshot as a `user` message directly
after the result it belongs to, so a batch can read `assistant(a,b,c)`, `tool a`,
`user(image)`, `tool b`, `tool c`. That image message is a *barrier*: skipping only leading
`tool`s stops on it and leaves `b` and `c` orphaned behind it with their assistant still
cut. Both nearby cut points failed — landing on `tool a` and landing on the image message
each left `['b','c']`. The first fix passed its own test and the randomised sweep because
the sweep's generator appended the image after *every* result in a step, which is not the
shape `route()` actually builds.

So `_trim()` advances the head to the first message that can legitimately *begin* a
history: an `assistant`, or a real `user` turn. The image message is told apart from a real
turn by content type — `route()` builds it as a list of parts, a user turn is always a
plain string. Everything before that point is a fragment of a group whose opener is gone,
and is dropped whole. The tail needs no such care.

Closed with the instrument that opened it: the same history that returns **400** through
the old blind slice returns **200** after `_trim`. Pinned by
`test_history_never_starts_on_an_orphaned_tool_result` and
`test_a_screenshot_message_does_not_shield_orphans_behind_it`, the second sweeping every
cut point rather than one chosen offset — the original bug was a parity accident, and
picking a single offset is exactly how it stayed hidden.

Measured on the same prompt that produced the wander: **12 calls with 7 unrequested host
commands, down to 6 calls with 1.** The blocked repeat leaves no audit line, since it never
reaches the gate — the journal records it (`stopped: vm_snapshot repeated with the same
arguments`) and the user gets it as the reply.

This is a heuristic, not a guarantee. It catches losing the thread, not a model that wanders
with varied arguments; `MAX_STEPS` remains the backstop for that.

## Permission model

**The gate is one function in our own dispatch path.** No framework to route around:
`_decide()` runs before every tool executes, because the loop calls it. Strictly stronger
than the previous design's hook, and the main structural win of the pivot.

### Tool classes

| Class | Tools | Guarded-mode behavior |
|---|---|---|
| Safe | `notify`, `job_list`, `job_read`, `vm_see`, read-only `run_shell` (below), **all `vm_*` tools** | auto-allow |
| Guarded | **everything else** — `run_shell`, `type`, `key`, `click`, `job_start`, `job_show`, `job_kill`, `job_send`, `delegate` | prompt |

Guarded is the **default, not a list.** The Safe set is enumerated; anything unrecognised
prompts — including a tool added later by someone who forgot to update the gate. A
guarded-*list* would fail open on the one name nobody predicted. **This polarity is
load-bearing and must not be inverted for convenience.**

**Why every `vm_*` tool is Safe:** the VM is disposable and snapshot-backed. Prompting to
click inside a machine whose entire state can be restored in seconds is friction with no
safety return. This is the payoff of the two-tier split — the tier boundary *is* the
security boundary, so the gate can be strict on the host and absent in the VM.

Two named exceptions, because they cross the boundary:

- `vm_restore` is **guarded** — it destroys guest state the user may still want.
- Anything that moves data host→VM or VM→host is **guarded**. A VM that can write to the
  host filesystem is not a sandbox. See "VM isolation."

There is **no hard never-list** on the host — per user decision, everything dangerous
(`sudo`, `rm -rf`, `dd`) is guarded and can be allowed through in auto mode.

### Judging a shell command (no shell parsing)

Writing a shell parser to decide safety is unwinnable (`$(...)`, etc.). Two cheap checks:

1. Command contains any of ``; & | ` $ ( ) > <`` or a newline → **always prompt.** No
   exceptions.
2. Metacharacter-free → match leading tokens against a read-only allowlist (`ls`, `cat`,
   `git status`, `git log`, `tmux ls`, `ps`, `df`, …) → auto-allow. Anything else →
   prompt.

Deliberately paranoid; it will ask about harmless things. That is the correct direction to
be wrong in. The allowlist grows from real usage, never speculation. Applies to host
`run_shell` only — `vm_shell` is Safe.

### Typing and clicking on the host are always guarded

`type`, `key`, and `click` inject at kernel level into whatever holds focus. A typed string
is arbitrary input to an unknown window — a shell, a password field, a chat box. So:

- They are **never** Safe, and no allowlist exempts them.
- The prompt shows the **exact keystrokes or coordinates**; what you approve is what lands.

Focus is not verifiable from the daemon (`GetWindows` is denied), so Z.OS cannot promise
*where* text goes. The prompt is the mitigation: the user knows what has focus. Their VM
counterparts are Safe because the target is unambiguous and revertible.

### Modes

```
guarded  (default)  — Safe auto-allows; everything else prompts
auto                — everything allows; nothing prompts
```

Auto is **sticky**: once on, it stays on until you say `guarded`. It does **not** time out.
Four properties keep it survivable:

- **Reverts on daemon restart.** Startup state is always `guarded`. A crash, reboot, or
  `systemctl --user restart zos` drops back to guarded — auto is never the state you wake
  up to, only the state you explicitly set this session.
- **Visible while on.** A resident `notify-send -u critical -t 0` badge stays up the whole
  time auto is live.
- **Still audited, still narrated.** Every action is logged in every mode; in auto, each
  allowed *host* action fires an after-the-fact notification. Auto trades the veto, not the
  visibility. VM actions are not narrated — they are Safe already, and narrating a click
  storm would be noise.
- **`guarded` is an emergency brake, and it lands mid-request.** `handle()` matches and
  consumes mode text *before* taking the request lock, so saying `guarded` while a request
  is already running takes effect at once instead of queueing behind it. The running
  request is not killed — it keeps its place — but every gate decision it has not yet
  reached now prompts. This is the only way to intervene in a request that is already
  under way, which makes it exactly the property a runaway auto-mode request needs.

  The path is unlocked in **both** directions, so `auto` also lands mid-request — and that
  is the direction worth checking, because it widens. It is safe only because of an
  ordering in `_decide_fast`: `self.denied` is tested *before* `self.auto`. A standing
  denial therefore outranks a mid-request `auto`, and an action the user has already
  vetoed cannot be resurrected by switching modes inside the same request. That ordering
  is load-bearing for this property, not just for the retry-around-a-Deny case it was
  written for. The claim that a mid-request `guarded` makes the remaining decisions prompt
  rests on `_decide_fast` reading `self.auto` live rather than snapshotting it, which
  `test_auto_is_sticky_and_never_expires` pins.

  **Do not "fix" this by serialising mode changes with everything else.** Putting the mode
  path behind the request lock looks tidier and reads as closing a race; what it actually
  does is make the brake wait for the thing it is meant to stop. Pinned by
  `test_a_mode_command_lands_while_a_request_is_still_running`, which holds the lock and
  asserts the flip happened *while it was still held* — the obvious version of that test,
  asserting the mode afterwards, passes either way, because releasing the lock lets a
  blocked handler finish and flip it anyway. That first version was green against the
  mutation and was only caught by testing it.

**Mode commands are matched by the daemon, not the model.** Because plain English is the
only input, "go full auto" is also plain English — if mode switching were a tool, a
prompt-injected page or file could talk the model into disabling its own gate. `zosd` does
a strict literal match (`auto`, `guarded`), consumes it, and never forwards it. ~5 lines,
no NLU. **The model has no code path to change its own permissions.** Strict matching is
deliberate: a missed "go full auto" is a harmless retype; a fuzzy match firing on "don't go
full auto" is not.

### Prompt injection and the VM

The VM makes injection *more* likely to be encountered, not less: reading a webpage or
running an untrusted installer in the guest is exactly what it is for, and `vm_see` feeds
guest pixels into the model's context. Guest content is **untrusted input**, never
instruction. Three defences:

- Mode matching happens before the model, so guest text cannot flip the mode.
- Every host tool stays gated, so a guest-sourced instruction to `rm -rf ~` still prompts,
  and the prompt shows a command that has nothing to do with what the user asked.
- Host↔VM data movement is guarded, so the guest cannot quietly write to the host.

The two-line prompt is the tell: if you asked for the time and it wants to `curl … | sh`,
the lines disagree.

### `source` field

`source: "user"` (human at keyboard) may use auto mode. Any other source (cron, inotify,
webhook) gets **no auto-allow outside the Safe class and never auto mode** — nobody is
watching the badge. One field, defers the whole trigger subsystem honestly.

### Prompting

> **Amended 2026-07-27, during Task 4.** This section originally specified a prompt with
> no window, using libnotify action buttons:
>
> ```
> notify-send -u critical -A allow=Allow -A deny=Deny -w "Z.OS" ...
> ```
>
> **That does not work on GNOME 49.** `notify-send` accepts the actions over D-Bus, exits
> successfully, and no button is ever rendered. Three prompts sat in process state `Sl`
> waiting for a reply that no UI existed to send; the user confirmed seeing the
> notifications with no buttons on them. Every guarded-mode prompt timed out, so guarded
> mode was unanswerable. Same class of failure as the Agent SDK's `can_use_tool`: an API
> that accepts input, reports success, and silently does not do the thing.
>
> The "no window" goal is abandoned, not worked around. A permission prompt that can be
> silently dropped is worse than a window. See `plans/notes-live-host.md`.

```
zenity --question --title "Z.OS" --ok-label Allow --cancel-label Deny --default-cancel \
  --timeout 60 --text "you said: <intent>\n\nwants to: <exact action>"
```

> **Amended 2026-07-27.** `--default-cancel` was missing, and without it **Allow is the
> default button, so a single Enter approves.** The dialog takes focus, so an Enter meant
> for whatever the user was actually typing in lands on the gate instead.
>
> Found by asking who approved a run of prompts during the router wander above. The user
> could not say — *"maybe, I'm not sure"* — and that answer is the finding. A gate nobody
> remembers passing is not a gate, and memory is not evidence, so it was measured instead:
> synthetic Enter into the old dialog returned **0** (Allow); into the fixed one it returns
> **1** (Deny). End-to-end, a gated `touch` is now recorded `deny - user denied` and the
> user is notified that it failed.
>
> This contradicted the module's own stated principle three lines above the bug — *the
> failure mode of the prompt system must be "nothing happens", never "it ran"* — while Enter,
> the likeliest stray keystroke there is, was wired to Allow. The lesson worth keeping:
> stating the invariant in a docstring does not enforce it. A test asserts the flag is
> passed.
>
> It compounds with the wander: a model issuing many prompts plus a user typing elsewhere
> equals silent approvals. Fixing either alone would have left the pair dangerous.

zenity is already a dependency (the client uses `zenity --entry`), and a dialog is a real
window that the compositor cannot drop. The prompt shows **both** the user's English and
the actual action — you approve the action, and the mismatch between the two lines is
exactly when you Deny.

**Exit-code mapping is the security boundary.** Only exit 0 is an allow. zenity returns 1
for both Deny and a closed window, 5 for its own `--timeout`, and other codes for other
states; every one of those must block. `--timeout` is passed to zenity as well as enforced
in `prompt_user`, so the dialog closes itself even when the daemon cannot signal it.

`prompt_user` returns `"allow"`, `"deny"` or `"fail"` and **never raises**. A raise reaches
`route()` and kills the request: the action is blocked, but no verdict is audited, making a
denial indistinguishable from a crashed daemon. Cleanup code in particular must not raise.

**Default on anything unexpected is deny:** 60s timeout, dismissed notification, daemon
restart mid-prompt, notification daemon down. Distinguish an explicit Deny from a *failed*
prompt: both block, but only an explicit Deny interrupts the turn — a failure must not be
reported to the model as a user decision. The failure mode of the prompt system must be
"nothing happens," never "it ran."

### sudo (host)

Z.OS cannot pass sudo's own password gate, and should not.

- **`SUDO_ASKPASS` → zenity password dialog (default).** The model runs `sudo -A`; you get
  a password box. Works in both modes, no system changes; the unskippable prompt is the
  OS's, not ours.
- **NOPASSWD sudoers entry (opt-in, config-only).** True zero-prompt sudo. Stated cost: not
  scoped to Z.OS — *any* process running as your user gets passwordless root permanently. A
  machine-wide widening, not a Z.OS setting.

In the guest, root is free — that is the point of the VM tier.

**Verified 2026-07-27, without any password.** The wiring was designed and wired long
before anything proved it fired: `zos.service` sets `SUDO_ASKPASS`, `zos-askpass` execs
`zenity --password`, and exactly one `sudo -A` had ever run through Z.OS (`apt update`,
04:04) — allowed by the user, but the audit log records the *verdict*, never whether sudo
then succeeded. So it was tested by pointing `SUDO_ASKPASS` at a stub that logs a line and
prints a deliberately wrong password: `sudo` invoked it **three** times (its retry limit)
and refused with `Authentication failed`.

That is the whole point of the method — a *rejected* password proves the plumbing better
than an accepted one would, because it shows `sudo` reached the helper, read its stdout,
and enforced its own gate, while never putting a real credential into a command line, a
process table, or a transcript. Never verify this path by typing the actual password to
make it "work"; the passing result carries no more information and the cost is a leaked
secret. The remaining link — that a `zenity` dialog from a `systemd --user` service can
actually reach the display — is proven independently by the gate dialogs, which are the
same mechanism from the same process.

### Socket & audit

- Unix socket at `$XDG_RUNTIME_DIR/zos.sock`, mode `0600`. **Never TCP**, not even
  loopback. `$XDG_RUNTIME_DIR` is already `0700` and user-owned, so filesystem perms
  suffice.
- QMP socket at `$XDG_RUNTIME_DIR/zos-vm.sock`, same reasoning. **Anyone who can write to
  the QMP socket owns the VM completely** — QMP has no auth.
- Audit log: every tool call, verdict, reason, tier, and source, one JSON line appended to
  `~/.local/share/zos/audit.log`. ~10 lines. For an operator that acts unwatched, "what did
  it do at 4am" must be answerable. **Writes in every mode, both tiers.**

## Tools

### Host tier

| Tool | Class | Implementation |
|---|---|---|
| `notify(text)` | safe | `notify-send` |
| `job_list()` | safe | `tmux ls` |
| `run_shell(command)` | safe iff allowlisted, else guarded | `create_subprocess_shell`, 60s cap, output truncated |
| `type(text)` | guarded | `ydotool type -- <text>` |
| `key(keys)` | guarded | `ydotool key <keys>` (e.g. `ctrl+alt+t`) |
| `click(x, y, button)` | guarded | `ydotool mousemove -a -x X -y Y` + `ydotool click` |
| `job_start(name, cmd)` | guarded | `tmux new-session -d -s name cmd` |
| `job_show(name)` | guarded | `gnome-terminal -- tmux attach -t name` |
| `job_kill(name)` | guarded | `tmux kill-session -t name` |
| `job_read(name)` | safe | `tmux capture-pane -p -t name` — the pane as text, for the model |
| `job_send(name, text, keys)` | guarded | `tmux send-keys` — literal text, then tmux key names |
| `delegate(agent, task, cwd)` | guarded | worker CLI live in a tmux session (below) |

Clipboard (`wl-copy`), app launching (`gtk-launch`), git, files — all plain `run_shell`. No
wrapper tools: wrapping a one-line shell command in a tool definition is pure overhead.

`run_shell` uses a **shell** (not `exec`), because pipes and redirection are most of a
one-liner's value. That is exactly why check 1 forces a prompt on any metacharacter.

### VM tier

All Safe except `vm_restore`. QMP over `$XDG_RUNTIME_DIR/zos-vm.sock`, hand-rolled JSON
(~40 lines, no dependency — verified working).

| Tool | Implementation |
|---|---|
| `vm_see()` | `screendump` → PPM → PNG → image part in the next message |
| `vm_type(text)` | `input-send-event`, text → qcodes |
| `vm_key(keys)` | `input-send-event` with modifiers |
| `vm_click(x, y, button)` | `input-send-event` abs pointer + button |
| `vm_shell(command)` | `ssh` into the guest on a forwarded port |
| `vm_snapshot(name)` | `snapshot-save` |
| `vm_restore(name)` | `snapshot-load` — **guarded** |
| `vm_status()` | `query-status` |

`ydotool` stays the host input path even though the portal offers `NotifyKeyboardKeysym`:
it already works, needs no session, and survives a portal session dying.
`ponytail:` one mechanism per capability — the one that already works.

### `delegate` — the super-agent seam

The point of the architecture: Z.OS routes, workers author.

```
tmux new-session -d -s zos-w-<agent> -c <cwd> '<agent> [seed-flag] "<task>"'
```

**The worker runs LIVE, in its interactive mode, and Z.OS drives it** — per user decision,
revised during Task 8. Z.OS is supposed to do what the user would do at the machine, and
nobody drives a coding agent by firing one non-interactive shot at it and walking away.
One-shot mode (`codex exec`, `aider -m`, `agy -p`) would make Z.OS a batch launcher and turn
every question the worker asks into a silent hang — starting with codex's "Do you trust the
contents of this directory?", which is unanswerable in a detached session.

So delegation is a loop, not a launch:

1. `delegate` starts the worker and returns at once.
2. `job_read` returns the pane as text — what it is doing, or what it is asking.
3. `job_send` answers. A **menu** takes a bare `Enter` (or `Down Down Enter`); a **chat
   input** takes text *and* `Enter`. `Enter` is therefore never implied, and `job_send`
   carries literal text and tmux key names as separate fields.

Two properties of `job_send` were only discoverable against a real worker, and both are
load-bearing (see `plans/notes-delegation.md`):

- It must branch on the child's **exit status**, not its output. A helper that returns
  output folds a silent success into the truthy string `"exit 0"`, and a model told its
  keystrokes failed will send them into a live coding agent again.
- Text and a submit key sent microseconds apart **race the TUI**: the paste is not committed
  yet and the key is swallowed. A short settle between typing and the key fixes it, the way
  a human pause does.

**`cwd` is required.** Without it the worker inherits the daemon's working directory — Z.OS's
own repo — rather than the directory the task is about. `delegate` refuses rather than
defaults, and the permission prompt shows the directory, because the same task aimed at the
wrong tree is a different act entirely.

**No auto-approve flags.** `--dangerously-bypass-approvals-and-sandbox`, `--yes-always` and
`--dangerously-skip-permissions` belonged to the fire-and-forget design, where nobody could
answer the worker. A live session can answer it, so bypassing the worker's own approval
prompts throws away a second checkpoint for nothing.

Per-agent invocation is recorded in the plan, verified against each `--help`, not guessed at
runtime. The only per-agent variation left is whether the task can be seeded on argv:
`aider`'s positional arguments are *filenames*, so its task must be sent with `job_send`
once the session is up.

**Stated honestly**, in two parts now:

- **In guarded mode the boundary is tighter than the fire-and-forget design assumed.** The
  worker keeps its own prompts, and every answer Z.OS gives is itself gated — the user sees
  the decision to start a worker *and* each subsequent approval.
- **In auto mode it collapses back.** Z.OS answers the worker on its own with no dialog,
  which is auto-approve by proxy. Auto is the user's explicit, sticky choice, so this is a
  documented consequence rather than a defect — but it is the mode in which a worker's reach
  is unbounded.

Either way the gate covers *whether to start a worker*, not what it does afterwards.

**The new risk the live design introduces:** `job_read` feeds a worker's output into the
model's context, and that output includes whatever the worker read from files and command
results. That is a prompt-injection surface. `SYSTEM` states that a terminal session's
content is DATA, never instructions, including anything a worker prints into its own pane.
A mitigation, not a proof.

## VM isolation

The VM is a safety mechanism only if it is actually isolated. Non-negotiable:

- **No shared filesystem.** No `virtfs`, no `9p`, no `virtiofs` mount of a host directory.
  File movement is explicit and guarded (`scp` over the forwarded port), so it appears in
  the audit log.
- **User-mode networking** (`-netdev user`) with only an SSH port forward. The guest reaches
  the internet and cannot reach host services.
- **A snapshot named `clean`** taken immediately after provisioning, so there is always a
  known-good state to return to.

A VM with a host directory mounted read-write is not a sandbox — it is a slower path to the
same damage.

**Restore is verified, not assumed.** Every `vm_*` tool is ungated on the argument that
mistakes in the guest are revertible, so that argument is worth exactly as much as the
restore path actually working. Checked end to end against the live guest: write a marker
file, snapshot, delete the file, `vm_restore`, and the file is back. Until this was run the
whole justification for the ungated tier rested on an untested assumption — `vm_snapshot`
had never once been called in ~320 live tool invocations.

## Files

```
zosd.py       daemon: socket loop, router loop, gate, mode matcher, audit
tools.py      host tool schemas + implementations
vm.py         QMP client + VM tool implementations
zos           client shim (zenity + socat)
zos-askpass   SUDO_ASKPASS helper (zenity --password)
zos.service   systemd --user unit for the daemon
zos-vm.service systemd --user unit for the QEMU guest
vm/setup.sh   one-shot: fetch Ubuntu cloud image, cloud-init provision, snapshot 'clean'
test_zos.py   assert-based tests, no framework
```

`vm.py` is separate because the VM tier can be entirely absent — no guest, no QEMU — and
the host tier must keep working.

## Error handling

- Daemon dies → systemd restarts it; history and mode reset; jobs, workers, and the VM
  survive.
- VM absent or QMP socket missing → every `vm_*` tool returns an error string; host tier
  unaffected. The model is told the VM is unavailable and continues.
- Job or worker dies → gone from `job_list`; output stays in tmux scrollback.
- Prompt fails (any reason) → deny.
- Malformed tool arguments → error string back to the model as the tool result; it retries
  or explains. Never crashes the loop.
- Router loop exceeds 12 iterations → stop, notify that it gave up.
- Gemini API error or timeout → notify with the error. No silent failure.
- Second request while busy → queued behind one `asyncio.Lock`. Requests are short by
  design (long work lives in tmux), so one session suffices — no concurrency layer.

## Testing (one file, assert-based, no framework)

The runner block must stay at the **very bottom** of `test_zos.py`. It collects tests from
`globals()`, which is evaluated where the block runs — so for a while 14 tests sat below it
and simply never executed, while the suite reported that everything passed. Anything that
reports success without running is worse than no suite at all.

**The suite redirects every path it can write to `zosd`'s module globals — `AUDIT` and
`SOCK` — once, at import.** Both had to be learned the hard way. `AUDIT` because tests that
reach `_gate` would otherwise forge lines into the real audit trail. `SOCK` because two
tests bind a real server at `zosd.SOCK` and unlink it afterwards: run against the default
path, that is the *live* daemon's socket, and the unlink leaves the running daemon holding a
listening inode no filename reaches. The daemon stays up, keeps logging hotkey presses, and
answers nothing. Every client after that dies on a broken pipe with no line in any log
saying why. A test asserts the redirect is still in place, because the failure it prevents
is invisible.

**Why it hid is worth keeping.** Pressing the hotkey kept working perfectly while the daemon
was already deaf, because the two paths are independent up to the moment text is submitted:
the client shows its box, and `[ -n "$text" ] || exit 0` returns before `socat` ever runs.
Every press that day was dismissed with Esc, so not one of them touched the socket. A dead
socket and a healthy one are indistinguishable from the keyboard unless you actually send
something.

The general rule — and both redirects at the top of the suite, `AUDIT` and `SOCK`, are
instances of it: **a test that can reach production state will eventually break production
silently.** Redirect at import, not per-test; a per-test contextmanager only protects the
tests someone remembered to wrap.

`hotkey_check.py` is separate and manual: it needs `/dev/uinput` and the `input` group, so
it cannot live in an offline suite. Run it after touching the listener.

- metacharacter check rejects `ls; rm -rf ~`, `cat x > y`, `ls $(whoami)`
- allowlist accepts `git status --short`, rejects `git push`
- gate fails closed on an unrecognised tool name
- `type`, `key`, `click`, `delegate`, `job_send` are guarded; `job_read`, `vm_type`,
  `vm_click`, `vm_see` are Safe
- `vm_restore` is guarded despite the `vm_` prefix
- guarded mode denies when the prompt mechanism fails, and distinguishes that from an
  explicit Deny
- auto mode is sticky (does not expire) but a fresh daemon is guarded
- non-`user` source never gets auto mode
- a mode command is consumed by the daemon and never reaches the model
- `job_start` creates a real tmux session; `job_kill` removes it
- `delegate` refuses a `cwd` that is not an existing directory, and no worker is ever
  started with its approvals bypassed
- the `delegate` prompt shows the directory and the whole task, untruncated
- `job_send` reports success when the command is silent, and still reports a real failure
- the hotkey detector fires once on Meta+Space, not on autorepeat, not on Space alone, and
  retains nothing but the modifier state
- the suite never writes the real audit log
- QMP client round-trips `query-status` and `screendump` against a live guest
- router loop terminates on a tool-less response and caps at 12 iterations
- audit log gets one line per verdict, tier recorded, in both modes

## Explicitly out of scope

Chat UI · log store · job scheduler · event dispatcher · plugin system · vector DB · host
window/workspace control (compositor-blocked, no portal) · host screen reading
(DRM-master-blocked) · OCR · voice (STT/TTS) · wake word · multi-user · cloud sync · GPU
passthrough · multiple concurrent VMs. Voice and wake word are planned later tiers.

## Build order

1. `tools.py` — host schemas + implementations, tested by calling handlers directly. No API.
2. `zosd.py` — socket loop + router loop + gate + mode matcher + audit + tests. Gemini calls
   only in the final end-to-end check.
3. Live host end-to-end: hotkey → zenity → route → notify, both modes, prompt and deny.
4. `vm/setup.sh` + `zos-vm.service` — provision the guest, snapshot `clean`, verify boot.
5. `vm.py` — QMP client, `vm_see` into a real routing decision, `vm_type`/`vm_click`
   round-trip.
6. `zos.service` + `zos-askpass` + `Super+Space` via `gsettings` and the daemon's own
   `/dev/input` listener; restart invariants.
7. `delegate` against a real worker CLI on a real task, driven live via `job_read`/`job_send`.

## Install

Everything needed to rebuild this machine. Commands are what was actually run, not
what was first planned — the differences are noted. **No key value appears here.**

### 1. Credentials, outside the repo

```bash
mkdir -p ~/.config/zos
umask 077
grep -m1 '^ZEROOS_API_KEY=' \
  /run/media/yash/External/Zerostic/ZeroOS/stage0/.env \
  | sed 's/^ZEROOS_API_KEY=/GEMINI_API_KEY=/' > ~/.config/zos/env
chmod 600 ~/.config/zos/env
wc -c < ~/.config/zos/env      # sanity check only — never cat this file
```

The daemon reads `GEMINI_API_KEY`; the existing key is stored under a different name,
hence the rename. Sourcing `stage0/.env` directly is not enough — the daemon starts
fine and then fails on the first request with
`httpx.LocalProtocolError: Illegal header value b'Bearer '`.

It lives in `~/.config/zos/`, not the repo, so no `.gitignore` mistake can commit it.

### 2. The unit

```bash
ln -sf /run/media/yash/External/Zerostic/Z.OS/zos.service ~/.config/systemd/user/zos.service
systemctl --user daemon-reload
systemctl --user enable --now zos.service
systemctl --user status zos.service --no-pager | head -5
```

To stop a manually-started daemon first, resolve the PID and `kill` it. Do **not** use
`pkill -f 'python.*zosd.py'`: the pattern matches the command line of the shell running
it, so the invoking shell dies instead (exit 144). This bites every time.

### 3. Super+Space

`<Super>space` has **two** owners on this desktop, not one. Freeing only the GNOME
keybinding is not enough — ibus holds it separately.

```bash
# GNOME's input-source switcher. The plan set both to [], but only <Super>space needs
# freeing, so XF86Keyboard is kept and the dedicated key still switches sources.
gsettings set org.gnome.desktop.wm.keybindings switch-input-source "['XF86Keyboard']"
gsettings set org.gnome.desktop.wm.keybindings switch-input-source-backward "['<Shift>XF86Keyboard']"

# ibus, the second owner. Missed by the original plan. Clearing the key is not enough:
# ibus grabbed <Super>space when it started, and a grab is runtime state, not
# configuration, so it keeps the key until it is restarted. Until then the press does
# nothing visible at all, because no input sources are configured.
gsettings set org.freedesktop.ibus.general.hotkey triggers "[]"
ibus restart

# custom0, the name GNOME's own Settings UI uses. Any name works in principle, but
# matching the canonical one removes a variable when this misbehaves.
K=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$K']"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$K" name 'Z.OS'
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$K" command '/run/media/yash/External/Zerostic/Z.OS/zos'
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$K" binding '<Super>space'
```

This machine has no input sources configured (`org.gnome.desktop.input-sources sources`
is empty), so neither switcher was doing anything useful before being freed.

### 4. Diagnosing the keybinding

Two tools lie here, and both cost time:

- **`dconf read` and `dconf dump` return empty for keys that are definitely set.** Trust
  `gsettings get` in a *fresh process* instead; same-invocation reads prove nothing.
- **`pgrep -f <pattern>`** matches the shell running it, so it reports processes that do
  not exist. Use `pgrep -x <comm>`. Note this is not only a shell-hygiene problem: the same
  trap reappeared *inside the daemon* as a `/proc` cmdline scan (§5). Identify a process by
  something it holds, not by text that happens to appear on a command line.

- **`ydotool` cannot test this at all.** A synthetic `<Super>` press does open the
  overview, which proves synthetic input reaches mutter's *internal* keybindings — but
  custom keybindings are grabbed by `gsd-media-keys` over
  `org.gnome.Shell.GrabAccelerators`, and synthetic presses never fire them. Only a
  physical press is evidence.

`org.gnome.SettingsDaemon.MediaKeys.service` sets `RefuseManualStart`/`RefuseManualStop`,
so `systemctl --user restart` on the *service* is refused. Restarting its **target** works
and is not refused, because the service is `PartOf` it:

```bash
systemctl --user restart org.gnome.SettingsDaemon.MediaKeys.target
```

That replaces the earlier advice to log out. It does re-grab accelerators — but see §5:
it did not make the binding fire, so a stale `gsd-media-keys` was not the cause.

**`gnome-shell` cannot be restarted this way on Wayland, and that is the harder half.**
A GNOME "log out / log in" on this machine did *not* restart the session: after one,
`gnome-shell` and `gsd-media-keys` still carried their process start times from three days
earlier. Check before believing a re-login took effect:

```bash
ps -o lstart= -p "$(pgrep -x gnome-shell)"
```

### 5. The daemon's own hotkey listener

Per user decision ("do both"), the GNOME keybinding is not the only owner of Super+Space.
`zosd` also watches the keyboard directly, so the hotkey belongs to Z.OS rather than to a
desktop setting a reinstall or a GNOME upgrade can silently drop.

It reads raw `input_event` records (`struct llHHi`) from `/dev/input/by-path/*-event-kbd`
and `by-id/*-event-kbd`, added to the event loop with `add_reader`. Access comes from
membership in the `input` group — checked, not assumed: the device is `root:input 0660`
with no `uaccess` ACL, so the group is the only thing granting it. If no device is readable
it logs and continues — the GNOME binding is still there.

**This is the most invasive thing Z.OS does, so the restraint is structural, not a
promise.** The detector is a ten-line class holding a set of currently-held modifier codes;
it looks at exactly three key codes (`KEY_LEFTMETA`, `KEY_RIGHTMETA`, `KEY_SPACE`) and
retains nothing else. Nothing accumulates, nothing is logged, nothing is sent anywhere.
Autorepeat (`value == 2`) is ignored so a held key cannot open a second dialog. There are
unit tests for exactly this — including one asserting the listener remembers nothing but
the modifier — because "it does not log keystrokes" has to be checkable, not trusted.

Both paths run the same client, so a duplicate would open two dialogs. A short grace period
lets the GNOME binding win when it works — on this machine it never has (see Status) — and
before firing the daemon checks whether a client is already up.

**That check is a lock the client holds, not a process search** — `flock` on
`$XDG_RUNTIME_DIR/zos-client.lock`, taken by the client for as long as its box is open. The
process-based versions were wrong three times over, each silently:

| Attempt | Why it fails |
|---|---|
| `pgrep -f <path>` | matches the command line of the shell running it |
| scan `/proc` cmdlines for the path | matches any shell that merely *names* it (`rm /path/zos`), so the hotkey stands down for as long as that command runs |
| match `argv[0]` | the client is a script, so `argv[0]` is the interpreter, not the client |

The daemon *takes* the lock to test it rather than only querying it. That is safe: if a
client is starting at that instant it loses the race and exits, and the daemon — having got
the lock — then sees no client and starts one. Either way exactly one box opens.

**Locking must never be able to suppress the box.** The client runs under `set -euo
pipefail` and GNOME discards its stderr, so any exit before `zenity` looks exactly like a
dead hotkey. A missing `flock` or an unopenable lock path therefore *falls through* and
shows the box; only winning the lock and losing the race may exit. Verified by running the
client with `flock` off `PATH` and with an unwritable `XDG_RUNTIME_DIR` — the box appears
in both.

The client also closes the lock fd once the box is gone (`exec 9>&-`), because it means
"a box is up", nothing more. Left open, it is inherited by the `socat` that follows, and
one hung socket write would keep the hotkey dead long after the dialog closed.

**Status.** Everything from the file descriptor down is **verified against real kernel
events** by `hotkey_check.py`, which creates a uinput virtual keyboard, points
`_keyboards()` at its evdev node and runs the real `Daemon.watch_hotkey`. Observed: one
client on Meta+Space, *no* second client while the first is still up (the dedup branch),
nothing at all on Space without Meta, and a client again once the first exits.

That harness is also what found the `/proc` scan bug above — its own invoking shell named
the fake client in an `rm`, and the listener dutifully stood down. Worth stating plainly:
the offline suite had passed throughout, because it had never run this wiring at all.

**Verified by physical press.** `_keyboards()` does find the physical keyboard: real
presses log `hotkey: meta+space detected`, and the box opens. The listener is the working
owner of Super+Space.

**GNOME's binding does not fire, and the daemon's logs alone could not show that.**
`hotkey: client already open, standing down` reports *state* — a lock is held — not *cause*.
The daemon deduping against a box it opened itself seven seconds earlier produces the
identical line, so a run of standing-down entries reads as "GNOME is winning the race" when
it may mean nothing of the kind. The client therefore logs its **parent** before taking the
lock:

```bash
logger -t zos-client "launched by $PPID $(ps -o comm= -p $PPID 2>/dev/null)" 2>/dev/null || true
```

The parent is causal where the lock state is not: `python` is the daemon, `gsd-media-keys`
is GNOME. Logged *before* the lock so a launch that loses the race still records itself —
otherwise the losing owner is exactly the one that leaves no trace. `|| true` and a
discarded stderr are mandatory here for the §5 reason: under `set -euo pipefail` a missing
`logger` would turn a diagnostic into a dead hotkey.

Every observed press logs one launch, parent `python`. Never `gsd-media-keys`.

Ruled out as causes:

| Suspect | Ruled out by |
|---|---|
| ibus / input-source grab on `<Super>space` | `switch-input-source` is `XF86Keyboard`, ibus triggers `[]`, no input sources configured |
| stale `gsd-media-keys` predating the config | restarted via its target; a fresh process behaves identically |
| a conflict specific to `<Super>space` | rebound the same custom keybinding to `<Super>F9` — the listener ignores F9 by design, so GNOME was the only possible owner, and **nothing launched** |

That last one is the decisive experiment: it isolates GNOME's path completely. The failure
is GNOME custom keybindings in general in this session, not this key.

**Cause unknown — and a stale `gnome-shell` grab is not it.** That was the obvious next
suspect, since `gnome-shell` has held its accelerator grabs since before the keybinding
existed and on Wayland cannot be restarted without ending the session (which a GNOME log
out did not do here, §4). But the F9 experiment already rules it out: `<Super>F9` was never
bound to anything, by mutter or anyone else, so there was no stale grab on it to hold — and
it still did not fire. A stale-grab explanation requires a pre-existing grab on the key in
question. **Do not reboot chasing this**; the same isolation that ruled out the key conflict
rules out the grab.

**It changes nothing operationally.** The GNOME binding was always redundancy: the reason
for owning the hotkey in the daemon is precisely that a desktop setting can be dropped by a
reinstall or an upgrade. That redundancy has simply never been the one carrying the key.
The config is left in place — it is inert and removing it buys nothing — but
it must not be *relied* on, and the install steps should not claim the hotkey works because
of it.

The dedup lock keeps earning its place regardless of owner count: two presses in quick
succession would otherwise open two boxes.
