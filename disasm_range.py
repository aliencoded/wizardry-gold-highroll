"""Linearly disassemble VA ranges of the Wizardry Gold exe for analysis.

Usage: python disasm_range.py [path-to-exe]
"""

import struct
import sys
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

from patch_wizgold import find_exe

EXE = find_exe(sys.argv[1] if len(sys.argv) > 1 else None)
if not EXE:
    sys.exit("Could not find the game exe. Pass it: python disasm_range.py <path>")


def parse_pe(data):
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    image_base = struct.unpack_from("<I", data, opt + 28)[0]
    sec = opt + opt_size
    sections = []
    for i in range(num_sections):
        off = sec + i * 40
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((va, raw_ptr, raw_size, vsize))
    return image_base, sections


def va_to_off(va, image_base, sections):
    rva = va - image_base
    for sva, raw_ptr, raw_size, vsize in sections:
        if sva <= rva < sva + max(raw_size, vsize) and rva - sva < raw_size:
            return raw_ptr + (rva - sva)
    return None


RANGES = [
    ("ROLL GENERATOR", 0x475940, 0x475A60),
    ("ELITE FLAG INIT @476840", 0x476840, 0x4768B0),
]


def main():
    with open(EXE, "rb") as f:
        data = f.read()
    image_base, sections = parse_pe(data)
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    for label, start, end in RANGES:
        off = va_to_off(start, image_base, sections)
        print(f"\n===== {label}  (0x{start:X}..0x{end:X}) =====")
        code = data[off:off + (end - start)]
        for ins in md.disasm(code, start):
            raw = ins.bytes.hex()
            print(f"  0x{ins.address:08X}: {raw:<16s} {ins.mnemonic:7s} {ins.op_str}")


if __name__ == "__main__":
    main()
