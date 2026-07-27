"""Drive the real daemon's hotkey listener with real kernel input events.

Not part of test_zos.py: that suite is offline and this needs /dev/uinput and the `input`
group. Run it by hand after touching the listener -- `python3 hotkey_check.py`.

Creates a uinput virtual keyboard, points zosd._keyboards at its evdev node, and runs the
actual Daemon.watch_hotkey. Everything from the fd down is production code: real evdev
read, real _on_key offset math, real Hotkey.feed, real _fire_hotkey, real _client_open.

Only the glob is stubbed -- and it is stubbed because _keyboards deliberately excludes
uinput devices (a virtual keyboard Z.OS itself can type on would let `key` trigger the
hotkey). That exclusion is verified separately by listing /dev/input/by-path: on this
machine only the physical keyboard has a -event-kbd symlink.

What this CANNOT cover, and no synthetic test can: that the glob finds a real keyboard, and
that GNOME's <Super>space binding fires. Both need a physical press.
"""
import asyncio, fcntl, os, pathlib, struct, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import zosd

UI = 0x55
UI_DEV_CREATE, UI_DEV_DESTROY = (UI << 8) | 1, (UI << 8) | 2
UI_SET_EVBIT = (1 << 30) | (4 << 16) | (UI << 8) | 100
UI_SET_KEYBIT = (1 << 30) | (4 << 16) | (UI << 8) | 101
EV_SYN, EV_KEY = 0, 1
KEY_SPACE, KEY_LEFTMETA = 57, 125
NAME = b"zos-hotkey-test-kbd"


def make_keyboard():
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
    for code in (KEY_SPACE, KEY_LEFTMETA):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    os.write(fd, struct.pack("80sHHHHi" + "i" * 256, NAME, 0x03, 0x1234, 0x5678, 1, 0,
                             *([0] * 256)))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(0.5)                       # udev has to create the node
    return fd


def node_for(name: bytes) -> str:
    block = ""
    for line in pathlib.Path("/proc/bus/input/devices").read_text().split("\n\n"):
        if f'Name="{name.decode()}"' in line:
            block = line
    ev = [w for w in block.split() if w.startswith("event")]
    if not ev:
        raise SystemExit("virtual keyboard did not appear in /proc/bus/input/devices")
    return f"/dev/input/{ev[0]}"


def count(marker) -> int:
    return marker.read_text().count("fired") if marker.exists() else 0


def emit(fd, etype, code, value):
    os.write(fd, zosd._EVENT.pack(0, 0, etype, code, value))
    os.write(fd, zosd._EVENT.pack(0, 0, EV_SYN, 0, 0))


async def main():
    ui = make_keyboard()
    dev = node_for(NAME)
    print(f"virtual keyboard at {dev}")

    # A fake client, so nothing pops up on the user's screen and "did it launch?" is a
    # file on disk rather than a judgement call. It lingers, which is what lets the
    # second press exercise the dedup branch.
    marker = pathlib.Path("/tmp/zos-hotkey-fired")
    marker.unlink(missing_ok=True)
    # Isolated from the live desktop's lock, so a real client cannot skew this.
    zosd.SOCK = pathlib.Path("/tmp/zos-hotkey-check.sock")
    lock = zosd.SOCK.with_name("zos-client.lock")
    lock.unlink(missing_ok=True)
    client = pathlib.Path("/tmp/zos-fake-client")
    client.write_text(f'#!/bin/sh\nexec 9>"{lock}"\nflock -n 9 || exit 0\n'
                      f'echo fired >> {marker}\nsleep 4\n')
    client.chmod(0o755)
    zosd.CLIENT = str(client)
    zosd._keyboards = lambda: [dev]

    d = zosd.Daemon()
    d.watch_hotkey()

    print("\n-- press 1: meta down, space, meta up")
    emit(ui, EV_KEY, KEY_LEFTMETA, 1)
    emit(ui, EV_KEY, KEY_SPACE, 1)
    emit(ui, EV_KEY, KEY_SPACE, 0)
    emit(ui, EV_KEY, KEY_LEFTMETA, 0)
    await asyncio.sleep(1.5)
    fired = count(marker)
    print(f"clients launched: {fired}  (expect 1)")

    print("\n-- press 2, while the first client is still up (dedup branch)")
    emit(ui, EV_KEY, KEY_LEFTMETA, 1)
    emit(ui, EV_KEY, KEY_SPACE, 1)
    emit(ui, EV_KEY, KEY_SPACE, 0)
    emit(ui, EV_KEY, KEY_LEFTMETA, 0)
    await asyncio.sleep(1.5)
    fired2 = count(marker)
    print(f"clients launched: {fired2}  (expect still 1)")

    print("\n-- space alone, no meta")
    emit(ui, EV_KEY, KEY_SPACE, 1)
    emit(ui, EV_KEY, KEY_SPACE, 0)
    await asyncio.sleep(1.5)
    fired3 = count(marker)
    print(f"clients launched: {fired3}  (expect still 1)")

    await asyncio.sleep(4)                # let the fake client exit
    print("\n-- press 3, after the client exited")
    emit(ui, EV_KEY, KEY_LEFTMETA, 1)
    emit(ui, EV_KEY, KEY_SPACE, 1)
    emit(ui, EV_KEY, KEY_SPACE, 0)
    emit(ui, EV_KEY, KEY_LEFTMETA, 0)
    await asyncio.sleep(1.5)
    fired4 = count(marker)
    print(f"clients launched: {fired4}  (expect 2)")

    fcntl.ioctl(ui, UI_DEV_DESTROY)
    os.close(ui)
    ok = (fired, fired2, fired3, fired4) == (1, 1, 1, 2)
    print("\nRESULT:", "PASS" if ok else f"FAIL {(fired, fired2, fired3, fired4)}")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
