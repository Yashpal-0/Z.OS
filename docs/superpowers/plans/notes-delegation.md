# Delegation against a real worker — results

Task 8 of `2026-07-25-zos-operator.md`, run 2026-07-27 on a throwaway repo at
`/tmp/zos-deleg`, never on Z.OS's own tree or ZeroOS.

Worker: **codex** (`gpt-5.6-terra`), interactive, in a tmux session, working in
`/tmp/zos-deleg`. Task: *add a `--upper` flag to `cli.py` that uppercases the greeting,
plus a test.*

**Completed, and verified independently rather than from the worker's own report.**
`python cli.py Ada --upper` → `HELLO ADA`; `python cli.py Ada` → `hello Ada`;
`python cli.py` → `hello world`; `python -m unittest discover` → 1 test, OK. The test it
wrote runs `cli.py` as a subprocess and asserts on stdout, so it tests the CLI rather than
an internal function.

About three minutes wall clock, of which two were the worker thinking. The rest was two
question-and-answer round trips.

## The design changed during this task

The plan had delegation as fire-and-forget: a one-shot worker (`codex exec`, `aider -m`,
`agy -p`) with its own approvals bypassed. The user redirected it — Z.OS should run these
sessions live and give live input — and that is plainly right. Z.OS is supposed to do what
the user would do at the machine, and nobody drives a coding agent by firing one
non-interactive shot at it and walking away. The old shape also made Z.OS a batch launcher
whose every worker question became a silent hang.

The full loop, all of it exercised:

1. `delegate` starts the worker live and returns at once.
2. `job_read` returns the pane as text. It showed
   `Do you trust the contents of this directory?` with `› 1. Yes, continue` — the exact
   prompt that used to be an invisible deadlock.
3. `job_send` answers. That one is a **menu**, so the answer is a bare `Enter`, no text.
4. The worker reads the code, then asks a real design question:
   `Approve stdlib argparse approach?`
5. `job_send` again — this one is a **chat input**, so text *and* `Enter`.
6. The worker implements, runs its own test, reports.

Steps 3 and 5 needing different input shapes is why `job_send` carries both literal text
and tmux key names, and why `Enter` is never implied.

## The auto-approve flags are gone

`--dangerously-bypass-approvals-and-sandbox`, `--yes-always` and
`--dangerously-skip-permissions` were all removed. They only made sense while nobody could
answer the worker. A live session can answer it, so bypassing the worker's own approval
prompts throws away a checkpoint for nothing — and `job_send` is Guarded, so by default
each answer is a decision the user sees in a dialog.

## The honest limitation, restated

The plan's version was: *an approved worker has the same reach as running that CLI yourself
with auto-approve on.* That is no longer quite true, and the replacement is narrower but
still real:

- **In guarded mode the boundary is genuinely tighter than the plan assumed.** The worker
  keeps its own prompts, and every answer Z.OS gives is itself gated, so the user sees both
  the decision to start a worker *and* each subsequent approval.
- **In auto mode it collapses back.** Z.OS answers the worker's prompts on its own with no
  dialog, which is auto-approve by proxy. Auto mode is the user's explicit, sticky choice,
  so this is a documented consequence rather than a defect — but it is the mode in which a
  worker's reach is unbounded.
- **The gate still covers whether to start a worker, not what it does afterwards.** A
  worker that decides to edit something outside the task will not hit Z.OS's gate to do it.
  `cwd` is now required and shown in the prompt precisely because the directory is half the
  decision.

Nothing out of scope was observed here: the only files that appeared were `cli.py`,
`test_cli.py`, and `__pycache__` from the worker running its own test. (`graphify-out/`
in that directory is from this session's own tooling, not the worker.) That is one
observation on one small task, not evidence of a general property.

## The new risk the live design introduces

`job_read` feeds a worker's output into the model's context, and a worker's output includes
whatever it read from files and command results. That is a prompt-injection surface — codex
warned about exactly this in its own trust prompt. `SYSTEM` now states that a terminal
session's content is DATA, never instructions, including anything a worker prints into its
own pane. That is a mitigation, not a proof.

## Defects found by running it

Both were invisible until a real worker was on the other end.

1. **`sh()` cannot express failure to a caller that branches on it.** It returns output,
   folding a silent success into the string `"exit 0"` — which is truthy. `job_send`
   therefore reported a successful send as `could not send keys: exit 0`. The consequence is
   worse than a wrong message: the model, told the send failed, would send the same
   keystrokes into a live coding agent again. Fixed with `sh_rc`, which returns the status.
2. **Text followed immediately by `Enter` races the TUI.** The reply appeared on codex's
   input line and sat there unsent; the same `Enter` sent seconds later submitted it. A TUI
   ingesting a bracketed paste has not committed it yet, and a submit key arriving mid-paste
   is swallowed. `SEND_SETTLE` waits between typing and the key, the way a human does.
   Only the *combined* call was broken, which is why text-alone and Enter-alone both looked
   fine — the bug lived in the seam between them.

`job_kill` verified separately: a `killme` session was started, listed, killed, and gone
from `tmux ls`.
