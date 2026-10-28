#!/usr/bin/env python3
"""
Wizardry Gold - generic action-loop locator (read-only runtime trace)
=====================================================================

Same read-only call-stack sampling as rest_trace.py, but for ANY action you
name. Captures an IDLE baseline, then captures while you repeatedly perform the
action, and prints the code frames that are hot during the action but cold when
idle -- i.e. the code that paces that action.

Use it to find a specific timer to tune, e.g.:
    py action_trace.py --label "eyeball / search animation"
    py action_trace.py --label "combat message scroll"

Run as Administrator (it must open the game's threads). 64-bit Python is fine.
"""

import argparse
import ctypes
import sys

import rest_trace as rt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="the action", help="what you'll perform in phase 2")
    ap.add_argument("--exe", default=r"D:/Wizardry 7 Gold/WIZARD_720.EXE.bak",
                    help="pristine exe for call-instruction validation")
    ap.add_argument("--secs", type=float, default=4.0, help="capture seconds per phase")
    args = ap.parse_args()

    try:
        is_after_call = rt.load_call_validator(args.exe)
    except Exception as e:
        sys.exit(f"Could not read exe for validation ({e}). Pass --exe <path>.")

    pid = rt.find_game_pid()
    if pid is None:
        return
    rt.OPEN_PROC = rt.k32.OpenProcess(
        rt.PROCESS_VM_READ | rt.PROCESS_QUERY_INFORMATION, False, pid)
    if not rt.OPEN_PROC:
        sys.exit(f"OpenProcess failed (err {ctypes.get_last_error()}). Run as Administrator.")

    print("\n=== Phase 1: IDLE baseline ===")
    print("Stand still, do NOTHING. Then press Enter.")
    input("  [Enter to capture idle] ")
    print(f"  capturing {args.secs}s ...")
    n0, idle = rt.sample(pid, args.secs, is_after_call)
    print(f"  {n0} samples.")

    print(f"\n=== Phase 2: {args.label} ===")
    print(f"Begin '{args.label}' and KEEP doing it (repeat if short), then press")
    print("Enter so we sample WHILE it happens. Keep performing it for ~4s.")
    input(f"  [Enter the instant '{args.label}' is happening] ")
    print(f"  capturing {args.secs}s ...")
    n1, act = rt.sample(pid, args.secs, is_after_call)
    print(f"  {n1} samples.")

    if not n0 or not n1:
        sys.exit("No samples captured. Run as Administrator.")

    print(f"\n=== Frames hot during '{args.label}', cold when idle ===")
    print(f"{'VA':>10}  {'act%':>6}  {'idle%':>6}  {'delta':>6}")
    scored = []
    for va, c in act.items():
        af = c / n1
        idf = idle.get(va, 0) / n0
        scored.append((af - idf, va, af, idf))
    scored.sort(reverse=True)
    for delta, va, af, idf in scored[:25]:
        print(f"0x{va:08X}  {af*100:5.0f}%  {idf*100:5.0f}%  {delta*100:+5.0f}")
    print("\nHand me the top few VAs (high act%, ~0 idle%).")


if __name__ == "__main__":
    main()
