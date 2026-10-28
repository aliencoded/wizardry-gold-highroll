"""Wizardry Gold -- shared-timer UNIT MISMATCH fix ("ambient stuff runs way too fast").

THE BUG (vanilla; present in the pristine 2013 exe)
---------------------------------------------------
The game paces every subsystem with ONE shared pair of globals:

    [0x4a0b5c]  dword  "last step timestamp"
    [0x4a2af0]  word   "wait N time units"   (each subsystem sets its own N)

~30 inline throttle copies across the exe compute the timestamp the DOS way --
`GetTickCount / 0x37` (55 ms, the 18.2 Hz PC tick):

    call [GetTickCount]; mov ecx,0x37; sub edx,edx; div ecx     <- 55 ms unit

Examples: 0x421C20, 0x4231C8 (combat), 0x42ABBA, 0x434BD0, 0x451855,
0x463CC1, 0x47190E, 0x472982, plus the init stamps at 0x406FC3 / 0x412E8A.
The ambient/idle animation sites (0x421C20, 0x434BD0, 0x451855, 0x463CC1) all
toggle `[0x4a0f84]` and index the sprite table at 0x4add50 -- these are the
flying birds/bats and ambience frame flips.

But ONE throttle -- `0x417851`, the one reached through the case-machine at
0x4178AC -- uses a **shift instead of a divide**:

    call [GetTickCount]; mov [ebp-4],eax; shr [ebp-4],7          <- 128 ms unit
    ...
    call [GetTickCount]; mov [0x4a0b5c],eax; sar [0x4a0b5c],7    <- 128 ms stamp

128 != 55. Someone turned `/55` into `>>7` and changed the time base. Because
0x417851 writes the SAME `[0x4a0b5c]` everyone else reads, the moment it runs
the shared stamp is in the wrong scale:

  * a /55 reader then sees `now/55 - now/128` = a huge positive elapsed
    -> its threshold is always met -> that subsystem free-runs at full CPU
       speed (birds/bats flying in and out constantly, crickets retriggering).
  * the >>7 reader after a /55 write sees `now/128 - now/55` = negative, which
    the unsigned `ja` treats as huge -> it also fires immediately.

So the shared timer is corrupt in BOTH directions whenever the two families
interleave -- and they do: 0x4178AC dispatches case 1 -> 0x4231C8 (/55) and
case 2 -> 0x417851 (>>7) from the same state machine.

THE FIX
-------
Rewrite 0x417851's two timestamp computations to the game's own /55 idiom, so
every throttle in the exe shares one time base. Both edits fit EXACTLY in the
original byte windows -- no code cave, no relocation:

  window A  0x41785A..0x417874 (26 B)   -- also folds the redundant
      xor eax,eax / sub eax,[stamp] / neg eax  into  mov eax,[stamp]
  window B  0x417884..0x417896 (18 B)

After the fix the divisor byte (`push <div>`) is the speed knob for this site,
and it is kept in sync with the other family by leaving it at 55. Lower it to
go faster (55/div = factor), but note that DESYNCS it again -- prefer
patch_speed.py's per-subsystem knobs. --tickdiv is exposed for experiments.

Usage:
  py patch_timer.py --status [path]
  py patch_timer.py --fix [--tickdiv N] [path]
  py patch_timer.py --revert [path]

NOTE: this replaces the two `shr/sar ,7` immediate bytes that patch_speed.py's
--factor tunes. Apply the timer fix and patch_speed --factor are mutually
exclusive; --status here tells you which state the exe is in.
"""

import argparse
import sys

from patch_wizgold import find_exe, is_locked, _read, _write_at

GETTICK = bytes.fromhex("ff156ce46a00")      # call dword [0x6ae46c]
STAMP = bytes.fromhex("5c0b4a00")            # &[0x4a0b5c]
STOCK_DIV = 0x37                             # 55 ms -- the DOS 18.2 Hz tick

# --- window A: 0x41785A .. 0x417874 (26 bytes) -------------------------------
ORIG_A = (
    GETTICK                       # call [GetTickCount]
    + bytes.fromhex("8945fc")     # mov  [ebp-4], eax
    + bytes.fromhex("c16dfc07")   # shr  [ebp-4], 7
    + bytes.fromhex("33c0")       # xor  eax, eax
    + b"\x2b\x05" + STAMP         # sub  eax, [0x4a0b5c]
    + bytes.fromhex("f7d8")       # neg  eax
    + bytes.fromhex("2945fc")     # sub  [ebp-4], eax
)
assert len(ORIG_A) == 26

# --- window B: 0x417884 .. 0x417896 (18 bytes) -------------------------------
ORIG_B = (
    GETTICK                       # call [GetTickCount]
    + b"\xa3" + STAMP             # mov  [0x4a0b5c], eax
    + b"\xc1\x3d" + STAMP + b"\x07"   # sar [0x4a0b5c], 7
)
assert len(ORIG_B) == 18


def _core(div):
    """xor edx,edx; push <div>; pop ecx; div ecx  -- eax = GetTickCount/div."""
    return b"\x33\xd2" + bytes([0x6A, div]) + b"\x59\xf7\xf1"


def new_a(div):
    out = (
        GETTICK
        + _core(div)
        + bytes.fromhex("8945fc")     # mov  [ebp-4], eax
        + b"\xa1" + STAMP             # mov  eax, [0x4a0b5c]
        + bytes.fromhex("2945fc")     # sub  [ebp-4], eax
        + b"\x90\x90"                 # pad to 26
    )
    assert len(out) == len(ORIG_A)
    return out


def new_b(div):
    out = GETTICK + _core(div) + b"\xa3" + STAMP   # mov [0x4a0b5c], eax
    assert len(out) == len(ORIG_B)
    return out


def _find_unique(data, pat, label):
    i = data.find(pat)
    if i == -1:
        return None
    if data.find(pat, i + 1) != -1:
        raise RuntimeError(f"{label} signature is ambiguous (2+ matches)")
    return i


def _find_patched(data, builder, label):
    """Find a patched window regardless of its divisor byte. Returns (off, div)."""
    for div in range(1, 128):
        off = _find_unique(data, builder(div), label)
        if off is not None:
            return off, div
    return None, None


def locate(data):
    """Return (state, a_off, b_off, div).

    state: 'stock' | 'fixed' | 'shift-tuned' | 'mixed' | 'notfound'
    """
    a_stock = _find_unique(data, ORIG_A, "window A")
    b_stock = _find_unique(data, ORIG_B, "window B")
    if a_stock is not None and b_stock is not None:
        return "stock", a_stock, b_stock, None
    a_fix, div_a = _find_patched(data, new_a, "window A (fixed)")
    b_fix, div_b = _find_patched(data, new_b, "window B (fixed)")
    if a_fix is not None and b_fix is not None:
        if div_a != div_b:
            return "mixed", a_fix, b_fix, (div_a, div_b)
        return "fixed", a_fix, b_fix, div_a
    if a_fix is None and b_fix is None and a_stock is None and b_stock is None:
        # Probably patch_speed.py --factor changed the two shift immediates.
        return "shift-tuned", None, None, None
    return "mixed", a_stock or a_fix, b_stock or b_fix, None


def status(path):
    print(f"Exe:    {path}")
    data = _read(path)
    state, a, b, div = locate(data)
    if state == "stock":
        print(f"Sites:  A@0x{a:X}  B@0x{b:X}")
        print("State:  STOCK -- 0x417851 uses the 128 ms (>>7) unit while ~30")
        print("        other throttles use 55 ms (/0x37). Shared stamp [0x4a0b5c]")
        print("        is therefore corrupt: ambient animation/sound free-runs.")
    elif state == "fixed":
        print(f"Sites:  A@0x{a:X}  B@0x{b:X}")
        factor = STOCK_DIV / div
        print(f"State:  FIXED -- 0x417851 now uses /{div} ({div} ms unit)"
              f"{'' if div == STOCK_DIV else f', {factor:.2f}x vs the other throttles'}")
        if div != STOCK_DIV:
            print("        WARNING: div != 55 desyncs this site again. Use --tickdiv 55.")
    elif state == "shift-tuned":
        print("State:  patch_speed.py --factor is applied (shift immediates != 7).")
        print("        Run `py patch_speed.py --revert` first, then --fix here.")
    else:
        print(f"State:  MIXED/UNKNOWN ({div}) -- only one window matches. "
              "Restore a pristine exe.")
    return state


def apply_fix(path, div):
    if not (1 <= div <= 127):
        print("--tickdiv must be 1..127.")
        return 1
    state = status(path)
    if state == "fixed":
        data = _read(path)
        _, a, b, cur = locate(data)
        if cur == div:
            print(f"\nAlready fixed with /{div}. Nothing to do.")
            return 0
    elif state != "stock":
        print("\nRefusing to patch from this state.")
        return 1
    if is_locked(path):
        print("\nThe exe is locked -- close Wizardry Gold first, then rerun.")
        return 1
    data = _read(path)
    _, a, b, _ = locate(data)
    _write_at(path, a, new_a(div))
    _write_at(path, b, new_b(div))
    print(f"\nPatched: 0x417851 now stamps GetTickCount/{div}, matching the "
          f"other ~30 throttles.")
    print("Ambient birds/bats/crickets should settle to their intended rate.")
    return 0


def revert(path):
    data = _read(path)
    state, a, b, _ = locate(data)
    if state == "stock":
        print("Already stock. Nothing to do.")
        return 0
    if state != "fixed":
        print(f"State is '{state}' -- nothing this tool can safely revert.")
        return 1
    if is_locked(path):
        print("The exe is locked -- close Wizardry Gold first, then rerun.")
        return 1
    _write_at(path, a, ORIG_A)
    _write_at(path, b, ORIG_B)
    print("Reverted 0x417851 to the stock >>7 form (bug restored).")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--tickdiv", type=int, default=STOCK_DIV,
                    help=f"ms per time unit (default {STOCK_DIV} = match the "
                         "rest of the game; lower = faster but desyncs again)")
    args = ap.parse_args()

    path = find_exe(args.path)
    if not path:
        sys.exit("Could not find the game exe. Pass it: py patch_timer.py <path>")

    if args.revert:
        sys.exit(revert(path))
    if args.fix:
        sys.exit(apply_fix(path, args.tickdiv))
    status(path)


if __name__ == "__main__":
    main()
