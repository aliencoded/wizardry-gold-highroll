#!/usr/bin/env python3
"""
Wizardry Gold - Combat Action Overlay (standalone, no DLL)
==========================================================

Records what you click during combat and replays named sequences with one
key, so you don't have to walk every character through the menus each round.

It is "linked to the running executable" two ways, but it is NOT injected:
  1. It finds the game WINDOW by the game's PID (not by guessing a title), so
     clicks are anchored to the live window's client area and survive the
     window being moved.
  2. It opens the game PROCESS read-only (ReadProcessMemory, same as
     wizgold_highroll.py) so replay can WAIT on a game-state flag instead of a
     blind sleep -- self-syncing timing. (Memory gating is optional and is
     verified with the `watch` command first; see WATCH below.)

It drives the game only with synthetic input (mouse move + click, optional key
presses). No code is injected and no game memory is written.

COMMANDS
  py wizcombat.py watch              live dump of candidate state flags (verify)
  py wizcombat.py record <name>      capture clicks/keys until you press STOP key
  py wizcombat.py play <name>        replay a sequence into the game window
  py wizcombat.py list               list saved sequences
  py wizcombat.py del <name>         delete a sequence
  py wizcombat.py show <name>        print a sequence's steps

Hotkeys during record: press the STOP key (default F9) to finish; ESC aborts.
Windows only. Run as Administrator (memory read + reliable input need it).
"""

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

# Reuse the proven process-discovery + memory-read plumbing.
import wizgold_highroll as wz

if sys.platform != "win32":
    sys.exit("Windows only (that's where Wizardry Gold lives).")

user32 = ctypes.WinDLL("user32", use_last_error=True)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "wizcombat.cfg.json")

# ---------------------------------------------------------------------------
# Win32 plumbing: windows + input + key state
# ---------------------------------------------------------------------------

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

# SendInput
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_ESCAPE = 0x1B
VK_F9 = 0x78
VK_F12 = 0x7B
# F1..F8 = 0x70..0x77 : usable hotkey slots for `run`.
FKEYS = {0x70 + i: f"F{i + 1}" for i in range(8)}

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse_abs_input(x, y, flags):
    # SendInput absolute coords are 0..65535 normalized across the VIRTUAL
    # desktop (all monitors), so this works when the game is on a secondary
    # monitor whose origin is negative.
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    nx = int((x - vx) * 65535 / max(vw - 1, 1))
    ny = int((y - vy) * 65535 / max(vh - 1, 1))
    mi = MOUSEINPUT(nx, ny, 0,
                    MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                    | MOUSEEVENTF_VIRTUALDESK | flags, 0, 0)
    return INPUT(INPUT_MOUSE, _INPUT_UNION(mi=mi))


def click_screen(x, y, button="L", down_ms=30):
    """Move to a SCREEN pixel and click."""
    if button == "R":
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    _send(_mouse_abs_input(x, y, 0))            # move first (settles hover)
    time.sleep(0.01)
    _send(_mouse_abs_input(x, y, down))
    time.sleep(down_ms / 1000.0)
    _send(_mouse_abs_input(x, y, up))


def press_vk(vk, down_ms=30):
    ki_down = KEYBDINPUT(vk, 0, 0, 0, 0)
    ki_up = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)
    _send(INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki_down)))
    time.sleep(down_ms / 1000.0)
    _send(INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki_up)))


def key_down(vk):
    """True if key currently held (high bit of GetAsyncKeyState)."""
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# ---------------------------------------------------------------------------
# Game window (found by PID, not by title)
# ---------------------------------------------------------------------------

def find_game_hwnd(pid):
    """First visible top-level window owned by `pid` with a real client area."""
    found = []

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            rc = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rc)):
                if rc.right > 50 and rc.bottom > 50:
                    found.append((hwnd, rc.right, rc.bottom))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    if not found:
        return None
    # Biggest client area = the game viewport.
    found.sort(key=lambda t: t[1] * t[2], reverse=True)
    return found[0][0]


def client_origin(hwnd):
    """Screen-pixel (x,y) of the window's client (0,0), and (w,h)."""
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    return pt.x, pt.y, rc.right, rc.bottom


def cursor_pos():
    pt = wintypes.POINT(0, 0)
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


# ---------------------------------------------------------------------------
# Config / sequence storage
# ---------------------------------------------------------------------------

def load_cfg():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"sequences": {}, "gate": None}


def save_cfg(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Game lookup (silent: pick the single obvious match, else prompt)
# ---------------------------------------------------------------------------

def get_game_pid():
    procs = wz.list_processes()
    matches = [(pid, name) for pid, name in procs
               if any(tag in name.lower() for tag in wz.LIKELY_NAMES)]
    if len(matches) == 1:
        return matches[0][0]
    return wz.find_game_pid()  # falls back to its interactive picker


# Candidate state flags to observe for memory-gating. Verify with `watch` which
# of these flips exactly when the combat menu is waiting for your input.
WATCH_CANDIDATES = [
    ("mode_sel_50", 0x4A8050, 4),
    ("mode_sel_52", 0x4A8052, 2),
    ("gamemode_8052b", 0x4A8052, 1),
]


# ---------------------------------------------------------------------------
# watch: live flag dump for verifying the gate
# ---------------------------------------------------------------------------

def cmd_watch(_args):
    pid = get_game_pid()
    if pid is None:
        return
    try:
        mem = wz.Mem(pid)
    except OSError as e:
        print(e)
        return
    print("Watching candidate state flags. Move between exploration, the combat")
    print("menu, and 'waiting for your action'. Note which value flips when the")
    print("game is asking a character to choose an action. Ctrl-C to stop.\n")
    hwnd = find_game_hwnd(pid)
    if hwnd:
        ox, oy, w, h = client_origin(hwnd)
        print(f"game window: '{window_title(hwnd)}'  client {w}x{h} @ ({ox},{oy})\n")
    try:
        last = None
        while True:
            parts = []
            for name, addr, width in WATCH_CANDIDATES:
                v = mem.read_int(addr, width)
                parts.append(f"{name}=0x{addr:X}:{v}")
            line = "  ".join(parts)
            if line != last:           # only print on change -> easy to read
                print(line)
                last = line
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        mem.close()


# ---------------------------------------------------------------------------
# record: capture clicks + keys (window-relative), poll-based
# ---------------------------------------------------------------------------

# Keys we record presses of (combat-relevant). Extend as needed.
RECORD_VKS = {
    0x0D: "Enter", 0x20: "Space", 0x1B: "Esc",
    **{0x30 + i: str(i) for i in range(10)},          # 0-9
    **{0x41 + i: chr(0x41 + i) for i in range(26)},   # A-Z
}


def cmd_record(args):
    name = args.name
    stop_vk = VK_F9
    pid = get_game_pid()
    if pid is None:
        return
    hwnd = find_game_hwnd(pid)
    if not hwnd:
        print("Couldn't find the game window. Is the game showing?")
        return
    ox, oy, w, h = client_origin(hwnd)
    print(f"Recording '{name}'. Game window {w}x{h} @ ({ox},{oy}).")
    print("Click/type your combat sequence in the game now.")
    print("Press F9 to FINISH, ESC to ABORT.\n")

    steps = []
    t0 = time.time()
    # Edge-detection state.
    was_l = was_r = False
    key_was = {vk: False for vk in RECORD_VKS}

    while True:
        now = time.time()
        # Stop / abort
        if key_down(stop_vk):
            break
        if key_down(VK_ESCAPE):
            print("aborted (no save).")
            return

        l = key_down(VK_LBUTTON)
        r = key_down(VK_RBUTTON)
        # Mouse button down-edges -> record a window-relative click.
        if l and not was_l:
            cx, cy = cursor_pos()
            steps.append({"t": round(now - t0, 3), "kind": "click",
                          "btn": "L", "x": cx - ox, "y": cy - oy})
            print(f"  +click L  ({cx - ox},{cy - oy})")
        if r and not was_r:
            cx, cy = cursor_pos()
            steps.append({"t": round(now - t0, 3), "kind": "click",
                          "btn": "R", "x": cx - ox, "y": cy - oy})
            print(f"  +click R  ({cx - ox},{cy - oy})")
        was_l, was_r = l, r

        # Key down-edges.
        for vk, label in RECORD_VKS.items():
            d = key_down(vk)
            if d and not key_was[vk]:
                steps.append({"t": round(now - t0, 3), "kind": "key",
                              "vk": vk, "label": label})
                print(f"  +key {label}")
            key_was[vk] = d

        time.sleep(0.008)   # ~120 Hz poll: catches fast clicks

    # Drop the trailing F9 if it leaked in as a recorded key (it isn't in
    # RECORD_VKS, so it won't, but guard anyway).
    cfg = load_cfg()
    cfg.setdefault("sequences", {})[name] = {
        "client_w": w, "client_h": h, "steps": steps,
    }
    save_cfg(cfg)
    print(f"\nsaved '{name}': {len(steps)} step(s) -> {CONFIG_PATH}")


# ---------------------------------------------------------------------------
# play: replay a sequence into the game window
# ---------------------------------------------------------------------------

def play_sequence(name, hwnd, focus=True):
    """Replay sequence `name` into window `hwnd`. Returns True on success."""
    cfg = load_cfg()
    seq = cfg.get("sequences", {}).get(name)
    if not seq:
        print(f"no sequence named '{name}'.")
        return False

    if focus:
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
    ox, oy, w, h = client_origin(hwnd)

    rw = seq.get("client_w", w) or w
    rh = seq.get("client_h", h) or h
    sx = w / rw if rw else 1.0
    sy = h / rh if rh else 1.0

    steps = seq["steps"]
    prev_t = 0.0
    for i, st in enumerate(steps):
        # Recorded inter-step delay, capped so a long record pause doesn't stall
        # replay. (Memory-gating replaces this once the flag is verified.)
        dt = st.get("t", prev_t) - prev_t
        prev_t = st.get("t", prev_t)
        time.sleep(min(max(dt, 0.0), 1.5) if i else 0.0)
        if st["kind"] == "click":
            click_screen(int(ox + st["x"] * sx), int(oy + st["y"] * sy),
                         st.get("btn", "L"))
        elif st["kind"] == "key":
            press_vk(st["vk"])
    return True


def cmd_play(args):
    name = args.name
    cfg = load_cfg()
    seq = cfg.get("sequences", {}).get(name)
    if not seq:
        print(f"no sequence named '{name}'. `list` to see them.")
        return
    pid = get_game_pid()
    if pid is None:
        return
    hwnd = find_game_hwnd(pid)
    if not hwnd:
        print("Couldn't find the game window.")
        return

    # Bring game to foreground so input lands there.
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    ox, oy, w, h = client_origin(hwnd)

    # If the window was a different size when recorded, scale offsets.
    rw = seq.get("client_w", w) or w
    rh = seq.get("client_h", h) or h
    sx = w / rw if rw else 1.0
    sy = h / rh if rh else 1.0

    steps = seq["steps"]
    print(f"playing '{name}': {len(steps)} step(s) into {w}x{h} window.")
    prev_t = 0.0
    for i, st in enumerate(steps):
        # Pace by the recorded inter-step delay (capped so a long pause while
        # recording doesn't stall replay). Memory-gating will replace this once
        # the gate flag is verified (see `watch`).
        dt = st.get("t", prev_t) - prev_t
        prev_t = st.get("t", prev_t)
        time.sleep(min(max(dt, 0.0), 1.5) if i else 0.0)

        if st["kind"] == "click":
            cx = ox + st["x"] * sx
            cy = oy + st["y"] * sy
            click_screen(int(cx), int(cy), st.get("btn", "L"))
            print(f"  click {st.get('btn','L')} ({st['x']},{st['y']})")
        elif st["kind"] == "key":
            press_vk(st["vk"])
            print(f"  key {st.get('label', st['vk'])}")
    print("done.")


# ---------------------------------------------------------------------------
# list / show / del
# ---------------------------------------------------------------------------

def cmd_list(_args):
    cfg = load_cfg()
    seqs = cfg.get("sequences", {})
    if not seqs:
        print("no saved sequences yet. Record one: py wizcombat.py record <name>")
        return
    for name, seq in seqs.items():
        print(f"  {name:20}  {len(seq.get('steps', []))} step(s)  "
              f"({seq.get('client_w','?')}x{seq.get('client_h','?')})")


def cmd_show(args):
    cfg = load_cfg()
    seq = cfg.get("sequences", {}).get(args.name)
    if not seq:
        print(f"no sequence named '{args.name}'.")
        return
    for st in seq["steps"]:
        if st["kind"] == "click":
            print(f"  t={st['t']:6.2f}  click {st['btn']}  ({st['x']},{st['y']})")
        else:
            print(f"  t={st['t']:6.2f}  key   {st.get('label', st['vk'])}")


def cmd_del(args):
    cfg = load_cfg()
    if args.name in cfg.get("sequences", {}):
        del cfg["sequences"][args.name]
        save_cfg(cfg)
        print(f"deleted '{args.name}'.")
    else:
        print(f"no sequence named '{args.name}'.")


# ---------------------------------------------------------------------------
# bind / run: hotkey-driven playback
# ---------------------------------------------------------------------------

def _fkey_vk(label):
    """'F1'..'F8' -> VK, or None."""
    for vk, name in FKEYS.items():
        if name.lower() == label.lower():
            return vk
    return None


def cmd_bind(args):
    vk = _fkey_vk(args.key)
    if vk is None:
        print(f"key must be F1..F8 (got '{args.key}').")
        return
    cfg = load_cfg()
    if args.name not in cfg.get("sequences", {}):
        print(f"no sequence named '{args.name}'. Record it first.")
        return
    cfg.setdefault("binds", {})[str(vk)] = args.name
    save_cfg(cfg)
    print(f"bound {FKEYS[vk]} -> '{args.name}'.")


def cmd_run(_args):
    cfg = load_cfg()
    binds = {int(k): v for k, v in cfg.get("binds", {}).items()}
    if not binds:
        print("no hotkeys bound. Bind one: py wizcombat.py bind F1 <name>")
        return
    pid = get_game_pid()
    if pid is None:
        return

    print("Hotkey listener running. In-game:")
    for vk in sorted(binds):
        seq = cfg["sequences"].get(binds[vk], {})
        n = len(seq.get("steps", []))
        print(f"  {FKEYS.get(vk, hex(vk))} -> '{binds[vk]}'  ({n} step(s))")
    print("  F12 -> quit listener\n")

    held = {vk: False for vk in list(binds) + [VK_F12]}
    try:
        while True:
            if key_down(VK_F12) and not held[VK_F12]:
                print("quit.")
                break
            held[VK_F12] = key_down(VK_F12)

            for vk in binds:
                d = key_down(vk)
                if d and not held[vk]:
                    hwnd = find_game_hwnd(pid)   # re-acquire (window may move)
                    if not hwnd:
                        print("  game window gone; stopping.")
                        return
                    name = binds[vk]
                    print(f"  {FKEYS.get(vk, hex(vk))} -> playing '{name}' ...")
                    play_sequence(name, hwnd, focus=False)  # already in-game
                    print("  done.")
                    # swallow the physical key still being held
                    while key_down(vk):
                        time.sleep(0.01)
                held[vk] = key_down(vk)
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\nstopped.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Wizardry Gold combat action overlay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch").set_defaults(func=cmd_watch)
    p = sub.add_parser("record"); p.add_argument("name"); p.set_defaults(func=cmd_record)
    p = sub.add_parser("play"); p.add_argument("name"); p.set_defaults(func=cmd_play)
    sub.add_parser("list").set_defaults(func=cmd_list)
    p = sub.add_parser("show"); p.add_argument("name"); p.set_defaults(func=cmd_show)
    p = sub.add_parser("del"); p.add_argument("name"); p.set_defaults(func=cmd_del)
    p = sub.add_parser("bind"); p.add_argument("key"); p.add_argument("name")
    p.set_defaults(func=cmd_bind)
    sub.add_parser("run").set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
