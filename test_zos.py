#!/usr/bin/env python3
"""Z.OS tests. Plain asserts, no framework. Run: .venv/bin/python test_zos.py

No API calls: route() is exercised with a stubbed _call_model, so the whole router
loop and gate are testable offline and for free.
"""
import asyncio
import json
import pathlib
import tempfile

import tools
import zosd


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
    _stub(d, [_call("run_shell", {"command": "echo loop"}, f"c{i}")
              for i in range(zosd.MAX_STEPS + 5)])
    out = asyncio.run(d.route("loop forever"))
    assert "gave up" in out, out
    assert len(calls) == zosd.MAX_STEPS, len(calls)


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


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
            print("ok", _name)
    print("all passed")
