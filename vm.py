"""Z.OS VM tier: QMP over a Unix socket. No dependency — QMP is line-delimited JSON.

Every tool here is Safe in the gate EXCEPT vm_restore, because the guest is
disposable and snapshot-backed. That is only true while the guest has no host
filesystem access; see vm/setup.sh.
"""
import asyncio
import json
import os
import pathlib

VM_SOCK = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "zos-vm.sock"
FRAME = pathlib.Path("/tmp/zos-vm-frame.ppm")

# The snapshot jobs address the block graph by NODE name, not by the legacy device
# alias that query-block reports as "device". Passing "virtio0" fails with
# `No block device node 'virtio0'`, and the auto-generated name (#block112) is
# internal and changes every boot — hence node-name=zosdisk in zos-vm.service.
# The seed ISO is raw and cannot hold a snapshot, so it is deliberately not listed.
DISK = "zosdisk"


def available() -> bool:
    return VM_SOCK.exists()


async def qmp(command: str, **args) -> dict:
    """One QMP call per connection: simpler than a persistent reader, and the VM
    tier is not hot-path. ponytail: reconnect-per-call, pool it if latency shows."""
    reader, writer = await asyncio.open_unix_connection(VM_SOCK)
    try:
        await reader.readline()                        # greeting

        async def send(c, **a):
            payload = {"execute": c, **({"arguments": a} if a else {})}
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    return {"error": {"desc": "QMP closed"}}
                m = json.loads(line)
                if "return" in m or "error" in m:
                    return m                            # skip async events

        await send("qmp_capabilities")
        return await send(command, **args)
    finally:
        writer.close()


def _err(r):
    return r["error"]["desc"] if "error" in r else None


# ---- handlers -------------------------------------------------------------

async def h_vm_status(a):
    r = await qmp("query-status")
    return _err(r) or f"guest is {r['return']['status']}"


async def h_vm_see(a):
    """Returns a marker string; zosd turns the PPM into an image part."""
    FRAME.unlink(missing_ok=True)
    r = await qmp("screendump", filename=str(FRAME))
    if e := _err(r):
        return f"could not capture screen: {e}"
    return f"__ZOS_IMAGE__{FRAME}"


# QMP wants qcodes, not characters. Only what a text console needs; extend as used.
_QCODE = {" ": "spc", "\n": "ret", "\t": "tab", "-": "minus", "=": "equal",
          ".": "dot", ",": "comma", "/": "slash", ";": "semicolon", "'": "apostrophe"}


def _keys(text):
    out = []
    for ch in text:
        if ch in _QCODE:
            out.append((_QCODE[ch], False))
        elif ch.isupper():
            out.append((ch.lower(), True))
        else:
            out.append((ch, False))
    return out


async def h_vm_type(a):
    for qcode, shift in _keys(a["text"]):
        events = []
        if shift:
            events.append({"type": "key", "data": {"down": True, "key": {
                "type": "qcode", "data": "shift"}}})
        events += [{"type": "key", "data": {"down": d, "key": {
            "type": "qcode", "data": qcode}}} for d in (True, False)]
        if shift:
            events.append({"type": "key", "data": {"down": False, "key": {
                "type": "qcode", "data": "shift"}}})
        r = await qmp("input-send-event", events=events)
        if e := _err(r):
            return f"typing failed at {qcode!r}: {e}"
    return f"typed {len(a['text'])} chars into the guest"


async def h_vm_key(a):
    parts = a["keys"].split("+")
    down = [{"type": "key", "data": {"down": True, "key": {
        "type": "qcode", "data": p}}} for p in parts]
    up = [{"type": "key", "data": {"down": False, "key": {
        "type": "qcode", "data": p}}} for p in reversed(parts)]
    r = await qmp("input-send-event", events=down + up)
    return _err(r) or f"pressed {a['keys']} in the guest"


async def h_vm_click(a):
    btn = a.get("button", "left")
    events = [
        {"type": "abs", "data": {"axis": "x", "value": int(a["x"])}},
        {"type": "abs", "data": {"axis": "y", "value": int(a["y"])}},
        {"type": "btn", "data": {"down": True, "button": btn}},
        {"type": "btn", "data": {"down": False, "button": btn}},
    ]
    r = await qmp("input-send-event", events=events)
    return _err(r) or f"clicked {btn} at {a['x']},{a['y']} in the guest"


SSH = ["ssh", "-i", str(pathlib.Path.home() / ".local/share/zos/vm-key"),
       "-p", "2222", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
       "-o", "LogLevel=ERROR", "zos@127.0.0.1"]


async def h_vm_shell(a):
    p = await asyncio.create_subprocess_exec(
        *SSH, a["command"], stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=300)
    except asyncio.TimeoutError:
        p.kill()
        return "guest command timed out after 300s"
    return out.decode(errors="replace").strip()[:16000] or f"exit {p.returncode}"


async def h_vm_snapshot(a):
    tag = a["name"]
    r = await qmp("snapshot-save", **{"job-id": f"save-{tag}", "tag": tag,
                                      "vmstate": DISK, "devices": [DISK]})
    return _err(r) or f"snapshot {tag!r} started"


async def h_vm_restore(a):
    tag = a["name"]
    r = await qmp("snapshot-load", **{"job-id": f"load-{tag}", "tag": tag,
                                      "vmstate": DISK, "devices": [DISK]})
    return _err(r) or f"restoring snapshot {tag!r}"


HANDLERS = {"vm_status": h_vm_status, "vm_see": h_vm_see, "vm_type": h_vm_type,
            "vm_key": h_vm_key, "vm_click": h_vm_click, "vm_shell": h_vm_shell,
            "vm_snapshot": h_vm_snapshot, "vm_restore": h_vm_restore}

# vm_restore is deliberately absent: it destroys guest state the user may want.
SAFE = {"vm_status", "vm_see", "vm_type", "vm_key", "vm_click", "vm_shell",
        "vm_snapshot"}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required}}}


SCHEMAS = [
    _fn("vm_status", "Check whether the sandbox VM is running.", {}, []),
    _fn("vm_see", "Capture the sandbox VM's screen and look at it. Use this to see "
                  "what is on the guest display before typing or clicking.", {}, []),
    _fn("vm_shell", "Run a shell command inside the sandbox VM. PREFERRED over "
                    "run_shell for any command that does not need host-only state. "
                    "The VM user has full sudo; the VM is disposable and snapshottable.",
        {"command": {"type": "string"}}, ["command"]),
    _fn("vm_type", "Type text on the sandbox VM's keyboard.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("vm_key", "Press a key combination in the sandbox VM, e.g. 'ctrl+alt+f2'.",
        {"keys": {"type": "string"}}, ["keys"]),
    _fn("vm_click", "Click at coordinates on the sandbox VM's screen.",
        {"x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]}},
        ["x", "y"]),
    _fn("vm_snapshot", "Save a named snapshot of the VM before doing something risky.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("vm_restore", "Restore the VM to a named snapshot, discarding changes since.",
        {"name": {"type": "string"}}, ["name"]),
]
