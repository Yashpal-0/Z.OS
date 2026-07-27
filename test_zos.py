#!/usr/bin/env python3
"""Z.OS tests. Plain asserts, no framework. Run: .venv/bin/python test_zos.py

No API calls: route() is exercised with a stubbed _call_model, so the whole router
loop and gate are testable offline and for free.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import tempfile
import time

import tools
import zosd

# Every test that reaches _gate writes an audit line, so without this the suite
# appends to the real ~/.local/share/zos/audit.log — a trail the tests forge is not
# an audit trail. Worse, the audit assertions below count lines, so a live daemon
# appending concurrently made them flaky. Redirect once, for the whole run.
zosd.AUDIT = pathlib.Path(tempfile.gettempdir(), "zos-test-audit.log")
zosd.AUDIT.unlink(missing_ok=True)

# SOCK needs the same treatment, and for a sharper reason. Two tests below bind a real
# server at zosd.SOCK and unlink it afterwards. Against the default path that is the *live*
# daemon's socket: the unlink leaves the running daemon holding a listening inode that no
# filename reaches any more, so it stays up, keeps logging its hotkey, and answers nothing.
# Every client then dies on a broken pipe with no line in any log to say why. Running the
# offline suite must not be able to take down the daemon it is testing.
zosd.SOCK = pathlib.Path(tempfile.mkdtemp(prefix="zos-test-"), "zos.sock")


def _fake_notify(td):
    """A stand-in for notify-send that records its argv. Returns (path, logfile);
    the gate fires notifications without awaiting them, so the caller must settle."""
    fake = pathlib.Path(td, "fake-notify")
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$0.log"\n')
    fake.chmod(0o755)
    return str(fake), pathlib.Path(str(fake) + ".log")


# ---- shell judgement -------------------------------------------------------

def test_metachars_always_prompt():
    for cmd in ("ls; rm -rf ~", "cat x > y", "ls $(whoami)", "ls && rm -rf /",
                "ls\nrm -rf /", "echo `id`", "ls | xargs rm"):
        assert zosd.judge_shell(cmd) is False, cmd


def test_allowlist_accepts_readonly_only():
    for cmd in ("ls -la /tmp", "git status --short", "tmux ls", "df -h", "date"):
        assert zosd.judge_shell(cmd) is True, cmd
    for cmd in ("git push", "rm -rf /", "sudo -A apt update", "dd if=/dev/zero of=x",
                "mv a b", "chmod 777 /etc"):
        assert zosd.judge_shell(cmd) is False, cmd


# ---- gate polarity ---------------------------------------------------------

def test_gate_fails_closed_on_unknown_tool():
    # A tool name nobody predicted must prompt, not run. This is the property an
    # inverted (guarded-list) gate would silently lose.
    assert zosd.Daemon()._decide_fast("SomeToolInventedNextYear", {})[0] is None


def test_safe_tools_auto_allow():
    d = zosd.Daemon()
    for name in ("notify", "job_list"):
        assert d._decide_fast(name, {})[0] is True, name


def test_dangerous_host_tools_prompt():
    d = zosd.Daemon()
    for name, args in (("run_shell", {"command": "rm -rf /tmp/x"}),
                       ("type", {"text": "hello"}),
                       ("key", {"keys": "ctrl+alt+t"}),
                       ("click", {"x": 10, "y": 10}),
                       ("job_start", {"name": "x", "cmd": "sleep 1"}),
                       ("delegate", {"agent": "codex", "task": "do a thing"})):
        assert d._decide_fast(name, args)[0] is None, name


def test_typing_is_never_exempted_by_the_allowlist():
    # type/key/click inject into whatever has focus; no shell allowlist applies.
    d = zosd.Daemon()
    assert d._decide_fast("type", {"text": "ls"})[0] is None
    assert d._decide_fast("type", {"text": "git status"})[0] is None


# ---- modes -----------------------------------------------------------------

def test_fresh_daemon_is_guarded():
    assert zosd.Daemon().auto is False


def test_auto_is_sticky_and_never_expires():
    d = zosd.Daemon()
    d.auto, d.current_source = True, "user"
    for _ in range(200):
        assert d._decide_fast("run_shell", {"command": "rm -rf /tmp/x"})[0] is True
    assert d.auto is True


def test_auto_applies_only_to_the_human_at_the_keyboard():
    d = zosd.Daemon()
    d.auto, d.current_source = True, "cron"
    assert d._decide_fast("run_shell", {"command": "rm -rf /tmp/x"})[0] is None


def test_mode_matcher_is_strict():
    assert zosd.match_mode("auto") is True
    assert zosd.match_mode("  Guarded  ") is False
    for text in ("go full auto", "don't go full auto", "auto for 30m",
                 "list my files", "autopsy"):
        assert zosd.match_mode(text) is None, text


def test_non_user_source_cannot_change_mode():
    d = zosd.Daemon()
    asyncio.run(d.set_mode(True, "cron"))
    assert d.auto is False, "a non-user source must not be able to enable auto"


def test_mode_command_never_reaches_the_model():
    # The prompt-injection defence: mode text is consumed by the daemon.
    d = zosd.Daemon()
    routed = []

    async def spy(text):
        routed.append(text)
        return ""

    d.route = spy

    async def drive(payload):
        r, w = await asyncio.open_unix_connection(zosd.SOCK)
        w.write(json.dumps(payload).encode()); await w.drain(); w.write_eof()
        await r.read(); w.close()

    async def main():
        zosd.SOCK.unlink(missing_ok=True)
        srv = await asyncio.start_unix_server(d.handle, path=zosd.SOCK)
        await drive({"source": "user", "text": "auto"})
        await drive({"source": "user", "text": "guarded"})
        await drive({"source": "user", "text": "list my files"})
        await asyncio.sleep(0.15)
        srv.close()
        zosd.SOCK.unlink(missing_ok=True)

    asyncio.run(main())
    assert routed == ["list my files"], routed


def test_a_mode_command_lands_while_a_request_is_still_running():
    """`guarded` is the emergency brake, and a brake that queues is not a brake.

    handle() consumes mode text *before* taking the request lock, so the one situation
    the brake exists for — a request already running in auto mode, doing something the
    user wants stopped — cannot delay it. The in-flight request keeps going, but every
    gate decision it has not yet made now prompts.

    Pinned because the bug would be introduced by tidying: serialising mode changes with
    everything else looks more correct, reads as a race being fixed, and silently removes
    the only way to intervene in a request that is already under way. Nothing else in the
    suite would notice — the mode tests all run against an idle daemon.

    Held directly rather than by starting a real request: the lock is the whole mechanism,
    and taking it is what a router mid-flight does. If the mode path ever moves behind it
    this times out and fails rather than deadlocking the suite.
    """
    d = zosd.Daemon()
    d.auto = True                      # set directly; set_mode(True) raises a real badge

    async def main():
        zosd.SOCK.unlink(missing_ok=True)
        srv = await asyncio.start_unix_server(d.handle, path=zosd.SOCK)
        async with d.lock:             # stands in for a request mid-flight
            r, w = await asyncio.open_unix_connection(zosd.SOCK)
            w.write(json.dumps({"source": "user", "text": "guarded"}).encode())
            await w.drain(); w.write_eof()
            await r.read(); w.close()
            for _ in range(40):        # ~2s, far longer than an un-blocked path needs
                if d.auto is False:
                    break
                await asyncio.sleep(0.05)
            # Read *here*, still inside the lock. Asserting on d.auto after the block is
            # a tautology that passes either way: releasing the lock lets a blocked
            # handle() finish, so the mode flips regardless and the test proves only that
            # set_mode eventually ran. Caught by mutation — moving the mode path behind
            # the lock left the first version of this test green.
            flipped_while_locked = d.auto is False
        srv.close()
        zosd.SOCK.unlink(missing_ok=True)
        return flipped_while_locked

    assert asyncio.run(main()) is True, \
        "a mode command must not wait for the running request to finish"


# ---- prompt ----------------------------------------------------------------

def test_broken_prompt_is_fail_not_deny():
    # "fail" must be distinguishable from "deny": a broken prompt is not a user
    # decision, and only an explicit Deny should read as one.
    saved = zosd.PROMPT
    zosd.PROMPT = "zos-no-such-binary"
    try:
        assert asyncio.run(zosd.prompt_user("do a thing", "rm -rf /")) == "fail"
    finally:
        zosd.PROMPT = saved


def test_only_exit_zero_is_an_allow():
    # The whole permission model rests on this mapping. zenity exits 1 for Deny AND
    # for a closed window, 5 for its own --timeout, and anything else is an unknown
    # state — so every code except 0 must block.
    expected = {0: "allow", 1: "deny", 5: "fail", 2: "fail", 127: "fail", 255: "fail"}
    with tempfile.TemporaryDirectory() as td:
        for rc, want in expected.items():
            stub = pathlib.Path(td, f"exit{rc}")
            stub.write_text(f"#!/bin/sh\nexit {rc}\n")
            stub.chmod(0o755)
            saved, zosd.PROMPT = zosd.PROMPT, str(stub)
            try:
                got = asyncio.run(zosd.prompt_user("an intent", "a detail"))
            finally:
                zosd.PROMPT = saved
            assert got == want, (rc, got, want)


def test_prompt_timeout_returns_fail_and_never_raises():
    # An error in the timeout cleanup path used to escape prompt_user, so "must
    # prompt" became a dead request: nothing ran, nothing was audited, and the
    # daemon looked crashed. prompt_user must always yield one of three strings.
    with tempfile.TemporaryDirectory() as td:
        hang = pathlib.Path(td, "hang")
        hang.write_text('#!/bin/sh\ntrap "" TERM\nsleep 30\n')
        hang.chmod(0o755)
        zosd.PROMPT, saved = str(hang), zosd.PROMPT_TIMEOUT
        zosd.PROMPT_TIMEOUT = 1
        try:
            assert asyncio.run(zosd.prompt_user("an intent", "a detail")) == "fail"
        finally:
            zosd.PROMPT, zosd.PROMPT_TIMEOUT = "zenity", saved


def test_a_timed_out_prompt_is_audited_as_a_denial():
    # The verdict must reach the log even when no human ever answered.
    with tempfile.TemporaryDirectory() as td:
        hang = pathlib.Path(td, "hang")
        hang.write_text('#!/bin/sh\ntrap "" TERM\nsleep 30\n')
        hang.chmod(0o755)
        zosd.PROMPT, saved = str(hang), zosd.PROMPT_TIMEOUT
        zosd.PROMPT_TIMEOUT = 1
        before = zosd.AUDIT.read_text().count("\n") if zosd.AUDIT.exists() else 0
        try:
            allow, why = asyncio.run(
                zosd.Daemon()._gate("run_shell", {"command": "rm -rf /"}))
        finally:
            zosd.PROMPT, zosd.PROMPT_TIMEOUT = "zenity", saved
    assert allow is False and why == "prompt failed", (allow, why)
    assert zosd.AUDIT.read_text().count("\n") == before + 1


def test_the_prompt_defaults_to_deny_so_a_stray_enter_cannot_allow():
    """Without --default-cancel, Allow is the default button and one Enter approves. The
    dialog takes focus, so an Enter meant for another window lands on the gate. Measured
    against real zenity: Enter returned 0 before this flag and 1 after."""
    with tempfile.TemporaryDirectory() as td:
        rec = pathlib.Path(td, "argv")
        fake = pathlib.Path(td, "fake-zenity")
        fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {rec}\nexit 1\n')
        fake.chmod(0o755)
        zosd.PROMPT, saved = str(fake), zosd.PROMPT
        try:
            asyncio.run(zosd.prompt_user("some intent", "some detail"))
        finally:
            zosd.PROMPT = saved
        argv = rec.read_text().split("\n")
    assert "--default-cancel" in argv, argv


def test_gate_blocks_when_the_prompt_fails():
    saved = zosd.PROMPT
    zosd.PROMPT = "zos-no-such-binary"
    try:
        allow, why = asyncio.run(
            zosd.Daemon()._gate("run_shell", {"command": "rm -rf /"}))
    finally:
        zosd.PROMPT = saved
    assert allow is False and why == "prompt failed", (allow, why)


# ---- router loop -----------------------------------------------------------

def _stub(daemon, script):
    """Replace the model with a canned list of assistant messages."""
    steps = iter(script)

    async def fake(http, messages):
        return next(steps)

    daemon._call_model = fake


def _call(name, args, cid="c1"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]}


def test_router_stops_on_a_tool_less_response():
    d = zosd.Daemon()
    _stub(d, [{"content": "all done"}])
    assert asyncio.run(d.route("hello")) == "all done"


def test_router_executes_a_safe_tool_and_feeds_the_result_back():
    d = zosd.Daemon()
    seen = []

    async def fake_notify(a):
        seen.append(a["text"])
        return "notified"

    d.handlers["notify"] = fake_notify
    _stub(d, [_call("notify", {"text": "hi there"}), {"content": "told them"}])
    assert asyncio.run(d.route("say hi")) == "told them"
    assert seen == ["hi there"]


def test_router_caps_runaway_loops():
    d = zosd.Daemon()
    d.auto, d.current_source = True, "user"   # auto so nothing prompts
    calls = []

    async def fake_shell(a):
        calls.append(a["command"])
        return "ok"

    d.handlers["run_shell"] = fake_shell
    # Each command must differ, or the repeat guard below stops the loop long before the
    # cap and this stops testing the cap at all.
    _stub(d, [_call("run_shell", {"command": f"echo loop {i}"}, f"c{i}")
              for i in range(zosd.MAX_STEPS + 5)])
    out = asyncio.run(d.route("loop forever"))
    assert "gave up" in out, out
    assert len(calls) == zosd.MAX_STEPS, len(calls)


def test_router_stops_when_a_call_repeats_identically():
    """A model that re-issues the same state-changing call with the same arguments has
    stopped making progress. Observed live: an identical vm_snapshot, then every remaining
    step spent on host commands nobody asked for."""
    d = zosd.Daemon()
    d.auto, d.current_source = True, "user"      # auto so nothing prompts
    calls = []

    async def fake_shell(a):
        calls.append(a["command"])
        return "ok"

    d.handlers["run_shell"] = fake_shell
    _stub(d, [_call("run_shell", {"command": "echo same"}, f"c{i}") for i in range(6)])
    out = asyncio.run(d.route("do it"))
    assert "repeated" in out, out
    assert calls == ["echo same"], calls          # the second one never ran


def test_a_repeat_is_judged_on_arguments_not_just_the_tool():
    d = zosd.Daemon()
    d.auto, d.current_source = True, "user"
    calls = []

    async def fake_shell(a):
        calls.append(a["command"])
        return "ok"

    d.handlers["run_shell"] = fake_shell
    _stub(d, [_call("run_shell", {"command": "echo one"}, "c1"),
              _call("run_shell", {"command": "echo two"}, "c2"),
              {"content": "done"}])
    assert asyncio.run(d.route("two things")) == "done"
    assert calls == ["echo one", "echo two"], calls


def test_pollable_tools_may_repeat_because_that_is_how_watching_works():
    """job_read polling the same pane is the documented delegation loop, so the repeat
    guard must not break it."""
    d = zosd.Daemon()
    d.auto, d.current_source = True, "user"
    reads = []

    async def fake_read(a):
        reads.append(a["name"])
        return "worker output"

    d.handlers["job_read"] = fake_read
    _stub(d, [_call("job_read", {"name": "zos-w-codex"}, "c1"),
              _call("job_read", {"name": "zos-w-codex"}, "c2"),
              _call("job_read", {"name": "zos-w-codex"}, "c3"),
              {"content": "worker is done"}])
    assert asyncio.run(d.route("watch the worker")) == "worker is done"
    assert len(reads) == 3, reads


def test_a_stopped_repeat_leaves_history_the_model_can_be_sent_again():
    """Returning mid-batch would leave an assistant message carrying tool_calls with no
    matching tool results — malformed, and it poisons the *next* request, not this one."""
    d = zosd.Daemon()
    d.auto, d.current_source = True, "user"

    async def fake_shell(a):
        return "ok"

    d.handlers["run_shell"] = fake_shell
    _stub(d, [_call("run_shell", {"command": "echo same"}, f"c{i}") for i in range(6)])
    asyncio.run(d.route("do it"))
    ids = {m["id"] for msg in d.history for m in (msg.get("tool_calls") or [])}
    answered = {msg["tool_call_id"] for msg in d.history if msg.get("role") == "tool"}
    assert ids <= answered, (ids, answered)


def test_blocked_tool_never_executes_and_the_model_is_told():
    d = zosd.Daemon()                        # guarded, prompt will fail -> deny
    ran = []

    async def fake_shell(a):
        ran.append(a["command"])
        return "should not happen"

    d.handlers["run_shell"] = fake_shell
    saved, zosd.PROMPT = zosd.PROMPT, "zos-no-such-binary"
    captured = {}

    async def fake(http, messages):
        if len(messages) > 2:
            captured["tool_result"] = messages[-1]["content"]
            return {"content": "understood"}
        return _call("run_shell", {"command": "rm -rf /"})

    d._call_model = fake
    try:
        asyncio.run(d.route("delete everything"))
    finally:
        zosd.PROMPT = saved
    assert ran == [], "a blocked command must not run"
    assert "blocked by Z.OS gate" in captured["tool_result"], captured


def test_malformed_tool_arguments_do_not_crash_the_loop():
    d = zosd.Daemon()

    async def fake(http, messages):
        if len(messages) > 2:
            assert "not valid JSON" in messages[-1]["content"]
            return {"content": "recovered"}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "notify", "arguments": "{not json"}}]}

    d._call_model = fake
    assert asyncio.run(d.route("break it")) == "recovered"


def test_handler_exception_becomes_a_tool_result():
    d = zosd.Daemon()

    async def boom(a):
        raise RuntimeError("kaboom")

    d.handlers["notify"] = boom

    async def fake(http, messages):
        if len(messages) > 2:
            assert "kaboom" in messages[-1]["content"], messages[-1]
            return {"content": "handled"}
        return _call("notify", {"text": "x"})

    d._call_model = fake
    assert asyncio.run(d.route("x")) == "handled"


# ---- audit -----------------------------------------------------------------

def test_the_suite_never_writes_the_real_audit_log():
    # Guards the redirect at the top of this file. If someone drops it, the suite
    # silently starts forging lines into the production trail; this fails instead.
    assert zosd.AUDIT.parent != pathlib.Path.home() / ".local/share/zos"


def test_audit_writes_one_line_per_verdict_in_both_modes():
    d = zosd.Daemon()
    before = zosd.AUDIT.read_text().count("\n") if zosd.AUDIT.exists() else 0
    asyncio.run(d._gate("notify", {"text": "x"}))          # guarded, safe
    d.auto, d.current_source = True, "user"
    asyncio.run(d._gate("job_list", {}))                    # auto, safe
    after = zosd.AUDIT.read_text().count("\n")
    assert after == before + 2, (before, after)
    last = json.loads(zosd.AUDIT.read_text().splitlines()[-1])
    for field in ("ts", "tool", "tier", "verdict", "reason", "mode", "source"):
        assert field in last, field


def test_a_denial_is_terminal_for_the_rest_of_the_request():
    # Observed live: one Deny of `touch X` was followed by `python3 -c open(X)` and
    # then job_start carrying the same command. All three were denied, so nothing
    # ran — but one decision became a queue of dialogs, and prompt fatigue is how a
    # queue of dialogs eventually produces an Allow.
    d = zosd.Daemon()
    ran, told = [], []

    async def fake_shell(a):
        ran.append(a["command"])
        return "ok"

    async def fake_start(a):
        ran.append(a["cmd"])
        return "ok"

    async def fake_notify(a):
        told.append(a["text"])
        return "notified"

    d.handlers.update(run_shell=fake_shell, job_start=fake_start, notify=fake_notify)

    prompts = []

    async def deny(intent, detail):
        prompts.append(detail)
        return "deny"

    _stub(d, [_call("run_shell", {"command": "touch /tmp/x"}, "c1"),
              _call("run_shell", {"command": "python3 -c open('/tmp/x','w')"}, "c2"),
              _call("job_start", {"name": "mk", "cmd": "touch /tmp/x"}, "c3"),
              _call("notify", {"text": "could not do that"}, "c4"),
              {"content": "reported"}])
    saved, zosd.prompt_user = zosd.prompt_user, deny
    try:
        assert asyncio.run(d.route("create /tmp/x")) == "reported"
    finally:
        zosd.prompt_user = saved

    assert ran == [], f"nothing may run after a denial: {ran}"
    assert len(prompts) == 1, f"the user must be asked once, not per retry: {prompts}"
    assert told == ["could not do that"], f"notify must stay reachable: {told}"


def test_a_failed_prompt_does_not_silence_the_rest_of_the_turn():
    # A broken prompt is not the user's decision, so it must not stand as one.
    d = zosd.Daemon()
    saved, zosd.PROMPT = zosd.PROMPT, "zos-no-such-binary"
    try:
        allow, why = asyncio.run(d._gate("run_shell", {"command": "rm -rf /"}))
    finally:
        zosd.PROMPT = saved
    assert (allow, why) == (False, "prompt failed"), (allow, why)
    assert d.denied is False


def test_a_standing_denial_does_not_leak_into_the_next_request():
    d = zosd.Daemon()
    d.denied = True
    _stub(d, [{"content": "fresh"}])
    asyncio.run(d.route("a new request"))
    assert d.denied is False


# ---- the user always finds out ---------------------------------------------

def test_auto_mode_narrates_the_command_it_ran():
    # The audit line proves only the verdict. In sticky auto this notification is
    # the sole real-time signal a host action happened, and it is a separate code
    # path (fire-and-forget subprocess), so it needs its own check.
    d = zosd.Daemon()
    d.auto, d.current_source = True, "user"

    async def gate_and_settle():
        await d._gate("run_shell", {"command": "touch /tmp/zos-narration-check"})
        await asyncio.sleep(0.3)          # not awaited by _gate; let it land

    with tempfile.TemporaryDirectory() as td:
        zosd.NOTIFY, log = _fake_notify(td)
        try:
            asyncio.run(gate_and_settle())
            line = log.read_text()
        finally:
            zosd.NOTIFY = "notify-send"
    assert "Z.OS (auto)" in line, line
    assert "touch /tmp/zos-narration-check" in line, line


def test_a_tool_less_reply_still_reaches_the_user():
    # route() returning plain text used to be a silent no-op: handle() discarded
    # the return value, so a model that answered without calling notify produced
    # no notification, no audit line and no log output. Nothing at all.
    d = zosd.Daemon()
    _stub(d, [{"content": "nothing needed doing"}])

    async def drive():
        zosd.SOCK.unlink(missing_ok=True)
        srv = await asyncio.start_unix_server(d.handle, path=zosd.SOCK)
        r, w = await asyncio.open_unix_connection(zosd.SOCK)
        w.write(json.dumps({"source": "user", "text": "do nothing"}).encode())
        await w.drain(); w.write_eof()
        await r.read(); w.close()
        await asyncio.sleep(0.3)
        srv.close()
        zosd.SOCK.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory() as td:
        zosd.NOTIFY, log = _fake_notify(td)
        try:
            asyncio.run(drive())
            out = log.read_text()
        finally:
            zosd.NOTIFY = "notify-send"
    assert "nothing needed doing" in out, out


# ---- tools -----------------------------------------------------------------

def test_schemas_match_handlers_and_serialize():
    assert {s["function"]["name"] for s in tools.SCHEMAS} == set(tools.HANDLERS)
    json.dumps(tools.SCHEMAS)


def test_render_never_truncates_the_decision():
    task = "x" * 500
    assert task in tools.render("delegate", {"agent": "codex", "task": task})
    assert tools.render("run_shell", {"command": "rm -rf /"}) == "rm -rf /"


def test_delegate_rejects_an_unknown_agent():
    out = asyncio.run(tools.HANDLERS["delegate"]({"agent": "hal9000", "task": "x"}))
    assert "unknown agent" in out, out


def test_shell_timeout_is_enforced():
    assert "timed out" in asyncio.run(tools.shell("sleep 5", timeout=1))


def test_job_start_creates_a_real_tmux_session_and_job_kill_removes_it():
    name = "zos-selftest"
    run = lambda c: asyncio.run(c)
    listing = lambda: run(tools.HANDLERS["job_list"]({}))
    run(tools.HANDLERS["job_kill"]({"name": name}))
    run(tools.HANDLERS["job_start"]({"name": name, "cmd": "sleep 60"}))
    assert name in listing()
    run(tools.HANDLERS["job_kill"]({"name": name}))
    assert name not in listing()


# ---- VM tier ---------------------------------------------------------------

def test_vm_restore_is_guarded_despite_the_prefix():
    # Everything vm_* is Safe because the guest is disposable — except the one tool
    # that destroys guest state the user may still want. A name-prefix rule would
    # have swept this in.
    import vm
    d = zosd.Daemon()
    if "vm_restore" not in d.handlers:
        print("  (skipped: no VM running)", end=" ")
        return
    assert d._decide_fast("vm_restore", {"name": "clean"})[0] is None
    assert "vm_restore" not in vm.SAFE


def test_vm_tools_are_safe_when_registered():
    d = zosd.Daemon()
    if "vm_see" not in d.handlers:
        print("  (skipped: no VM running)", end=" ")
        return
    for name in ("vm_see", "vm_type", "vm_click", "vm_shell", "vm_snapshot"):
        assert d._decide_fast(name, {"text": "x", "command": "ls",
                                     "x": 1, "y": 1, "name": "s"})[0] is True, name
        assert d.tier[name] == "vm", name


def test_the_vm_tier_never_widens_the_host_tier():
    # Registering the VM must not make a single host tool auto-allow.
    d = zosd.Daemon()
    for name in ("run_shell", "type", "key", "click", "job_start", "delegate"):
        assert d.tier[name] == "host", name
        assert name not in d.safe, name


def test_qmp_round_trip_against_a_live_guest():
    import vm
    if not vm.available():
        print("  (skipped: no QMP socket)", end=" ")
        return
    out = asyncio.run(vm.HANDLERS["vm_status"]({}))
    assert "guest is" in out, out


def test_snapshots_address_the_node_name_not_the_device_alias():
    # snapshot-save takes a block-graph NODE name. "virtio0" is the legacy device
    # alias and fails with `No block device node 'virtio0'`; the auto-generated node
    # name changes every boot, so zos-vm.service pins node-name=zosdisk.
    import vm
    assert vm.DISK == "zosdisk"
    if not vm.available():
        print("  (skipped: no QMP socket)", end=" ")
        return
    nodes = [d["inserted"]["node-name"]
             for d in asyncio.run(vm.qmp("query-block"))["return"] if d.get("inserted")]
    assert vm.DISK in nodes, nodes


def test_vm_keys_maps_shift_and_specials():
    import vm
    assert vm._keys("aA") == [("a", False), ("a", True)]
    assert vm._keys("a b") == [("a", False), ("spc", False), ("b", False)]


# ---- hotkey ----------------------------------------------------------------

def test_input_event_struct_matches_the_kernel_layout():
    # 24 bytes on x86-64: struct timeval (two longs), __u16 type, __u16 code, __s32 value.
    # If this is wrong every field is misread and the hotkey silently never fires.
    assert zosd._EVENT.size == 24, zosd._EVENT.size


def test_meta_then_space_fires_once_not_on_autorepeat():
    h = zosd.Hotkey()
    assert h.feed(zosd.EV_KEY, 125, 1) is False          # Meta down is not the hotkey
    assert h.feed(zosd.EV_KEY, zosd.KEY_SPACE, 1) is True
    # Autorepeat (value 2) must not open a second dialog, nor must the release.
    assert h.feed(zosd.EV_KEY, zosd.KEY_SPACE, 2) is False
    assert h.feed(zosd.EV_KEY, zosd.KEY_SPACE, 0) is False


def test_space_alone_is_not_the_hotkey():
    h = zosd.Hotkey()
    assert h.feed(zosd.EV_KEY, zosd.KEY_SPACE, 1) is False
    h.feed(zosd.EV_KEY, 125, 1)
    h.feed(zosd.EV_KEY, 125, 0)                          # Meta released again
    assert h.feed(zosd.EV_KEY, zosd.KEY_SPACE, 1) is False


def test_the_listener_remembers_nothing_but_the_modifier():
    """The privacy guarantee. Feeding a password's worth of keystrokes must leave no trace
    of them in the detector — only Meta state is ever retained."""
    h = zosd.Hotkey()
    for code in (30, 31, 32, 33, 34, 35, 2, 3, 4):       # a,s,d,f,g,h,1,2,3
        h.feed(zosd.EV_KEY, code, 1)
        h.feed(zosd.EV_KEY, code, 0)
    assert h.held == set(), h.held
    assert h.__dict__ == {"held": set()}, h.__dict__


def test_non_key_events_are_ignored():
    h = zosd.Hotkey()
    h.feed(zosd.EV_KEY, 125, 1)
    assert h.feed(0x03, zosd.KEY_SPACE, 1) is False      # EV_ABS, not a keypress


@contextlib.contextmanager
def _lock_dir():
    """Point the client lock at a scratch directory, so the suite never contends with a
    real client on the live desktop."""
    with tempfile.TemporaryDirectory() as d:
        real, zosd.SOCK = zosd.SOCK, pathlib.Path(d, "zos.sock")
        try:
            yield pathlib.Path(d, "zos-client.lock")
        finally:
            zosd.SOCK = real


def test_no_client_holding_the_lock_means_none_is_open():
    with _lock_dir():
        assert zosd._client_open() is False


def test_a_client_holding_the_lock_is_detected():
    """The dedup that stops one Super+Space opening two boxes. Identifying the client by
    process was wrong twice — `pgrep -f` matches the shell running it, and a /proc cmdline
    scan matches any shell that merely *names* the path (`rm /path/zos`), silencing the
    hotkey for as long as that command runs. argv[0] fails too: the client is a script, so
    argv[0] is the interpreter. The lock is what an actually-running client holds."""
    with _lock_dir() as lock:
        p = subprocess.Popen(["flock", str(lock), "sleep", "3"])
        try:
            time.sleep(0.3)
            assert zosd._client_open() is True
        finally:
            p.kill(), p.wait()
    # and it is released when that client exits
    with _lock_dir():
        assert zosd._client_open() is False


def test_the_daemon_and_the_client_agree_on_the_lock():
    """They derive the path independently — daemon from SOCK, client from
    XDG_RUNTIME_DIR — and if they ever diverge dedup stops working with no symptom but
    two boxes. A rename on either side has to fail somewhere; here."""
    client = pathlib.Path(__file__).with_name("zos").read_text()
    assert zosd.SOCK.with_name("zos-client.lock").name in client
    assert "XDG_RUNTIME_DIR" in client, "client must resolve the same directory as SOCK"


def test_the_client_shows_its_box_even_if_it_cannot_lock():
    """Locking must never be able to suppress the box. GNOME discards the client's stderr,
    so an early exit is indistinguishable from a dead hotkey — the failure this whole
    mechanism exists to prevent."""
    client = pathlib.Path(__file__).with_name("zos").read_text()
    assert "command -v flock" in client, "a missing flock must fall through, not exit"
    assert "{ exec 9>" in client, "an unopenable lock path must fall through, not exit"
    assert "exec 9>&-" in client, "the lock must be released once the box is gone"


def test_testing_the_lock_does_not_keep_it():
    """_client_open takes the lock to test it. If it did not give it straight back, the
    first press would lock out every later one."""
    with _lock_dir():
        assert zosd._client_open() is False
        assert zosd._client_open() is False


# ---- delegation ------------------------------------------------------------

def test_delegate_refuses_without_a_real_directory():
    """The worker would otherwise inherit the daemon's cwd — Z.OS's own repo — and edit
    the wrong tree with write access."""
    for bad in ({}, {"cwd": ""}, {"cwd": "/nonexistent/zos-target"}):
        out = asyncio.run(tools.HANDLERS["delegate"](
            {"agent": "codex", "task": "x", **bad}))
        assert "existing directory" in out, (bad, out)


def test_no_worker_is_started_with_approvals_bypassed():
    """Live sessions mean Z.OS can answer a worker's own prompts, so bypassing them would
    discard the second checkpoint for nothing."""
    for agent, (flags, _seeds) in tools.AGENT_FLAGS.items():
        for banned in ("--dangerously", "--yes-always", "skip-permissions"):
            assert banned not in flags, (agent, flags)


def test_the_delegate_prompt_shows_the_directory_and_the_whole_task():
    task = "add a --upper flag and a test for it, then run the suite"
    shown = tools.render("delegate", {"agent": "codex", "task": task,
                                      "cwd": "/tmp/zos-deleg"})
    assert task in shown and "/tmp/zos-deleg" in shown, shown


def test_job_send_is_never_safe_but_job_read_is():
    # Keystrokes into a session holding a coding agent can do anything that agent can.
    assert "job_send" not in tools.SAFE
    assert "job_read" in tools.SAFE


def test_job_send_prompt_shows_what_will_be_typed():
    shown = tools.render("job_send", {"name": "zos-w-codex", "text": "y", "keys": "Enter"})
    assert "zos-w-codex" in shown and "y" in shown and "Enter" in shown, shown


def test_the_suite_never_touches_the_live_daemons_socket():
    """Guards the redirect at the top of this file. Without it the suite unlinks the
    running daemon's socket and the daemon goes silently deaf — it stays up and logs
    nothing, so the damage surfaces only as a broken pipe in some later client."""
    assert zosd.SOCK != pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "zos.sock"


def _fake_tmux(td, script):
    """tmux is resolved via PATH, so a stub ahead of it makes send-keys deterministic."""
    fake = pathlib.Path(td, "tmux")
    fake.write_text(script)
    fake.chmod(0o755)
    return td


def test_job_send_reports_success_when_the_command_is_silent():
    """sh() folds a silent success into the truthy string "exit 0", so branching on it
    read a working send as a failure — and the model, told the send failed, would send
    the same keystrokes into a live coding agent a second time."""
    with tempfile.TemporaryDirectory() as td:
        _fake_tmux(td, "#!/bin/sh\nexit 0\n")
        saved = os.environ["PATH"]
        os.environ["PATH"] = f"{td}:{saved}"
        try:
            out = asyncio.run(tools.HANDLERS["job_send"](
                {"name": "s", "text": "y", "keys": "Enter"}))
        finally:
            os.environ["PATH"] = saved
    assert "could not" not in out, out
    assert "sent" in out and "Enter" in out, out


def test_job_send_still_reports_a_real_failure():
    with tempfile.TemporaryDirectory() as td:
        _fake_tmux(td, "#!/bin/sh\necho 'no such session: nope' >&2\nexit 1\n")
        saved = os.environ["PATH"]
        os.environ["PATH"] = f"{td}:{saved}"
        try:
            out = asyncio.run(tools.HANDLERS["job_send"]({"name": "nope", "keys": "Enter"}))
        finally:
            os.environ["PATH"] = saved
    assert "could not" in out and "no such session" in out, out


# The runner MUST stay at the very bottom. globals() is evaluated where this block runs,
# so anything defined below it is silently not a test — 14 tests sat below it unnoticed.
if __name__ == "__main__":
    _tests = sorted(k for k in globals() if k.startswith("test_"))
    for _name in _tests:
        globals()[_name]()
        print("ok", _name)
    print(f"all passed ({len(_tests)} tests)")
