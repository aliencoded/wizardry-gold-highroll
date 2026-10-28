#!/usr/bin/env python3
"""
Wizardry Gold - High Roll Helper
================================

A tiny, single-purpose game trainer for the *single-player* Windows version of
Wizardry Gold (Wizardry 7: Crusaders of the Dark Savant). It finds the
"bonus points" value on the character-creation screen and lets you set it to
whatever you want, so you never have to reroll for a high roll again.

Why this exists instead of Cheat Engine:
  Cheat Engine is a general scanner, so you repeat a multi-step hunt every
  session. This tool only ever looks for one value, so it walks you through a
  two-question scan, then remembers the result for next time.

HOW IT WORKS (no prior knowledge needed):
  1. Start Wizardry Gold and get to the screen that shows your bonus points.
  2. Run this tool (as Administrator -- see notes at the bottom).
  3. It asks "what number is on screen right now?" -> you type it.
  4. It asks you to reroll and type the new number. Repeat 1-2 times.
  5. Once it's down to a single address, you type your desired bonus and it's set.

This only reads/writes memory of a process YOU own and are running locally.
It does not modify the game's files, touch the network, or persist anything
beyond a small config file (the address it found) next to this script.

Windows only. WizGold is 32-bit; 64-bit Python works fine against it.
"""

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

if sys.platform != "win32":
    sys.exit("This tool only runs on Windows (that's where Wizardry Gold lives).")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Process access rights
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
ACCESS = (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
          PROCESS_VM_WRITE | PROCESS_VM_OPERATION)

# Memory region flags
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
WRITABLE_PROTECTIONS = {0x04, 0x08, 0x40, 0x80}  # RW, WC, EXEC_RW, EXEC_WC

# Toolhelp snapshot
TH32CS_SNAPPROCESS = 0x00000002

# Cap the address walk at 4 GB: WizGold is a 32-bit process.
MAX_ADDRESS = 0x100000000
# Skip pathologically large regions so we never allocate something huge.
MAX_REGION = 256 * 1024 * 1024

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "wizgold_highroll.cfg.json")
DEFAULT_TARGET = 25  # a comfortable "monster roll" that stats can still absorb

# The bonus-points value lives at a static address in WizGold's main module, so
# it's the same every launch. Hardcode it to skip the scan. (Re-confirm with a
# scan only if the game build changes and this stops reading sane values.)
KNOWN_ADDRESS = 0x4A209C
KNOWN_WIDTH = 4


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    # ctypes auto-inserts the alignment padding that this struct needs on
    # 64-bit, because RegionSize (c_size_t) forces 8-byte alignment there.
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE

k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL

k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                               ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t

k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wintypes.BOOL

k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wintypes.BOOL

k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
k32.Process32First.restype = wintypes.BOOL

k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
k32.Process32Next.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# Debugger plumbing (used to FORCE the bonus roll high)
# ---------------------------------------------------------------------------
# The game writes the rolled bonus, then in the same handler reads it to decide
# which professions you can pick. No outside write can land in that tiny gap.
# A debugger can: a hardware breakpoint on WRITES to the bonus address freezes
# the game the instant after it writes the roll; we overwrite to a high value,
# then resume, so the eligibility check reads the high roll. WizGold is 32-bit,
# so from 64-bit Python we drive the WoW64 thread context.

TH32CS_SNAPTHREAD = 0x00000004

EXCEPTION_DEBUG_EVENT = 1
CREATE_THREAD_DEBUG_EVENT = 2
CREATE_PROCESS_DEBUG_EVENT = 3
EXIT_PROCESS_DEBUG_EVENT = 5

DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001
EXCEPTION_SINGLE_STEP = 0x80000004
EXCEPTION_BREAKPOINT = 0x80000003
ERROR_SEM_TIMEOUT = 121

THREAD_ALL_ACCESS = 0x1FFFFF
WOW64_CONTEXT_i386 = 0x00010000
WOW64_CONTEXT_DEBUG_REGISTERS = WOW64_CONTEXT_i386 | 0x00000010

# DR7: enable DR0 (local), break on 4-byte WRITE.
#   bit0 L0=1 | bits16-17 RW0=01 (write) | bits18-19 LEN0=11 (4 bytes)
DR7_WRITE_4 = 0x1 | (0x1 << 16) | (0x3 << 18)


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


class WOW64_FLOATING_SAVE_AREA(ctypes.Structure):
    _fields_ = [
        ("ControlWord", wintypes.DWORD),
        ("StatusWord", wintypes.DWORD),
        ("TagWord", wintypes.DWORD),
        ("ErrorOffset", wintypes.DWORD),
        ("ErrorSelector", wintypes.DWORD),
        ("DataOffset", wintypes.DWORD),
        ("DataSelector", wintypes.DWORD),
        ("RegisterArea", ctypes.c_byte * 80),
        ("Cr0NpxState", wintypes.DWORD),
    ]


class WOW64_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("ContextFlags", wintypes.DWORD),
        ("Dr0", wintypes.DWORD),
        ("Dr1", wintypes.DWORD),
        ("Dr2", wintypes.DWORD),
        ("Dr3", wintypes.DWORD),
        ("Dr6", wintypes.DWORD),
        ("Dr7", wintypes.DWORD),
        ("FloatSave", WOW64_FLOATING_SAVE_AREA),
        ("SegGs", wintypes.DWORD),
        ("SegFs", wintypes.DWORD),
        ("SegEs", wintypes.DWORD),
        ("SegDs", wintypes.DWORD),
        ("Edi", wintypes.DWORD),
        ("Esi", wintypes.DWORD),
        ("Ebx", wintypes.DWORD),
        ("Edx", wintypes.DWORD),
        ("Ecx", wintypes.DWORD),
        ("Eax", wintypes.DWORD),
        ("Ebp", wintypes.DWORD),
        ("Eip", wintypes.DWORD),
        ("SegCs", wintypes.DWORD),
        ("EFlags", wintypes.DWORD),
        ("Esp", wintypes.DWORD),
        ("SegSs", wintypes.DWORD),
        ("ExtendedRegisters", ctypes.c_byte * 512),
    ]


class EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [
        ("ExceptionCode", wintypes.DWORD),
        ("ExceptionFlags", wintypes.DWORD),
        ("ExceptionRecord", ctypes.c_void_p),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", wintypes.DWORD),
        ("ExceptionInformation", ctypes.c_ulonglong * 15),
    ]


class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [
        ("ExceptionRecord", EXCEPTION_RECORD),
        ("dwFirstChance", wintypes.DWORD),
    ]


class _DEBUG_UNION(ctypes.Union):
    _fields_ = [
        ("Exception", EXCEPTION_DEBUG_INFO),
        ("raw", ctypes.c_byte * 192),
    ]


class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [
        ("dwDebugEventCode", wintypes.DWORD),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
        ("u", _DEBUG_UNION),
    ]


k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenThread.restype = wintypes.HANDLE

k32.SuspendThread.argtypes = [wintypes.HANDLE]
k32.SuspendThread.restype = wintypes.DWORD

k32.ResumeThread.argtypes = [wintypes.HANDLE]
k32.ResumeThread.restype = wintypes.DWORD

k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32First.restype = wintypes.BOOL

k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32Next.restype = wintypes.BOOL

k32.Wow64GetThreadContext.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(WOW64_CONTEXT)]
k32.Wow64GetThreadContext.restype = wintypes.BOOL

k32.Wow64SetThreadContext.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(WOW64_CONTEXT)]
k32.Wow64SetThreadContext.restype = wintypes.BOOL

k32.DebugActiveProcess.argtypes = [wintypes.DWORD]
k32.DebugActiveProcess.restype = wintypes.BOOL

k32.DebugActiveProcessStop.argtypes = [wintypes.DWORD]
k32.DebugActiveProcessStop.restype = wintypes.BOOL

k32.DebugSetProcessKillOnExit.argtypes = [wintypes.BOOL]
k32.DebugSetProcessKillOnExit.restype = wintypes.BOOL

k32.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT), wintypes.DWORD]
k32.WaitForDebugEvent.restype = wintypes.BOOL

k32.ContinueDebugEvent.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD]
k32.ContinueDebugEvent.restype = wintypes.BOOL


def list_thread_ids(pid):
    """Thread IDs belonging to `pid`."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return []
    out = []
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    try:
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                out.append(te.th32ThreadID)
            ok = k32.Thread32Next(snap, ctypes.byref(te))
    finally:
        k32.CloseHandle(snap)
    return out


def _set_hw_write_bp(tid, addr, enable=True):
    """Arm (or clear) a 4-byte hardware WRITE breakpoint on `addr` for a thread.

    The thread is suspended around the context edit so the write actually sticks.
    """
    h = k32.OpenThread(THREAD_ALL_ACCESS, False, tid)
    if not h:
        return False
    ok = False
    try:
        k32.SuspendThread(h)
        ctx = WOW64_CONTEXT()
        ctx.ContextFlags = WOW64_CONTEXT_DEBUG_REGISTERS
        if k32.Wow64GetThreadContext(h, ctypes.byref(ctx)):
            ctx.Dr0 = addr & 0xFFFFFFFF if enable else 0
            ctx.Dr7 = DR7_WRITE_4 if enable else 0
            ctx.Dr6 = 0
            ctx.ContextFlags = WOW64_CONTEXT_DEBUG_REGISTERS
            ok = bool(k32.Wow64SetThreadContext(h, ctypes.byref(ctx)))
    finally:
        k32.ResumeThread(h)
        k32.CloseHandle(h)
    return ok


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

# Common executable names for the Gold / Win release. We match loosely.
LIKELY_NAMES = ("wizgold", "wiz7", "wizardry", "wizard")


def list_processes():
    """Return [(pid, exe_name_lower), ...] for all running processes."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return []
    out = []
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("latin-1", "replace")
            out.append((entry.th32ProcessID, name))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def find_game_pid():
    procs = list_processes()
    matches = [(pid, name) for pid, name in procs
               if any(tag in name.lower() for tag in LIKELY_NAMES)]
    if len(matches) == 1:
        pid, name = matches[0]
        print(f"Found likely game process: {name} (PID {pid})")
        return pid
    if matches:
        print("Several candidate processes found:")
    else:
        print("Couldn't auto-detect the game. Here are all running processes.")
        matches = procs
    for i, (pid, name) in enumerate(matches):
        print(f"  [{i}] {name}  (PID {pid})")
    while True:
        choice = input("Pick the number for Wizardry Gold (or 'q'): ").strip()
        if choice.lower() == "q":
            return None
        if choice.isdigit() and 0 <= int(choice) < len(matches):
            return matches[int(choice)][0]
        print("  Not a valid choice.")


# ---------------------------------------------------------------------------
# Memory scanning
# ---------------------------------------------------------------------------

class Mem:
    def __init__(self, pid):
        self.pid = pid
        self.handle = k32.OpenProcess(ACCESS, False, pid)
        if not self.handle:
            err = ctypes.get_last_error()
            raise OSError(f"OpenProcess failed (error {err}). "
                          f"Try running this tool as Administrator.")

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def regions(self):
        """Yield (base, size) for committed, writable, non-guarded regions."""
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while addr < MAX_ADDRESS:
            ret = k32.VirtualQueryEx(self.handle, ctypes.c_void_p(addr),
                                     ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size == 0:
                break
            usable = (mbi.State == MEM_COMMIT
                      and (mbi.Protect & 0xFF) in WRITABLE_PROTECTIONS
                      and not (mbi.Protect & PAGE_GUARD))
            if usable and size <= MAX_REGION:
                yield base, size
            addr = base + size

    def read(self, address, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(self.handle, ctypes.c_void_p(address),
                                   buf, size, ctypes.byref(got))
        if not ok:
            return b""
        return buf.raw[:got.value]

    def write_int(self, address, value, width):
        data = int(value).to_bytes(width, "little", signed=True)
        wrote = ctypes.c_size_t(0)
        ok = k32.WriteProcessMemory(self.handle, ctypes.c_void_p(address),
                                    data, len(data), ctypes.byref(wrote))
        return bool(ok) and wrote.value == len(data)

    def read_int(self, address, width):
        raw = self.read(address, width)
        if len(raw) != width:
            return None
        return int.from_bytes(raw, "little", signed=True)

    def scan(self, value, width):
        """Return a set of addresses whose `width`-byte int equals value."""
        needle = int(value).to_bytes(width, "little", signed=True)
        found = set()
        for base, size in self.regions():
            data = self.read(base, size)
            if not data:
                continue
            start = 0
            while True:
                idx = data.find(needle, start)
                if idx == -1:
                    break
                found.add(base + idx)
                start = idx + 1
        return found


# ---------------------------------------------------------------------------
# Guided flow
# ---------------------------------------------------------------------------

def ask_int(prompt):
    while True:
        s = input(prompt).strip()
        if s.lstrip("-").isdigit():
            return int(s)
        print("  Please type a whole number.")


def guided_scan(mem):
    """Walk the user through narrowing to a single address. Returns (addr, width)."""
    print("\n--- Guided scan ---")
    print("Get to the character-creation screen that shows your BONUS POINTS.")
    first = ask_int("What bonus-points number is shown right now? ")

    width = 4
    print("Scanning memory... (a few seconds)")
    candidates = mem.scan(first, width)
    if not candidates:
        # The value may be stored narrower than 4 bytes; try 2 then 1.
        for w in (2, 1):
            candidates = mem.scan(first, w)
            if candidates:
                width = w
                break
    if not candidates:
        print("No matches found. Is the number on screen exactly what you typed?")
        return None
    print(f"  {len(candidates)} candidate(s) so far (reading values as "
          f"{width}-byte integers).")

    while len(candidates) > 1:
        print("\nReroll your character so the bonus number changes.")
        nxt = ask_int("New bonus-points number now shown: ")
        candidates &= mem.scan(nxt, width)
        print(f"  Narrowed to {len(candidates)} candidate(s).")
        if not candidates:
            print("Lost the value (it may have moved). Restarting the scan.")
            return guided_scan(mem)

    addr = next(iter(candidates))
    print(f"\nLocked onto a single address: 0x{addr:X}")
    return addr, width


def scan_tracking(mem, rounds=3):
    """Find ALL addresses that track the roll across several rerolls.

    Unlike guided_scan (which collapses to one address), this keeps the whole
    stable set. The displayed/spendable bonus is one of them; the value the game
    actually reads for profession eligibility may be a *different* copy in that
    set. Returns (sorted_addr_list, width) or None.
    """
    print("\n--- Gate hunt scan ---")
    print("Get to the screen that shows the BONUS number, then answer below.")
    first = ask_int("Bonus number shown right now? ")
    width = 4
    print("Scanning...")
    cands = mem.scan(first, width)
    if not cands:
        for w in (2, 1):
            cands = mem.scan(first, w)
            if cands:
                width = w
                break
    if not cands:
        print("No matches. Was the number exactly right?")
        return None
    print(f"  {len(cands)} candidate(s) (width {width}).")
    r = 1
    while r < rounds and len(cands) > 1:
        print("\nReroll so the bonus changes.")
        nxt = ask_int("New bonus number now shown: ")
        cands &= mem.scan(nxt, width)
        print(f"  {len(cands)} still tracking the roll.")
        if not cands:
            print("Lost them all -- restarting.")
            return scan_tracking(mem, rounds)
        r += 1
    addrs = sorted(cands)
    print(f"\nTracking {len(addrs)} address(es): "
          + ", ".join(f"0x{a:X}" for a in addrs))
    return addrs, width


class RollForcer:
    """Debugger that forces the bonus roll high the instant the game rolls it.

    Run as a background thread. It attaches to the game, sets a hardware WRITE
    breakpoint on the bonus address on every thread, and on each write trap (the
    game just wrote a fresh roll) overwrites the value to `target` before the
    game's eligibility check reads it. Detaches cleanly on stop.
    """

    def __init__(self, mem, addrs, width):
        self.mem = mem
        self.addrs = addrs
        self.width = width
        self.target = [None]      # None => don't overwrite
        self._stop = threading.Event()
        self._thread = None
        self.attached = False
        self.error = None

    def start(self, target):
        self.target[0] = target
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _arm_all(self):
        for tid in list_thread_ids(self.mem.pid):
            _set_hw_write_bp(tid, self.addrs[0], enable=True)

    def _run(self):
        pid = self.mem.pid
        if not k32.DebugActiveProcess(pid):
            self.error = (f"DebugActiveProcess failed (error "
                          f"{ctypes.get_last_error()}). Close other debuggers, "
                          f"run as Administrator.")
            return
        # If our tool dies, do NOT take the game down with it.
        k32.DebugSetProcessKillOnExit(False)
        self.attached = True
        self._arm_all()
        de = DEBUG_EVENT()
        try:
            while not self._stop.is_set():
                if not k32.WaitForDebugEvent(ctypes.byref(de), 200):
                    if ctypes.get_last_error() == ERROR_SEM_TIMEOUT:
                        continue
                    break
                code = de.dwDebugEventCode
                status = DBG_CONTINUE
                if code in (CREATE_PROCESS_DEBUG_EVENT, CREATE_THREAD_DEBUG_EVENT):
                    _set_hw_write_bp(de.dwThreadId, self.addrs[0], enable=True)
                elif code == EXCEPTION_DEBUG_EVENT:
                    ecode = de.u.Exception.ExceptionRecord.ExceptionCode & 0xFFFFFFFF
                    if ecode == EXCEPTION_SINGLE_STEP:
                        # Data write breakpoint hit: the roll was just written.
                        t = self.target[0]
                        if t is not None:
                            for a in self.addrs:
                                self.mem.write_int(a, t, self.width)
                    elif ecode == EXCEPTION_BREAKPOINT:
                        pass
                    else:
                        status = DBG_EXCEPTION_NOT_HANDLED
                elif code == EXIT_PROCESS_DEBUG_EVENT:
                    break
                k32.ContinueDebugEvent(de.dwProcessId, de.dwThreadId, status)
        finally:
            k32.DebugActiveProcessStop(pid)
            self.attached = False


def control_loop(mem, addrs, width):
    """Live control over one or more bonus-tracking addresses.

    `addrs` is a list. Writes and the background pin apply to ALL of them, so if
    the real eligibility gate is a second copy we don't have to know which one.

    Background pin races the game's roll (write happens the instant you pick a
    gender). Releasable so you can drop to 0 and finish creation.
    """
    if isinstance(addrs, int):
        addrs = [addrs]
    pin_target = [None]          # None => pin off
    stop = threading.Event()

    def write_all(value):
        ok = True
        for a in addrs:
            ok = mem.write_int(a, value, width) and ok
        return ok

    def pinner():
        while not stop.is_set():
            t = pin_target[0]
            if t is not None:
                for a in addrs:
                    if mem.read_int(a, width) != t:
                        mem.write_int(a, t, width)
            time.sleep(0.002)    # tight enough to overwrite the game's roll

    th = threading.Thread(target=pinner, daemon=True)
    th.start()
    forcer = RollForcer(mem, addrs, width)

    print("\n--- Live control ---")
    print(f"Controlling {len(addrs)} address(es): "
          + ", ".join(f"0x{a:X}" for a in addrs))
    print("FORCE THE ROLL (unlock Faery Ninja):")
    print("  1) Get to Faery, BEFORE the gender click. Type:  force 30")
    print("     (attaches a debugger; every roll is forced to 30)")
    print("  2) Click gender in-game -> bonus rolls 30 -> Faery Ninja is selectable. Pick it.")
    print("  3) Type:  stop   (detach). Then spend points; type 0 to finish creation.")
    print("Commands:")
    print("  force [n]  force every roll to n via debugger (default 30)")
    print("  stop       detach the debugger")
    print("  <number> write n now   p [n] pin all   u unpin")
    print("  f <n>    fresh-scan: list every address currently == n (diagnostic)")
    print("  r re-read   q quit\n")
    try:
        while True:
            vals = ", ".join(str(mem.read_int(a, width)) for a in addrs)
            flags = []
            if pin_target[0] is not None:
                flags.append(f"pin={pin_target[0]}")
            if forcer.attached:
                flags.append(f"FORCING={forcer.target[0]}")
            tag = " ".join(flags) if flags else "idle"
            s = input(f"[{vals} | {tag}] > ").strip().lower()
            if s == "q":
                break
            if s == "r":
                continue
            if s == "stop":
                forcer.stop()
                print("  debugger detached.")
                continue
            if s == "force" or s.startswith("force "):
                arg = s[5:].strip()
                target = int(arg) if arg.lstrip("-").isdigit() else 30
                forcer.start(target)
                time.sleep(0.3)
                if forcer.error:
                    print(f"  {forcer.error}")
                elif forcer.attached:
                    print(f"  forcing every roll to {target}. Now click gender in-game.")
                else:
                    print("  attaching...")
                continue
            if s == "u":
                pin_target[0] = None
                print("  unpinned.")
                continue
            if s == "p" or s.startswith("p "):
                arg = s[1:].strip()
                if arg.lstrip("-").isdigit():
                    pin_target[0] = int(arg)
                else:
                    v = mem.read_int(addrs[0], width)
                    pin_target[0] = v if v is not None else 0
                print(f"  pinning all at {pin_target[0]}.")
                continue
            if s.startswith("f "):
                arg = s[2:].strip()
                if not arg.lstrip("-").isdigit():
                    print("  use: f <number>")
                    continue
                hits = sorted(mem.scan(int(arg), width))
                print(f"  {len(hits)} address(es) == {arg}"
                      + (": " + ", ".join(f"0x{a:X}" for a in hits[:20])
                         + (" ..." if len(hits) > 20 else "") if hits else ""))
                continue
            if s.lstrip("-").isdigit():
                target = int(s)
                if pin_target[0] is not None:
                    pin_target[0] = target
                if write_all(target):
                    print(f"  wrote {target} to all.")
                else:
                    print("  a write failed -- run as Administrator.")
                continue
            print("  use: force [n] | stop | <number> | p [n] | u | f <n> | r | q")
    finally:
        stop.set()
        th.join(timeout=1)
        forcer.stop()


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            d = json.load(f)
            return int(d["address"]), int(d["width"])
    except Exception:
        return None


def save_config(addr, width):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump({"address": addr, "width": width}, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print(" Wizardry Gold - High Roll Helper")
    print("=" * 60)

    pid = find_game_pid()
    if pid is None:
        print("No process selected. Start Wizardry Gold first, then rerun.")
        return

    try:
        mem = Mem(pid)
    except OSError as e:
        print(e)
        return

    try:
        addrs = width = None

        # The bonus lives at a static address, so no scan is needed by default.
        # (Scan only if a game update ever moves it.)
        v = mem.read_int(KNOWN_ADDRESS, KNOWN_WIDTH)
        if v is None:
            print("Couldn't read the known address; falling back to a scan.")
            result = scan_tracking(mem)
            if not result:
                return
            addrs, width = result
        else:
            print(f"\nUsing known address 0x{KNOWN_ADDRESS:X} (reads {v}).")
            if input("Rescan instead? (y/N) ").strip().lower() == "y":
                result = scan_tracking(mem)
                if not result:
                    return
                addrs, width = result
            else:
                addrs, width = [KNOWN_ADDRESS], KNOWN_WIDTH

        control_loop(mem, addrs, width)
    finally:
        mem.close()


if __name__ == "__main__":
    main()
