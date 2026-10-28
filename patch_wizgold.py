"""Wizardry Gold - High Roll Patch
================================

Forces the character-creation BONUS roll into the game's own hidden "elite"
range (21-30) so high-requirement professions like Faery Ninja are selectable
at creation.

How it works (from disassembling WIZARD_720.EXE -- see WRITEUP.md):
  The roll generator normally produces ~5-11 points. It also contains a dormant
  branch: if the flag word at 0x4A20A0 == 1, it rolls random(10)+21 = 21..30.
  The shipped game forces that flag to 0, so the branch never runs. This patch
  NOPs the conditional jump guarding the branch, so the elite roll ALWAYS fires.
  The roll is produced by the game's own code, so profession eligibility (which
  is computed from the roll) sees the high value -- no memory editing, no race.

Safe and reversible:
  - Backs up the exe to <exe>.bak before patching.
  - Locates the patch site by byte signature and verifies it before writing.
  - --revert restores the original bytes.
  - Close the game first (the exe is file-locked while it runs).

Usage:
  python patch_wizgold.py [path-to-WIZARD_720.EXE]   # apply
  python patch_wizgold.py --revert [path]            # undo
  python patch_wizgold.py --status [path]            # show state

If you don't pass a path, the script auto-detects common Steam/GOG install
locations, or you can set the WIZGOLD_EXE environment variable.
"""

import glob
import os
import shutil
import sys

# Signature of the elite-roll guard:
#   movsx eax, word ptr [0x4A20A0]   0F BF 05 A0 20 4A 00
#   cmp   eax, 1                     83 F8 01
#   jne   <skip elite roll>          0F 85 16 00 00 00   <-- we NOP this
GUARD_PREFIX = bytes.fromhex("0fbf05a0204a0083f801")
ORIG_JNE = bytes.fromhex("0f8516000000")   # jne +0x16
NOP6 = b"\x90" * 6                          # 6x nop -> branch always taken
SIG_ORIG = GUARD_PREFIX + ORIG_JNE
SIG_PATCHED = GUARD_PREFIX + NOP6

# The elite roll is `random(spread) + base`. Both are single immediate bytes,
# at these offsets relative to the located `jne` site:
#   jne site +7   -> `push <spread>`  (the random(N) argument)   default 10
#   jne site +0x15-> `add eax, <base>`                            default 21
# So the roll lands in [base, base + spread - 1]; default 21..30.
SPREAD_REL = 7
BASE_REL = 0x15
DEFAULT_SPREAD = 0x0A
DEFAULT_BASE = 0x15

EXE_NAMES = ("WIZARD_720.EXE", "WIZARD.EXE")


# ---------------------------------------------------------------------------
# Locating the game executable
# ---------------------------------------------------------------------------

def _candidate_dirs():
    """Common install locations to search for the game folder."""
    libs = [
        r"Program Files (x86)\Steam\steamapps\common",
        r"Program Files\Steam\steamapps\common",
        r"SteamLibrary\steamapps\common",
        r"Steam\steamapps\common",
        r"GOG Games",
        r"GOG",
        r"Games",
        "",  # drive root (for folders like "Wizardry 7 Gold")
    ]
    out = [os.path.dirname(os.path.abspath(__file__))]
    for drive in ("C:", "D:", "E:", "F:", "G:"):
        for lib in libs:
            base = os.path.join(drive + os.sep, lib) if lib else drive + os.sep
            if os.path.isdir(base):
                out.append(base)
    return out


def find_exe(explicit=None):
    """Resolve the path to the game exe. Returns a path or None.

    Order: explicit arg -> $WIZGOLD_EXE -> auto-detect common locations.
    """
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)
    env = os.environ.get("WIZGOLD_EXE")
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    for base in _candidate_dirs():
        for name in EXE_NAMES:
            # direct, one level deep, two levels deep
            for pat in (name, os.path.join("*", name), os.path.join("*", "*", name)):
                hits = glob.glob(os.path.join(base, pat))
                if hits:
                    return os.path.abspath(hits[0])
    return None


# ---------------------------------------------------------------------------
# Patch logic (locate the site by signature, so it survives version/offset
# differences as long as the guard bytes match)
# ---------------------------------------------------------------------------

def locate(data):
    """Return (jne_offset, state) where state is 'unpatched'/'patched'/None."""
    i = data.find(SIG_ORIG)
    if i != -1:
        return i + len(GUARD_PREFIX), "unpatched"
    i = data.find(SIG_PATCHED)
    if i != -1:
        return i + len(GUARD_PREFIX), "patched"
    return None, None


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _write_at(path, off, data):
    with open(path, "r+b") as f:
        f.seek(off)
        f.write(data)


def is_locked(path):
    try:
        with open(path, "r+b"):
            return False
    except (PermissionError, OSError):
        return True


def current_range(data, off):
    """(lo, hi) the patched elite roll currently produces."""
    spread = data[off + SPREAD_REL]
    base = data[off + BASE_REL]
    return base, base + spread - 1


def range_to_bytes(lo, hi):
    """Validate a desired [lo, hi] and return (spread_byte, base_byte)."""
    if lo > hi:
        lo, hi = hi, lo
    spread = hi - lo + 1
    if not (1 <= spread <= 127) or not (0 <= lo <= 127):
        raise ValueError("range must satisfy 0 <= min and 1..127 span (min<=127)")
    return spread, lo


def status(path):
    print(f"Exe:    {path}")
    print(f"Backup: {'present' if os.path.exists(path + '.bak') else 'none'}")
    data = _read(path)
    off, state = locate(data)
    if state == "unpatched":
        print(f"Site:   0x{off:X} (file offset)")
        print("State:  UNPATCHED (normal rolls ~5-11)")
    elif state == "patched":
        lo, hi = current_range(data, off)
        print(f"Site:   0x{off:X} (file offset)")
        print(f"State:  PATCHED (every creation rolls {lo}-{hi})")
    else:
        print("State:  SIGNATURE NOT FOUND -- unsupported game version/build.")
    return state


def apply_patch(path, lo=None, hi=None):
    state = status(path)
    if state is None:
        return
    custom = lo is not None and hi is not None
    if custom:
        try:
            spread, base = range_to_bytes(lo, hi)
        except ValueError as e:
            print(f"\n{e}")
            return
    if state == "patched" and not custom:
        print("\nAlready patched. Nothing to do.")
        return
    if is_locked(path):
        print("\nThe exe is locked -- close Wizardry Gold first, then rerun.")
        return
    backup = path + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
        print(f"\nBacked up original to:\n  {backup}")
    off, _ = locate(_read(path))
    _write_at(path, off, NOP6)
    if custom:
        _write_at(path, off + SPREAD_REL, bytes([spread]))
        _write_at(path, off + BASE_REL, bytes([base]))
    data = _read(path)
    if locate(data)[1] == "patched":
        rlo, rhi = current_range(data, off)
        print(f"\nPATCHED. Every creation now rolls {rlo}-{rhi}.")
        print("  name -> Faery -> gender -> high roll -> pick Faery Ninja.")
        print("Revert anytime with:  python patch_wizgold.py --revert")
    else:
        print("\nWrite did not stick -- check permissions / run as Administrator.")


def revert(path):
    state = status(path)
    if state is None:
        return
    if state == "unpatched":
        print("\nAlready unpatched. Nothing to do.")
        return
    if is_locked(path):
        print("\nThe exe is locked -- close Wizardry Gold first, then rerun.")
        return
    backup = path + ".bak"
    if os.path.exists(backup):
        shutil.copy2(backup, path)
        print("\nReverted from backup.")
        return
    off, _ = locate(_read(path))
    _write_at(path, off, ORIG_JNE)
    print("\nReverted by restoring the original bytes (no backup file found).")


def main():
    args = sys.argv[1:]
    mode = "patch"
    lo = hi = None
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--revert", "--status", "--help", "-h"):
            mode = a
        elif a == "--min" and i + 1 < len(args):
            i += 1; lo = int(args[i])
        elif a == "--max" and i + 1 < len(args):
            i += 1; hi = int(args[i])
        elif a == "--value" and i + 1 < len(args):
            i += 1; lo = hi = int(args[i])
        else:
            rest.append(a)
        i += 1
    if mode in ("--help", "-h"):
        print(__doc__)
        print("Optional:  --min N --max M   (roll range, default 21 30)")
        print("           --value V          (always roll exactly V)")
        return
    path = find_exe(rest[0] if rest else None)
    if not path:
        print("Could not find WIZARD_720.EXE.")
        print("Pass the path explicitly, e.g.:")
        print(r'  python patch_wizgold.py "D:\Wizardry 7 Gold\WIZARD_720.EXE"')
        print("or set the WIZGOLD_EXE environment variable.")
        return
    if mode == "--revert":
        revert(path)
    elif mode == "--status":
        status(path)
    else:
        apply_patch(path, lo, hi)


if __name__ == "__main__":
    main()
