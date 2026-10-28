"""Diff two Wizardry Gold executables and explain what changed.

Use it to settle the novelty question: compare an ORIGINAL (unpatched) wizard.exe
against a copy that Cosmic Forge's roll-boost patch has been applied to. It maps
every changed byte to a virtual address, disassembles original-vs-patched at each
site, and flags whether the change lands in OUR region (the roll generator
0x475940-0x475A60 / the elite-flag guard at 0x475A15) or somewhere else.

Usage:
  python diff_exe.py ORIGINAL.exe COSMICFORGE_PATCHED.exe
"""

import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_32

# Our patch region of interest (file offsets), for "same locus?" detection.
GEN_VA_LO, GEN_VA_HI = 0x475940, 0x475A60   # the bonus-roll generator
ELITE_GUARD_VA = 0x475A15                    # the jne we NOP


def parse_pe(data):
    e = struct.unpack_from("<I", data, 0x3C)[0]
    coff = e + 4
    nsec = struct.unpack_from("<H", data, coff + 2)[0]
    optsz = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    base = struct.unpack_from("<I", data, opt + 28)[0]
    sec = opt + optsz
    sections = []
    for i in range(nsec):
        o = sec + i * 40
        vs, va, rs, rp = struct.unpack_from("<IIII", data, o + 8)
        sections.append((va, rp, rs, vs))
    return base, sections


def off_to_va(off, base, sections):
    for va, rp, rs, vs in sections:
        if rp <= off < rp + rs:
            return base + va + (off - rp)
    return None


def merge_ranges(diffs, gap=8):
    """Group nearby changed offsets into instruction-sized clusters."""
    if not diffs:
        return []
    runs = []
    start = prev = diffs[0]
    for o in diffs[1:]:
        if o - prev <= gap:
            prev = o
        else:
            runs.append((start, prev))
            start = prev = o
    runs.append((start, prev))
    return runs


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: python diff_exe.py ORIGINAL.exe COSMICFORGE_PATCHED.exe")
    a = open(sys.argv[1], "rb").read()
    b = open(sys.argv[2], "rb").read()
    if len(a) != len(b):
        print(f"NOTE: sizes differ ({len(a)} vs {len(b)}) -- comparing min length.")
    n = min(len(a), len(b))
    diffs = [i for i in range(n) if a[i] != b[i]]
    print(f"Original: {sys.argv[1]} ({len(a)} bytes)")
    print(f"Patched:  {sys.argv[2]} ({len(b)} bytes)")
    print(f"Changed bytes: {len(diffs)}")
    if not diffs:
        print("Files are identical in the compared region.")
        return

    base, sections = parse_pe(a)
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    runs = merge_ranges(diffs)
    print(f"Changed in {len(runs)} region(s):\n")

    touched_ours = False
    for lo, hi in runs:
        va = off_to_va(lo, base, sections)
        valabel = f"VA 0x{va:08X}" if va is not None else "(non-code)"
        print(f"=== file 0x{lo:X}..0x{hi:X}  {valabel} ===")
        print(f"  original: {a[lo:hi+1].hex()}")
        print(f"  patched:  {b[lo:hi+1].hex()}")
        if va is not None:
            wlo = max(0, lo - 4)
            wva = off_to_va(wlo, base, sections)
            for tag, blob in (("orig", a), ("patch", b)):
                ins = next(iter(md.disasm(blob[wlo:wlo + 16], wva)), None)
                # disassemble a few from the run start for context
                txt = "; ".join(f"{i.mnemonic} {i.op_str}".strip()
                                for i in list(md.disasm(blob[lo:lo + 16], va))[:3])
                print(f"  {tag:5s} @0x{va:08X}: {txt}")
            if GEN_VA_LO <= va <= GEN_VA_HI:
                touched_ours = True
                near = "  <-- INSIDE the roll generator we analyzed"
                if abs(va - ELITE_GUARD_VA) <= 6:
                    near = "  <-- THE SAME elite-flag guard jne we NOP!"
                print(near)
        print()

    print("=" * 60)
    if touched_ours:
        print("VERDICT: Cosmic Forge modifies the SAME code region we do.")
        print("Compare the exact bytes above to judge same-vs-different mechanism.")
    else:
        print("VERDICT: Cosmic Forge changes a DIFFERENT location than our patch")
        print(f"(nothing in the generator {hex(GEN_VA_LO)}-{hex(GEN_VA_HI)}).")
        print("-> our elite-flag finding is independent of Cosmic Forge's patch.")


if __name__ == "__main__":
    main()
