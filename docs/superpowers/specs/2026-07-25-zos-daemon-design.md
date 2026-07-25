# Z.OS — Headless Desktop Agent Daemon

**Date:** 2026-07-25
**Status:** Design approved, pending spec review

## What this is (and is not)

Z.OS is **not an operating system**. It is a persistent, headless agent that runs
as a background user service on Ubuntu/GNOME (Wayland) and treats the desktop as
its toolbelt. No kernel, no init, no scheduler. It rides on the existing OS.

Core thesis: **one persistent agent, reachable in ~200ms, with the machine as its
tools.** It draws nothing by default. It surfaces on demand (a job viewer, a
notification) or when it needs permission.

## Target environment (probed 2026-07-25)

- Ubuntu GNOME, Wayland session (`XDG_SESSION_TYPE=wayland`)
- Present: `tmux`, `notify-send` (libnotify 0.8.8 — supports `-A` action buttons),
  `zenity`, `wl-copy`/`wl-paste`, `gnome-screenshot`, `gtk-launch`, `gio`, `socat`,
  `systemctl`, `claude` CLI 2.1.219
- Missing and staying missing: `fuzzel`/`wofi` (need `wlr-layer-shell`, which GNOME
  does not implement), `rofi` (X11-only). Therefore the input surface is `zenity --entry`.
- `xdotool` is installed but only works on XWayland apps — do not rely on it for
  native GTK window control. Real GNOME window control is a separate subproject (out of scope).
- Python is 3.14.4 (both system and brew). **Flagged risk:** very new for the Agent
  SDK's anyio stack. First implementation step is a smoke test; if it breaks, pin a
  3.12 via `uv` for the daemon only. Not a design change.

## Architecture

```
 Super+Space ──> zenity --entry ──┐
                                   ├──> $XDG_RUNTIME_DIR/zos.sock (0600)
 later: voice, cron, inotify ─────┘              │
                                                 v
                                        zosd  (systemd --user)
                                    persistent Claude Agent SDK session
                                                 │
                    ┌────────────────┬───────────┴──────────┬─────────────┐
                    v                v                      v             v
              SDK built-ins     job_* tools            notify tool    permission
           Bash/Read/Write/       (tmux)             (notify-send)      gate
              Grep/Glob
```

### Three moving parts

**1. `zosd`** — Python daemon, systemd `--user` service. Owns the Unix socket and one
long-lived `ClaudeSDKClient`. The conversation persists across invocations — this is
what makes it feel like one entity rather than a fresh shell each time. Draws nothing.

**2. Clients** — dumb, stateless, one line each. `zenity --entry | socat - $SOCK`.
Each pushes `{"source": "user", "text": "..."}`. Voice and cron clients later write
to the same socket. Clients never parse the user's text.

**3. Jobs = tmux sessions.** The agent never blocks on long work. `job_start` spawns a
detached tmux session and returns immediately. tmux provides — for free, zero code —
persistence across daemon restarts, scrollback-as-log, `tmux ls` as status, `attach`
as viewer, `kill-session` as stop.

### Message flow

hotkey → zenity → socket → daemon → agent reasons → tools fire → `notify-send` with
the result. Long work → agent spawns a tmux job, returns, notifies on completion.

### Input is plain English

The client sends the raw user text. The client never parses or translates. The **agent**
decides which shell commands realize the intent. One exception: mode commands (below),
which the daemon matches *before* the agent sees the text.

## Permission model

The Agent SDK's `can_use_tool` callback is the entire gate — the only path to any tool,
so nothing routes around it.

### Tool classes

| Class | Tools | Guarded-mode behavior |
|---|---|---|
| Safe | `Read`, `Grep`, `Glob`, `WebSearch`, `job_list`, `notify` | auto-allow |
| Guarded | `Bash`, `Write`, `Edit`, `job_start`, `job_kill`, `app_launch` | prompt (see below) |

There is **no hard never-list** — per user decision, everything dangerous (including
`sudo`, `rm -rf`, `dd`) is guarded and can be allowed through in auto mode.

### Judging a Bash command (no shell parsing)

Writing a shell parser to decide safety is unwinnable (`$(...)`, etc.). Two cheap checks:

1. If the command string contains any of `; & | \` $( ) > < ` or a newline → **always prompt**. No exceptions.
2. If metacharacter-free, check the first word against a read-only allowlist
   (`ls`, `cat`, `git status`, `git log`, `tmux ls`, `ps`, …). Match → auto-allow.
   Anything else → prompt.

Deliberately paranoid; it will ask about harmless things. That is the correct direction
to be wrong in. The allowlist grows from real usage, never speculation.

### Modes

```
guarded  (default)  — Safe auto-allows; everything else prompts
auto                — everything allows; nothing prompts
```

Auto is safe to offer because of three properties:

- **Explicit and time-boxed.** Never the startup state, never sticky. Enabled for a
  window ("auto for 30m"); reverts on expiry or daemon restart, whichever first.
- **Visible while on.** A resident `notify-send -u critical -t 0` badge stays up the
  whole time auto is live.
- **Still audited, still narrated.** Every action is logged in every mode; destructive
  ones fire an after-the-fact notification. Auto trades the veto, not the visibility.

**Mode commands are matched by the daemon, not the agent.** Because plain English is the
only input, "go full auto" is also plain English — if mode switching were an agent tool,
a prompt-injected page or file could talk the agent into disabling its own gate. `zosd`
does a strict literal match on the incoming string (`auto`, `auto for 30m`, `guarded`),
consumes it, and never forwards it. ~5 lines, no NLU. The agent has no code path to change
its own permissions. Strict matching is deliberate: a missed "go full auto" is a harmless
retype; a fuzzy match that fires on "don't go full auto" is not.

### `source` field

`source: "user"` (human at keyboard) may use auto-allow / auto mode. Any other source
(cron, inotify, webhook) gets **no auto-allow outside the Safe class and never auto mode** —
nobody is watching the badge. One field, defers the whole trigger subsystem honestly.
This is the "reactive now, event hooks later" (option C) seam.

### Prompting with no window

```
notify-send -u critical -A allow=Allow -A deny=Deny -w "Z.OS" \
  "you said: <intent>\nwants to run: <command>"
```

libnotify 0.8.8 supports action buttons and `-w` blocks until clicked. The prompt shows
**both** the user's English and the actual command — you approve the command, and the
mismatch between the two lines is exactly when you Deny.

**Default on anything unexpected is deny:** 60s timeout, dismissed notification, daemon
restart mid-prompt, or notification daemon down all resolve to deny. The failure mode of
the prompt system must be "nothing happens," never "it ran."

### sudo

Z.OS cannot pass sudo's own password gate. Two options:

- **`SUDO_ASKPASS` → zenity password dialog (default, recommended).** Agent runs `sudo -A`;
  you get a password box. Works in both modes, no system changes; the unskippable prompt
  is the OS's, not ours.
- **NOPASSWD sudoers entry (opt-in, config-only).** True zero-prompt sudo. Stated cost:
  the entry is not scoped to Z.OS — *any* process running as your user gets passwordless
  root from then on, permanently. It is a machine-wide widening, not a Z.OS setting.

### Socket & audit

- Unix socket at `$XDG_RUNTIME_DIR/zos.sock`, mode `0600`. **Never TCP**, not even
  loopback. `$XDG_RUNTIME_DIR` is already `0700` and user-owned, so filesystem perms
  suffice; no peer-credential check needed.
- Audit log: every tool call, verdict, and source, one JSON line appended to
  `~/.local/share/zos/audit.log`. ~10 lines of code. For an agent that acts unwatched,
  "what did it do at 4am" must be answerable. Writes in every mode.

## Custom tools (five thin wrappers)

| Tool | Implementation |
|---|---|
| `job_start(name, cmd)` | `tmux new-session -d -s name cmd` |
| `job_list()` | `tmux ls` |
| `job_show(name)` | `gnome-terminal -- tmux attach -t name` |
| `job_kill(name)` | `tmux kill-session -t name` |
| `notify(text)` | `notify-send` |

Everything else — clipboard (`wl-copy`), screenshots (`gnome-screenshot`), app launching
(`gtk-launch`), git, files — is the agent using plain `Bash`. No wrapper tools for those;
wrapping a one-line shell command in a tool definition is pure overhead.

## Files (four)

```
zosd.py       daemon: socket loop, agent session, permission gate, mode matcher
tools.py      custom tools: job_start/list/show/kill, notify, app_launch
zos           client shim (zenity + socat)
zos.service   systemd --user unit
```

Plus this spec and one test file.

## Error handling

- Daemon dies → systemd restarts it; conversation resets; jobs survive in tmux.
- Job dies → gone from `job_list`; its output stays in tmux scrollback.
- Prompt fails (any reason) → deny.
- Second request while busy → queued on the socket; every request is short by design
  (long work lives in tmux), so one agent session suffices — no concurrency layer.

## Testing (one file, assert-based, no framework)

- metacharacter check rejects `ls; rm -rf ~`
- guarded mode denies when the prompt mechanism fails
- auto mode expires and reverts to guarded
- a mode command is consumed by the daemon and never reaches the agent
- `job_start` creates a real tmux session; `job_kill` removes it

## Explicitly out of scope

Chat UI · log store · job scheduler · event dispatcher · plugin system · vector DB ·
GNOME window/workspace control · voice (STT/TTS) · wake word · multi-user · cloud sync.
Several are planned as later tiers (voice, window control, wake word) but are not part
of this spec.

## Build order

1. Smoke-test Agent SDK on Python 3.14 (pin 3.12 via `uv` if it breaks).
2. `zosd` skeleton: socket loop + one agent session + `notify` tool. Prove the loop
   end-to-end (hotkey → zenity → notification) with no permission gate yet.
3. Permission gate + mode matcher + audit log + tests.
4. tmux job tools.
5. systemd unit + `Super+Space` keybinding via `gsettings`.
6. sudo askpass wiring.
