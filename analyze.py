"""Static analysis of WIZARD_720.EXE: find code that references the bonus
address 0x004A209C, disassemble around each site, classify read vs write.

Goal: locate the roll generator (the write that sets the bonus at gender-pick)
and the eligibility check (the read/compare that builds the profession list),
so we can patch the game to roll high legitimately.
"""

import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_32

from patch_wizgold import find_exe

# Usage: python analyze.py [hex-address] [path-to-exe]
TARGET_VA = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x004A209C
EXE = find_exe(sys.argv[2] if len(sys.argv) > 2 else None)
if not EXE:
    sys.exit("Could not find the game exe. Pass it: python analyze.py 0x4A209C <path>")
TARGET_BYTES = struct.pack("<I", TARGET_VA)  # 9c 20 4a 00


def parse_pe(data):
    """Return (image_base, [(name, va, raw_ptr, raw_size, vsize), ...])."""
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[e_lfanew:e_lfanew + 4] == b"PE\x00\x00", "not a PE"
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    assert magic == 0x10B, f"not PE32 (magic {magic:#x})"
    image_base = struct.unpack_from("<I", data, opt + 28)[0]
    sec = opt + opt_size
    sections = []
    for i in range(num_sections):
        off = sec + i * 40
        name = data[off:off + 8].rstrip(b"\x00").decode("latin-1")
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((name, va, raw_ptr, raw_size, vsize))
    return image_base, sections


def file_off_to_va(off, image_base, sections):
    for name, va, raw_ptr, raw_size, vsize in sections:
        if raw_ptr <= off < raw_ptr + raw_size:
            return image_base + va + (off - raw_ptr)
    return None


def va_to_file_off(va, image_base, sections):
    rva = va - image_base
    for name, sva, raw_ptr, raw_size, vsize in sections:
        if sva <= rva < sva + max(raw_size, vsize):
            if rva - sva < raw_size:
                return raw_ptr + (rva - sva)
    return None


def main():
    with open(EXE, "rb") as f:
        data = f.read()
    image_base, sections = parse_pe(data)
    print(f"ImageBase 0x{image_base:X}")
    for name, va, rp, rs, vs in sections:
        print(f"  {name:8s} VA 0x{image_base+va:08X} raw 0x{rp:06X} "
              f"rawsize 0x{rs:X} vsize 0x{vs:X}")
    print(f"Target 0x{TARGET_VA:X}, bytes {TARGET_BYTES.hex()}")

    # Find every occurrence of the 4-byte address constant in the file.
    hits = []
    start = 0
    while True:
        idx = data.find(TARGET_BYTES, start)
        if idx == -1:
            break
        hits.append(idx)
        start = idx + 1
    print(f"\n{len(hits)} occurrence(s) of the address constant:\n")

    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = True

    for off in hits:
        va = file_off_to_va(off, image_base, sections)
        if va is None:
            print(f"file 0x{off:X}: (not in a mapped section -- data, skip)")
            continue
        # Disassemble a window starting a bit before the operand so we catch the
        # instruction that uses it. Try several start offsets to align.
        print(f"=== operand at VA 0x{va:08X} (file 0x{off:X}) ===")
        best = None
        for back in range(1, 16):
            win_off = off - back
            win_va = va - back
            code = data[win_off:win_off + 24]
            for ins in md.disasm(code, win_va):
                # Does this instruction's bytes span the operand location?
                if ins.address <= va < ins.address + ins.size:
                    best = (back, ins)
                    break
            if best:
                break
        if best:
            ins = best[1]
            # Print a few instructions of context from the aligned start.
            ctx_off = off - best[0]
            ctx_va = va - best[0]
            for ins2 in list(md.disasm(data[ctx_off:ctx_off + 48], ctx_va))[:5]:
                mark = "  <== bonus" if ins2.address == ins.address else ""
                print(f"  0x{ins2.address:08X}: {ins2.mnemonic:7s} "
                      f"{ins2.op_str}{mark}")
        else:
            print("  (could not align disassembly -- likely a data reference)")
        print()


if __name__ == "__main__":
    main()
