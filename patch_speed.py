"""Wizardry Gold - Game Speed Patch
=================================

Speeds up the WHOLE game (movement, animations, combat pacing) by a clean
integer factor, with every per-step game rule left intact. This is the real
fix for "Wizardry Gold runs too slow": it is NOT a DOSBox game (it's a native
Win32 exe), and the in-game speed setting is famously useless. The community
`wizfast.exe` tool is for Wizardry 8, not Gold -- so this patch fills the gap.

How it works (from disassembling WIZARD_720.EXE -- see docs/TIMING.md):
  The game's universal step-timer (0x417851) decides "has enough time passed
  for the next movement/animation/combat step?" by converting the real clock
  with `GetTickCount >> 7`, i.e. a 128 ms time unit. Every subsystem sets a
  "wait N units" threshold and polls this timer. Lowering the shift shrinks the
  time unit, so thresholds are met proportionally sooner and the whole game
  runs faster -- uniformly, with all logic (encounters, aging, combat rolls)
  unchanged, because only the wall-clock unit changes.

  Two shift instructions carry the unit and must match:
    0x417863  shr dword [ebp-4], 7        (C1 6D FC 07)
    0x41788F  sar dword [0x4A0B5C], 7      (C1 3D 5C 0B 4A 00 07)

  shift 7 = 1x (stock), 6 = 2x, 5 = 4x, 4 = 8x, 3 = 16x.

Safe and reversible:
  - Locates both sites by byte signature; verifies before writing.
  - The edit is 2 bytes and symmetric, so --revert just restores shift 7.
    No backup file needed (and none of patch_wizgold's backup pitfalls).
  - Close the game first (the exe is file-locked while it runs).

This script tunes three independent things (see docs/TIMING.md):
  --factor  global game speed (movement/animation/combat pacing) via the
            universal step-timer's time-unit shift.
  --search  the dungeon "search"/screen-wipe sweep delay (ms/frame).
  --textms  message/combat TEXT dwell per line (ms; Enter still skips). The
            real fix for Gold's "text too fast". Independent of --factor.

Usage:
  python patch_speed.py [--factor 2|4|8|16] [path]   # apply global speed (def 2x)
  python patch_speed.py --search MS [path]           # search-wipe ms/frame (stock 33)
  python patch_speed.py --textms MS [path]           # text dwell ms (main tier; also quick if --texthalf omitted)
  python patch_speed.py --textms M --texthalf Q [p]  # split tiers: main M, quick Q ms
  python patch_speed.py --revert [path]              # restore everything to stock
  python patch_speed.py --status [path]              # show current state
"""

import argparse
import os
import re
import struct
import sys

from patch_wizgold import find_exe, is_locked, _read, _write_at

# Located instruction prefixes (last byte = shift amount, which we tune):
SHR_PREFIX = bytes.fromhex("c16dfc")          # shr dword [ebp-4], <shift>
SAR_PREFIX = bytes.fromhex("c13d5c0b4a00")    # sar dword [0x4A0B5C], <shift>

STOCK_SHIFT = 7
MIN_SHIFT = 3                                  # 16x; below this the clock is too coarse
FACTOR_TO_SHIFT = {1: 7, 2: 6, 4: 5, 8: 4, 16: 3}
SHIFT_TO_FACTOR = {v: k for k, v in FACTOR_TO_SHIFT.items()}

# Eyeball / dungeon "search" sweep animation has its OWN per-frame delay,
# separate from the global throttle above (so the global --factor doesn't touch
# it). The sweep (0x45C6FE) calls a draw+delay primitive (0x45CA4C) ~50 times;
# each frame busy-waits GetTickCount + 0x21 (33 ms). The delay constant is a
# single immediate byte, and the surrounding bytes are unique in the exe:
#   0x45CB77  call dword [GetTickCount]   FF 15 6C E4 6A 00
#   0x45CB7D  add  eax, 0x21              83 C0 <ms>   <-- we tune <ms>
SEARCH_PREFIX = bytes.fromhex("ff156ce46a0083c0")  # GetTickCount; add eax,<ms>
STOCK_SEARCH_MS = 0x21                              # 33 ms/frame (stock)

# Message/combat TEXT speed. Every "show a line and wait" call passes a dwell
# in ms to the shared text-wait 0x4175AA (-> 0x421EDD / 0x41754C), which waits
# that long OR until Enter. The dwell comes from the message-speed global
# [0x4A0FA8] (stock 50 ms, so text flies by). It is loaded in TWO forms, both
# immediately followed by `push eax; call 0x4175AA`:
#   FULL (46 ms paths): mov ax,[0x4A0FA8]                 66 A1 A8 0F 4A 00            (6B)
#   HALF ([..]/2 paths): movsx eax,[0x4A0FA8]; cdq; sub eax,edx; sar eax,1
#                                                 0F BF 05 A8 0F 4A 00 99 2B C2 C1 F8 01 (13B)
# Patching only FULL left HALF lines fast -> mixed speeds. We rewrite BOTH to
# push a fixed immediate so every line dwells the same time:
#   FULL -> mov ax,<MS>; nop; nop          66 B8 <imm16> 90 90              (6B)
#   HALF -> mov eax,<MS>; nop x8           B8 <imm32> 90*8                  (13B)
# (Enter still skips instantly.) 27 FULL + 19 HALF = 46 sites; ms <= 32767.
TEXT_FULL_STOCK = bytes.fromhex("66a1a80f4a00")              # mov ax,[0x4A0FA8]
TEXT_HALF_STOCK = bytes.fromhex("0fbf05a80f4a00992bc2c1f801")  # movsx ...; sar eax,1
TEXT_FULL_RE = re.compile(rb"\x66\xb8(..)\x90\x90", re.S)
TEXT_HALF_RE = re.compile(rb"\xb8(....)\x90{8}", re.S)
STOCK_TEXT_MS = 0x32                                          # 50 ms/line


def _text_full_patched(ms):
    return b"\x66\xb8" + struct.pack("<H", ms) + b"\x90\x90"


def _text_half_patched(ms):
    return b"\xb8" + struct.pack("<I", ms) + b"\x90" * 8


def _find_one(data, prefix, label):
    """Return the file offset of the shift byte (just after `prefix`)."""
    i = data.find(prefix)
    if i == -1:
        return None
    j = data.find(prefix, i + 1)
    if j != -1:
        raise RuntimeError(f"signature for {label} is ambiguous (found 2+).")
    return i + len(prefix)


def locate(data):
    """Return (shr_off, sar_off) of the two shift bytes, or (None, None)."""
    shr = _find_one(data, SHR_PREFIX, "shr [ebp-4]")
    sar = _find_one(data, SAR_PREFIX, "sar [0x4A0B5C]")
    if shr is None or sar is None:
        return None, None
    return shr, sar


def locate_search(data):
    """Return file offset of the search-sweep per-frame delay byte, or None."""
    return _find_one(data, SEARCH_PREFIX, "search-sweep delay")


def _shift_state(data, shr_off, sar_off):
    s1, s2 = data[shr_off], data[sar_off]
    return s1, s2


def status(path):
    print(f"Exe:    {path}")
    data = _read(path)
    shr_off, sar_off = locate(data)
    if shr_off is None:
        # patch_timer.py --fix replaces the two shift instructions with the
        # game's /55 divide, so --factor no longer applies. The search and text
        # patches are independent, so still report them.
        print("State:  global --factor site not present. Either an unsupported "
              "build, or patch_timer.py --fix is applied (check with "
              "`py patch_timer.py --status`).")
        se = locate_search(data)
        if se is not None:
            ms = data[se]
            tag = "stock" if ms == STOCK_SEARCH_MS else "patched"
            print(f"Search: {ms} ms/frame ({tag}) @0x{se:X}")
        text_status(data)
        return None
    s1, s2 = _shift_state(data, shr_off, sar_off)
    print(f"Sites:  shr@0x{shr_off:X}=shift {s1}, sar@0x{sar_off:X}=shift {s2}")
    if s1 != s2:
        print("State:  INCONSISTENT (the two shifts differ) -- revert to fix.")
        return (s1, s2)
    factor = SHIFT_TO_FACTOR.get(s1, "?")
    if s1 == STOCK_SHIFT:
        print("State:  STOCK (1x speed)")
    else:
        print(f"State:  PATCHED ({factor}x speed)")
    se = locate_search(data)
    if se is not None:
        ms = data[se]
        tag = "stock" if ms == STOCK_SEARCH_MS else "patched"
        print(f"Search: {ms} ms/frame ({tag}) @0x{se:X}")
    text_status(data)
    return (s1, s2)


def _sanity(s1, s2):
    """Both shifts must be in the safe tunable range we understand."""
    for s in (s1, s2):
        if not (MIN_SHIFT <= s <= STOCK_SHIFT):
            return False
    return True


def apply_patch(path, factor):
    state = status(path)
    if state is None:
        return
    s1, s2 = state
    if not _sanity(s1, s2):
        print("\nShift bytes are outside the known range -- refusing to patch. "
              "Restore a pristine exe first.")
        return
    shift = FACTOR_TO_SHIFT[factor]
    if s1 == s2 == shift:
        print(f"\nAlready set to {factor}x. Nothing to do.")
        return
    if is_locked(path):
        print("\nThe exe is locked -- close Wizardry Gold first, then rerun.")
        return
    data = _read(path)
    shr_off, sar_off = locate(data)
    _write_at(path, shr_off, bytes([shift]))
    _write_at(path, sar_off, bytes([shift]))
    print(f"\nPatched: game speed set to {factor}x (shift {shift}).")
    print("Launch the game and check movement/animation. Too fast/slow? "
          "Re-run with a different --factor, or --revert for stock.")


def set_search(path, ms):
    """Set the eyeball/search sweep per-frame delay (ms, 0..127)."""
    if not (0 <= ms <= 127):
        print("\n--search must be 0..127 ms.")
        return
    data = _read(path)
    se = locate_search(data)
    if se is None:
        print("Search-sweep signature not found -- unsupported build.")
        return
    if data[se] == ms:
        print(f"\nSearch already {ms} ms/frame. Nothing to do.")
        return
    if is_locked(path):
        print("\nThe exe is locked -- close Wizardry Gold first, then rerun.")
        return
    _write_at(path, se, bytes([ms]))
    print(f"\nSearch sweep set to {ms} ms/frame (was {data[se]}; stock {STOCK_SEARCH_MS}).")


def _find_text_sites(data):
    """Return dict with offsets for both dwell-load forms (full + half)."""
    return {
        "full_stock": [m.start() for m in re.finditer(re.escape(TEXT_FULL_STOCK), data)],
        "half_stock": [m.start() for m in re.finditer(re.escape(TEXT_HALF_STOCK), data)],
        "full_patched": [(m.start(), struct.unpack("<H", m.group(1))[0]) for m in TEXT_FULL_RE.finditer(data)],
        "half_patched": [(m.start(), struct.unpack("<I", m.group(1))[0]) for m in TEXT_HALF_RE.finditer(data)],
    }


def _text_counts(s):
    stock = len(s["full_stock"]) + len(s["half_stock"])
    patched = s["full_patched"] + s["half_patched"]
    return stock, patched


def _tier_tag(stock_offs, patched, total):
    """Human label for one text tier (e.g. '1000' / 'stock 50' / 'mixed [...]')."""
    if not patched:
        return f"stock {STOCK_TEXT_MS}"
    vals = sorted({m for _, m in patched})
    base = f"{vals[0]}" if len(vals) == 1 else f"mixed {vals}"
    return f"{base} ({len(patched)}/{total})"


def text_status(data):
    s = _find_text_sites(data)
    full_total = len(s["full_stock"]) + len(s["full_patched"])
    half_total = len(s["half_stock"]) + len(s["half_patched"])
    if full_total + half_total == 0:
        print("Text:   message-dwell sites not found.")
        return
    print(f"Text:   main {_tier_tag(s['full_stock'], s['full_patched'], full_total)} ms"
          f"  |  quick {_tier_tag(s['half_stock'], s['half_patched'], half_total)} ms")


def set_text(path, full_ms=None, half_ms=None):
    """Set message/combat text dwell per tier (ms; Enter still skips).

    The game splits combat lines into two dwell tiers: a MAIN tier (full
    message-speed) and a QUICK tier (half). Pass full_ms to set the main tier,
    half_ms to set the quick tier; either may be None to leave that tier alone.
    """
    for label, ms in (("--textms", full_ms), ("--texthalf", half_ms)):
        if ms is not None and not (1 <= ms <= 32767):
            print(f"\n{label} must be 1..32767.")
            return
    data = _read(path)
    s = _find_text_sites(data)
    if len(s["full_stock"]) + len(s["full_patched"]) + len(s["half_stock"]) + len(s["half_patched"]) == 0:
        print("Text-dwell signature not found -- unsupported build.")
        return
    if is_locked(path):
        print("\nThe exe is locked -- close Wizardry Gold first, then rerun.")
        return
    n = 0
    if full_ms is not None:
        new = _text_full_patched(full_ms)
        for off in s["full_stock"]:
            _write_at(path, off, new); n += 1
        for off, cur in s["full_patched"]:
            if cur != full_ms:
                _write_at(path, off, new); n += 1
    if half_ms is not None:
        new = _text_half_patched(half_ms)
        for off in s["half_stock"]:
            _write_at(path, off, new); n += 1
        for off, cur in s["half_patched"]:
            if cur != half_ms:
                _write_at(path, off, new); n += 1
    parts = []
    if full_ms is not None:
        parts.append(f"main={full_ms}")
    if half_ms is not None:
        parts.append(f"quick={half_ms}")
    print(f"\nText dwell set ({', '.join(parts) or 'no tier'}) ms/line; {n} sites changed. "
          f"Press Enter in-game to skip a line early.")


def _revert_text(path, data):
    s = _find_text_sites(data)
    for off, _ in s["full_patched"]:
        _write_at(path, off, TEXT_FULL_STOCK)
    for off, _ in s["half_patched"]:
        _write_at(path, off, TEXT_HALF_STOCK)
    return len(s["full_patched"]) + len(s["half_patched"])


def revert(path):
    data = _read(path)
    shr_off, sar_off = locate(data)
    if shr_off is None:
        print("Signature not found -- nothing to revert.")
        return
    if is_locked(path):
        print("The exe is locked -- close Wizardry Gold first, then rerun.")
        return
    _write_at(path, shr_off, bytes([STOCK_SHIFT]))
    _write_at(path, sar_off, bytes([STOCK_SHIFT]))
    se = locate_search(data)
    if se is not None:
        _write_at(path, se, bytes([STOCK_SEARCH_MS]))
    nt = _revert_text(path, data)
    print(f"Reverted to stock (1x speed, stock search, {nt} text sites restored).")


def main():
    ap = argparse.ArgumentParser(description="Wizardry Gold game-speed patch.")
    ap.add_argument("path", nargs="?", help="path to WIZARD_720.EXE / WIZARD.EXE")
    ap.add_argument("--factor", type=int, choices=sorted(FACTOR_TO_SHIFT),
                    default=2, help="global speed multiplier (default 2)")
    ap.add_argument("--search", type=int, metavar="MS",
                    help="eyeball/search sweep delay in ms/frame (0..127; "
                         f"stock {STOCK_SEARCH_MS}). Lower = faster search.")
    ap.add_argument("--textms", type=int, metavar="MS",
                    help="MAIN text-line dwell in ms (stock "
                         f"{STOCK_TEXT_MS}). Higher = more time to read; Enter "
                         "still skips. Try 800-1500. If --texthalf is omitted, "
                         "this also sets the quick tier (uniform).")
    ap.add_argument("--texthalf", type=int, metavar="MS",
                    help="QUICK (half) text-line dwell in ms. Omit to mirror "
                         "--textms; set lower than --textms to keep the game's "
                         "two-tier feel (e.g. --textms 1200 --texthalf 600).")
    ap.add_argument("--status", action="store_true", help="show current speed")
    ap.add_argument("--revert", action="store_true", help="restore stock")
    args = ap.parse_args()

    path = find_exe(args.path)
    if not path:
        sys.exit("Could not find the game exe. Pass it: python patch_speed.py <path>")

    if args.status:
        status(path)
    elif args.revert:
        revert(path)
    elif args.search is not None:
        set_search(path, args.search)
    elif args.textms is not None or args.texthalf is not None:
        # --textms alone -> both tiers uniform; --texthalf overrides the quick
        # tier; --texthalf alone -> only the quick tier.
        full = args.textms
        half = args.texthalf if args.texthalf is not None else args.textms
        set_text(path, full, half)
    else:
        apply_patch(path, args.factor)


if __name__ == "__main__":
    main()
