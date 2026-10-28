#!/usr/bin/env python3
"""
Wizardry Gold - Rest-loop locator (read-only runtime trace)
===========================================================

Goal: find the code that runs while the party RESTS, so we can shrink the
per-step real-time delay (make rest take ~3 seconds) WITHOUT removing the
per-step game logic (random encounter rolls, aging, healing all still run).

How it finds the loop, safely:
  It NEVER writes to the game. It only samples the call stack. For a few
  seconds it repeatedly: suspends each game thread, reads EIP, scans the
  thread stack for return addresses (a dword that points into the exe's .text
  AND is immediately preceded by a CALL instruction), then resumes the thread.
  Every function on the stack during that instant gets a tally.

  A function that is on the stack the WHOLE time you rest -- but not when you
  stand idle -- is the rest loop. So we take two captures, "idle" and "rest",
  and print the addresses that are hot during REST but cold while IDLE.

Usage (run as Administrator, 64-bit Python is fine -- game is 32-bit WoW64):
    py rest_trace.py                 # interactive: capture idle, then rest
    py rest_trace.py --exe <path>    # pristine exe for call-validation
                                     # (default: WIZARD_720.EXE.bak in game dir)

Output: ranked candidate VAs. Hand the top few back to disassemble.
"""

import argparse
import ctypes
import struct
import sys
import time
from collections import Counter
from ctypes import wintypes

if sys.platform != "win32":
    sys.exit("Windows only (that's where Wizardry Gold lives).")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
THREAD_ALL_ACCESS = 0x1FFFFF
WOW64_CONTEXT_i386 = 0x00010000
WOW64_CONTEXT_CONTROL = WOW64_CONTEXT_i386 | 0x00000001
WOW64_CONTEXT_INTEGER = WOW64_CONTEXT_i386 | 0x00000002
WOW64_CONTEXT_FULL = WOW64_CONTEXT_CONTROL | WOW64_CONTEXT_INTEGER

LIKELY_NAMES = ("wizgold", "wiz7", "wizardry", "wizard")

TEXT_LO = 0x401000
TEXT_HI = 0x49C110


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


class WOW64_FLOATING_SAVE_AREA(ctypes.Structure):
    _fields_ = [
        ("ControlWord", wintypes.DWORD), ("StatusWord", wintypes.DWORD),
        ("TagWord", wintypes.DWORD), ("ErrorOffset", wintypes.DWORD),
        ("ErrorSelector", wintypes.DWORD), ("DataOffset", wintypes.DWORD),
        ("DataSelector", wintypes.DWORD), ("RegisterArea", ctypes.c_byte * 80),
        ("Cr0NpxState", wintypes.DWORD),
    ]


class WOW64_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("ContextFlags", wintypes.DWORD),
        ("Dr0", wintypes.DWORD), ("Dr1", wintypes.DWORD), ("Dr2", wintypes.DWORD),
        ("Dr3", wintypes.DWORD), ("Dr6", wintypes.DWORD), ("Dr7", wintypes.DWORD),
        ("FloatSave", WOW64_FLOATING_SAVE_AREA),
        ("SegGs", wintypes.DWORD), ("SegFs", wintypes.DWORD),
        ("SegEs", wintypes.DWORD), ("SegDs", wintypes.DWORD),
        ("Edi", wintypes.DWORD), ("Esi", wintypes.DWORD), ("Ebx", wintypes.DWORD),
        ("Edx", wintypes.DWORD), ("Ecx", wintypes.DWORD), ("Eax", wintypes.DWORD),
        ("Ebp", wintypes.DWORD), ("Eip", wintypes.DWORD), ("SegCs", wintypes.DWORD),
        ("EFlags", wintypes.DWORD), ("Esp", wintypes.DWORD), ("SegSs", wintypes.DWORD),
        ("ExtendedRegisters", ctypes.c_byte * 512),
    ]


PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

for fn, argt, rest in [
    ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
    ("OpenThread", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
    ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ("SuspendThread", [wintypes.HANDLE], wintypes.DWORD),
    ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
    ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
    ("Process32First", [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)], wintypes.BOOL),
    ("Process32Next", [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)], wintypes.BOOL),
    ("Thread32First", [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)], wintypes.BOOL),
    ("Thread32Next", [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)], wintypes.BOOL),
    ("Wow64GetThreadContext", [wintypes.HANDLE, ctypes.POINTER(WOW64_CONTEXT)], wintypes.BOOL),
    ("ReadProcessMemory", [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
]:
    f = getattr(k32, fn)
    f.argtypes = argt
    f.restype = rest


def list_processes():
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return []
    out = []
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    ok = k32.Process32First(snap, ctypes.byref(entry))
    while ok:
        out.append((entry.th32ProcessID, entry.szExeFile.decode("latin-1", "replace")))
        ok = k32.Process32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return out


def find_game_pid():
    procs = list_processes()
    matches = [(pid, n) for pid, n in procs if any(t in n.lower() for t in LIKELY_NAMES)]
    if len(matches) == 1:
        print(f"Found game process: {matches[0][1]} (PID {matches[0][0]})")
        return matches[0][0]
    pool = matches or procs
    if matches:
        print("Several candidates:")
    else:
        print("Couldn't auto-detect; all processes:")
    for i, (pid, n) in enumerate(pool):
        print(f"  [{i}] {n}  (PID {pid})")
    while True:
        c = input("Pick number for Wizardry Gold (or q): ").strip()
        if c.lower() == "q":
            return None
        if c.isdigit() and 0 <= int(c) < len(pool):
            return pool[int(c)][0]


def list_thread_ids(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return []
    out = []
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            out.append(te.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return out


def load_call_validator(exe_path):
    """Return is_after_call(va): True if the bytes just before va in .text are a
    CALL instruction, so va is a plausible return address."""
    data = open(exe_path, "rb").read()
    e = struct.unpack_from("<I", data, 0x3c)[0]
    nsec = struct.unpack_from("<H", data, e + 6)[0]
    opt = e + 24
    imgbase = struct.unpack_from("<I", data, opt + 28)[0]
    so = opt + struct.unpack_from("<H", data, e + 20)[0]
    text = None
    for i in range(nsec):
        o = so + i * 40
        name = data[o:o + 8].rstrip(b"\x00").decode()
        vs, va, rs, rp = struct.unpack_from("<IIII", data, o + 8)
        if name == ".text":
            text = (imgbase + va, rp, rs)
    base, rp, rs = text

    def v2o(va):
        off = va - base
        if 0 <= off < rs:
            return rp + off
        return None

    def is_after_call(va):
        # E8 rel32 call: 5 bytes
        o = v2o(va - 5)
        if o is not None and data[o] == 0xE8:
            return True
        # FF /2 call r/m: opcode FF, modrm reg field == 010, length 2..7
        for length in range(2, 8):
            o = v2o(va - length)
            if o is None:
                continue
            if data[o] == 0xFF and ((data[o + 1] >> 3) & 7) == 2:
                return True
        return False

    return is_after_call


def sample(pid, duration, is_after_call):
    """Sample call stacks for `duration` seconds. Returns (n_samples, Counter)
    where the Counter tallies, per sample, each distinct code-range frame seen
    (EIP + validated stack return addresses)."""
    hist = Counter()
    n = 0
    stack_words = 256  # 1 KB window above ESP
    buf = (ctypes.c_uint32 * stack_words)()
    got = ctypes.c_size_t(0)
    ctx = WOW64_CONTEXT()
    t_end = time.time() + duration
    tids = list_thread_ids(pid)
    while time.time() < t_end:
        for tid in tids:
            h = k32.OpenThread(THREAD_ALL_ACCESS, False, tid)
            if not h:
                continue
            try:
                if k32.SuspendThread(h) == 0xFFFFFFFF:
                    continue
                ctx.ContextFlags = WOW64_CONTEXT_FULL
                if not k32.Wow64GetThreadContext(h, ctypes.byref(ctx)):
                    continue
                frames = set()
                if TEXT_LO <= ctx.Eip < TEXT_HI:
                    frames.add(ctx.Eip)
                esp = ctx.Esp
                if esp:
                    ok = k32.ReadProcessMemory(
                        OPEN_PROC, ctypes.c_void_p(esp), buf,
                        stack_words * 4, ctypes.byref(got))
                    if ok:
                        cnt = got.value // 4
                        for i in range(cnt):
                            v = buf[i]
                            if TEXT_LO <= v < TEXT_HI and is_after_call(v):
                                frames.add(v)
                for fr in frames:
                    hist[fr] += 1
                n += 1
            finally:
                k32.ResumeThread(h)
                k32.CloseHandle(h)
        time.sleep(0.0008)
    return n, hist


OPEN_PROC = None  # set in main after opening the process for RPM


def main():
    global OPEN_PROC
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=r"D:/Wizardry 7 Gold/WIZARD_720.EXE.bak",
                    help="pristine exe for call-instruction validation")
    ap.add_argument("--secs", type=float, default=4.0, help="capture seconds per phase")
    args = ap.parse_args()

    try:
        is_after_call = load_call_validator(args.exe)
    except Exception as e:
        sys.exit(f"Could not read exe for validation ({e}). Pass --exe <path>.")

    pid = find_game_pid()
    if pid is None:
        return
    OPEN_PROC = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not OPEN_PROC:
        sys.exit(f"OpenProcess failed (err {ctypes.get_last_error()}). Run as Administrator.")

    print("\n=== Phase 1: IDLE baseline ===")
    print("In-game, stand still in camp/dungeon. Do NOT rest. Then press Enter.")
    input("  [Enter to capture idle] ")
    print(f"  capturing {args.secs}s ...")
    n0, idle = sample(pid, args.secs, is_after_call)
    print(f"  {n0} samples.")

    print("\n=== Phase 2: REST ===")
    print("Start RESTING in-game (make camp -> rest until healed / many days),")
    print("then IMMEDIATELY press Enter so we sample while it's resting.")
    input("  [Enter the instant rest begins] ")
    print(f"  capturing {args.secs}s ...")
    n1, rest = sample(pid, args.secs, is_after_call)
    print(f"  {n1} samples.")

    if not n0 or not n1:
        sys.exit("No samples captured (thread access failed?). Run as Administrator.")

    # Fraction of samples each frame appeared in, per phase.
    print("\n=== Candidate rest-loop frames (hot in REST, cold in IDLE) ===")
    print(f"{'VA':>10}  {'rest%':>6}  {'idle%':>6}  {'delta':>6}")
    scored = []
    for va, c in rest.items():
        rf = c / n1
        idf = idle.get(va, 0) / n0
        scored.append((rf - idf, va, rf, idf))
    scored.sort(reverse=True)
    for delta, va, rf, idf in scored[:25]:
        print(f"0x{va:08X}  {rf*100:5.0f}%  {idf*100:5.0f}%  {delta*100:+5.0f}")
    print("\nHand the top few VAs (high rest%, ~0 idle%) back to disassemble.")


if __name__ == "__main__":
    main()
