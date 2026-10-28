"""General VA-range disassembler for WIZARD_720.EXE.

Usage: py wdis.py <start_va> <end_va> [exe]
"""
import sys
import struct

from capstone import Cs, CS_ARCH_X86, CS_MODE_32

from patch_wizgold import find_exe, _read

IMAGE_BASE = 0x400000


def sections(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    off = pe + 24 + opt
    out = []
    for i in range(nsec):
        e = off + i * 40
        name = data[e:e + 8].rstrip(b"\x00").decode("latin1")
        vaddr = struct.unpack_from("<I", data, e + 12)[0]
        vsize = struct.unpack_from("<I", data, e + 8)[0]
        raw = struct.unpack_from("<I", data, e + 20)[0]
        rawsz = struct.unpack_from("<I", data, e + 16)[0]
        out.append((name, IMAGE_BASE + vaddr, vsize, raw, rawsz))
    return out


def va_to_off(secs, va):
    for _, sva, vsize, raw, rawsz in secs:
        if sva <= va < sva + max(vsize, rawsz):
            return raw + (va - sva)
    return None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    start = int(sys.argv[1], 0)
    end = int(sys.argv[2], 0)
    path = find_exe(sys.argv[3] if len(sys.argv) > 3 else None)
    data = _read(path)
    secs = sections(data)
    o = va_to_off(secs, start)
    if o is None:
        sys.exit(f"VA 0x{start:X} not in any section")
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    for ins in md.disasm(data[o:o + (end - start)], start):
        print(f"  0x{ins.address:08X}: {ins.bytes.hex():<20} {ins.mnemonic:<8} {ins.op_str}")


if __name__ == "__main__":
    main()
