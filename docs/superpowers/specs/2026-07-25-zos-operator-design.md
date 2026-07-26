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
   job_*       delegate(agy/codex)                vm_type   vm_key  vm_click
   notify                                         vm_shell  (ssh)
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
viewer, `kill-session` as stop.

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

## Permission model

**The gate is one function in our own dispatch path.** No framework to route around:
`_decide()` runs before every tool executes, because the loop calls it. Strictly stronger
than the previous design's hook, and the main structural win of the pivot.

### Tool classes

| Class | Tools | Guarded-mode behavior |
|---|---|---|
| Safe | `notify`, `job_list`, `vm_see`, read-only `run_shell` (below), **all `vm_*` tools** | auto-allow |
| Guarded | **everything else** — `run_shell`, `type`, `key`, `click`, `job_start`, `job_show`, `job_kill`, `delegate` | prompt |

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
Three properties keep it survivable:

- **Reverts on daemon restart.** Startup state is always `guarded`. A crash, reboot, or
  `systemctl --user restart zos` drops back to guarded — auto is never the state you wake
  up to, only the state you explicitly set this session.
- **Visible while on.** A resident `notify-send -u critical -t 0` badge stays up the whole
  time auto is live.
- **Still audited, still narrated.** Every action is logged in every mode; in auto, each
  allowed *host* action fires an after-the-fact notification. Auto trades the veto, not the
  visibility. VM actions are not narrated — they are Safe already, and narrating a click
  storm would be noise.

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
zenity --question --title "Z.OS" --ok-label Allow --cancel-label Deny \
  --timeout 60 --text "you said: <intent>\n\nwants to: <exact action>"
```

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
| `delegate(agent, task)` | guarded | worker CLI in a tmux session (below) |

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
tmux new-session -d -s zos-w<N> '<agent> <auto-approve-flag> "<task>"'
```

Per user decision, **the delegation is the decision**: one prompt showing the agent and the
full task, then the worker runs with its own auto-approve flag inside tmux. One approval per
task, not per command — matching how these CLIs get used by hand. Watchable (`job_show`),
killable (`job_kill`), scrollback is the log.

**Stated honestly:** an approved worker has the same reach as running that CLI yourself with
auto-approve on. The gate covers *whether to start it*, not what it does afterwards. The
narrow prompt is the whole safety story, which is why the task text is shown in full and
never truncated.

Per-agent flags are recorded in the plan, verified, not guessed at runtime.

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

- metacharacter check rejects `ls; rm -rf ~`, `cat x > y`, `ls $(whoami)`
- allowlist accepts `git status --short`, rejects `git push`
- gate fails closed on an unrecognised tool name
- `type`, `key`, `click`, `delegate` are guarded; `vm_type`, `vm_click`, `vm_see` are Safe
- `vm_restore` is guarded despite the `vm_` prefix
- guarded mode denies when the prompt mechanism fails, and distinguishes that from an
  explicit Deny
- auto mode is sticky (does not expire) but a fresh daemon is guarded
- non-`user` source never gets auto mode
- a mode command is consumed by the daemon and never reaches the model
- `job_start` creates a real tmux session; `job_kill` removes it
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
6. `zos.service` + `zos-askpass` + `Super+Space` via `gsettings`; restart invariants.
7. `delegate` against a real worker CLI on a real task.
