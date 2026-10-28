# Wizardry Gold — timing / game-speed system (reverse-engineered)

Session 2026-06-27/28. All addresses are VAs for the 729088-byte
`WIZARD_720.EXE` / `WIZARD.EXE` build (ImageBase 0x400000, no ASLR, so stable
on every machine). The game is a **native Win32 PE** — NOT DOSBox. The GOG
`Launch.exe` is only a wrapper; `WIZARD*.EXE` runs natively.

## How the game paces itself

No `Sleep`. All waiting is busy-loop on `GetTickCount` (IAT `0x6ae46c`) /
`timeGetTime` (IAT `0x6ae634`). Found via import scan + xref.

### Universal step-timer (paces movement, animation, combat, rest)

`0x417851` — "has enough time passed?" rate-limiter. One caller (`0x4178CE`,
inside the case-machine `0x4178ac`, which has 7 callers across subsystems —
this is shared game-wide timing):

```
0x417851  func:
  call [GetTickCount]
  mov  [ebp-4], eax
  shr  [ebp-4], 7          ; <-- TIME UNIT = 128 ms  (0x417863, imm byte = 07)
  xor  eax,eax
  sub  eax,[0x4a0b5c]      ; lastStamp
  neg  eax
  sub  [ebp-4], eax        ; elapsed = (now>>7) - lastStamp
  movsx eax,[0x4a2af0]     ; threshold (word), units of 128 ms
  cmp  eax,[ebp-4]
  ja   0x41789f            ; (0x41787E) if threshold > elapsed -> return 0 (wait)
  call [GetTickCount]
  mov  [0x4a0b5c], eax
  sar  [0x4a0b5c], 7       ; <-- store now>>7  (0x41788F, imm byte = 07)
  mov  ax,1                ; return 1 (a step is due)
  ...
0x41789f: xor ax,ax; ret   ; return 0
```

- `[0x4a2af0]` (word) = "wait N units" threshold. **Scratch global**, written
  by ~37 sites as `66 c7 05 f0 2a 4a 00 <imm16>` (`mov word [0x4a2af0],N`);
  each subsystem sets its own N before polling. So it is NOT one speed
  constant — don't just patch one writer.
- `[0x4a0b5c]` (dword) = last tick stamp (also written by other timer code via
  `div 0x37`/`div 0x64` near 0x406FBD / 0x412E84).

### Global speed knob (clean multiplier)

The two `>>7` shifts in `0x417851` define the time unit (128 ms). Lowering the
shift makes `elapsed` grow faster per real ms → thresholds met sooner → whole
game faster, uniformly, with ALL per-step logic intact:

- shift 7 (`07`) = stock
- shift 6 (`06`) = ~2x speed
- shift 5 (`05`) = ~4x speed
- shift 4 (`04`) = ~8x speed

Patch BOTH imm bytes together so the now/last stamps stay in the same units:
- `0x417863`: `C1 6D FC 07` (`shr [ebp-4],7`) — byte at +3
- `0x41788F`: `C1 3D 5C0B4A00 07` (`sar [0x4a0b5c],7`) — last byte

**Status: IN-GAME VERIFIED (2026-08-03).** User applied the shift edit and
observed combat speed change in-game, confirming `0x417863`/`0x41788F` is the
live combat-pacing site. Risk noted earlier (anything that also writes
`[0x4a0b5c]` with a different scale — the div-0x37 timers — could interact):
combat scaling confirmed; movement/animation desync not separately stress-tested.

### THE UNIT-MISMATCH BUG (found + fixed 2026-08-11) -- `patch_timer.py`

**Symptom:** in the *pristine* exe, ambient overworld/dungeon life runs at
absurd speed -- birds/bats flying in and out of frame constantly, cricket
ambience retriggering every second or two. Combat step pacing erratic too.
Reported by the user; reproduced on the untouched 2013 `WIZARD_720.EXE`, so
this is a **vanilla Gold bug, not caused by any of our patches**.

**Cause.** The whole game shares ONE timestamp global `[0x4a0b5c]` and one
threshold `[0x4a2af0]`. ~30 inline throttle copies compute the stamp the DOS
way -- `GetTickCount / 0x37`, i.e. **55 ms**, which is the 18.2 Hz PC tick
(1000/18.2 = 54.9):

```
call [GetTickCount]; mov ecx,0x37; sub edx,edx; div ecx
```

Confirmed at `0x421C20`, `0x4231C8`, `0x42ABBA`, `0x434BD0`, `0x451855`,
`0x463CC1`, `0x47190E`, `0x472982`, plus the init stamps `0x406FC3` and
`0x412E8A`. (`0x412E8A` also has a `div 0x64` sibling right after -- a third
scale for a different global.)

Exactly ONE throttle -- `0x417851`, the universal step-timer documented above --
used a **shift instead of a divide**: `>>7` = **128 ms**. Someone converted
`/55` into `>>7` and silently changed the time base.

Because `0x417851` writes the same `[0x4a0b5c]` everyone reads, the shared
stamp is corrupt the moment the two families interleave -- and they do, from
the *same* dispatcher: `0x4178AC` sends case 1 -> `0x4231C8` (/55) and case 2
-> `0x417851` (>>7).

- /55 reader after a >>7 write: `now/55 - now/128` = huge positive elapsed
  -> threshold always met -> subsystem free-runs at CPU speed.
- >>7 reader after a /55 write: negative, and the compare uses an unsigned
  `ja` -> reads as huge -> also fires immediately.

Corrupt in both directions. The ambient animation sites (`0x421C20`,
`0x434BD0`, `0x451855`, `0x463CC1`) all toggle `[0x4a0f84]` and index the
sprite table at `0x4add50` -- that is the birds/bats frame flip.

**Fix.** Rewrite `0x417851`'s two timestamp computations to the game's own /55
idiom so every throttle shares one time base. Both edits fit **exactly** in the
original byte windows -- no code cave, no relocation, all branch targets
(`0x41789F`, `0x4178A7`) and the `movsx` at `0x417874` stay put:

- window A `0x41785A`..`0x417874` (26 B). Buys the 5 bytes it needs by folding
  the redundant `xor eax,eax / sub eax,[stamp] / neg eax / sub [ebp-4],eax`
  into `mov eax,[0x4a0b5c] / sub [ebp-4],eax`. Ends with 2 NOPs.
- window B `0x417884`..`0x417896` (18 B). Exact fit, no padding.

Core inserted in both: `xor edx,edx; push 0x37; pop ecx; div ecx`
(`33 D2 6A 37 59 F7 F1`). Clobbers eax/ecx/edx only -- same as the game's own
/55 sites, and the function already preserves ebx/esi/edi.

**Interaction with `--factor`:** the fix overwrites the two `shr/sar ,7`
immediates that `patch_speed.py --factor` tunes, so the two are mutually
exclusive. `patch_speed.py --status` detects the missing site and points at
`patch_timer.py`. No loss -- `--factor 2` (shift 6 = 64 ms) was only ever
*approximating* 55 and thereby masking the desync, not acting as a real speed
control. After the fix the `push <div>` imm8 is the equivalent knob
(`--tickdiv`), but lowering it re-desyncs this site; prefer the per-subsystem
knobs.

**Status: IN-GAME VERIFIED (2026-08-11).** User confirmed on the patched
`WIZARD.EXE`: ambient birds/bats and cricket sounds now at sane frequency,
combat text pacing correct, eyeball scan and book-open animations correct.
Round-trip `--fix` -> `--revert` is byte-identical to pristine.

### UI / message animation speed

`[0x4a0fa4]` (word, init `0x36`=54 at `0x408926`) scales UI delays: caller
`0x404710` computes `delay = [0x4a0fa4] * 20` ms then calls `Delay(ms)`. Gated
by flag `[0x4b4b8a]`. The in-game animation-speed setting.

### Message / combat TEXT dwell ("text too fast" -- the real fix)

`[0x4a0fa8]` (word, init `0x32`=50 ms at `0x40892F`) = how long a printed line
stays before auto-advancing. Combat/event/dialog text all show a line then call
the shared text-wait `0x4175AA` -> `0x421EDD` (`WaitKeyOrTimeout(ms)`: waits
`ms` OR until Enter `0x0D`). 50 ms is why text flies by in Gold; it's
recomputed at runtime from the (useless) in-game speed setting via
`[0x49ff04]*2` (`0x42A595`) / byte `[0x4b4b84]` (`0x48D8AF`), so bumping the
init alone won't stick.

Fix (in `patch_speed.py --textms MS`): the dwell is loaded in **two** forms,
both immediately followed by `push eax; call 0x4175AA`. Patch BOTH or text
speed comes out uneven (some lines slow, some fast):
- FULL, 27 sites: `mov ax,[0x4A0FA8]` = `66 A1 A8 0F 4A 00` (6 B)
  -> `mov ax,<MS>; nop; nop` = `66 B8 <imm16> 90 90`
- HALF, 19 sites: `movsx eax,[0x4A0FA8]; cdq; sub eax,edx; sar eax,1`
  = `0F BF 05 A8 0F 4A 00 99 2B C2 C1 F8 01` (13 B; pushes `[0x4A0FA8]/2`)
  -> `mov eax,<MS>; nop x8` = `B8 <imm32> 90*8`

46 sites total. This path uses `GetTickCount + ms` directly (NOT the `>>7`
throttle), so it is independent of the global `--factor`. Reversible (restore
both stock loads). ms <= 32767. (First pass patched only the 27 FULL sites,
which left the 19 HALF lines at ~25 ms -> visibly mixed combat-text speeds.)

The two forms are the game's deliberate **two-tier** dwell: MAIN lines wait the
full message-speed, QUICK lines wait half. The patcher exposes both:
`--textms` sets the MAIN tier (and mirrors to QUICK if `--texthalf` is omitted);
`--texthalf` sets the QUICK tier independently. So you can force them equal
(uniform) or keep a split (e.g. main 1000 / quick 500) to preserve the original
pacing feel. Both GUIs have separate "main" and "quick" inputs. Defaults tried:
uniform 1000, or split 1000/500.

### Eyeball / dungeon "search" sweep = generic screen-wipe

The slow on-screen sweep before you can press a button is `0x45C6FE` (a screen
wipe used by ~19 transitions, not search-specific). It draws ~50 cells via
GDI BitBlt, each frame busy-waiting `GetTickCount + 0x21` (33 ms) inside the
draw primitive `0x45CA4C` (delay at `0x45CB7D`; that `GetTickCount; add eax,0x21`
byte pattern is UNIQUE in the exe). `patch_speed.py --search MS` tunes that one
byte (0 = instant). Speeds all wipe transitions, not just search.

### Delay(ms) primitives (busy-wait, pump messages)

- `0x402448` — `Delay(word ms @ [ebp+8])`; loop polls GetTickCount, calls msg
  pump `0x4686a4`. Callers: `0x402414`, `0x404710`.
- `0x408F87` — twin Delay; loop calls `0x408eee`. Callers: `0x40AE8D`,
  `0x40B66D`, `0x40B751`.

## Mode dispatchers near the timer

- `0x4177ee` — state machine on `[0x4a8050]` (cases 1/2/3 → `0x4177a3` /
  `0x488bb8` / `0x434bd0`). 11 callers.
- `0x4178ac` — state machine on `[0x4a8052]` (case1 → `0x4231c8`, case2 →
  throttle `0x417851`). 7 callers.

## Rest loop (the earlier task, now deferred)

Located via runtime stack-sampling (`rest_trace.py`): rest-only frames cluster
in `0x417000–0x4179xx`. Rest steps are gated by the same throttle `0x417851`.
To make rest fast while keeping encounter rolls / aging / healing per step:
force the throttle to always pass — NOP the `ja 0x41789f` at `0x41787E`
(`0F 87 1B 00 00 00` -> `90*6`). Deferred per user (rest speed is acceptable).

## Tooling added this session

- `rest_trace.py` — read-only runtime call-stack sampler. Suspends each game
  thread, reads EIP + validated stack return addresses (dword in `.text`
  `0x401000–0x49C110` immediately preceded by a CALL), histograms frames over an
  idle phase and a rest phase, prints frames hot in one but not the other.
  Found the rest/timing region. Run as Administrator.
- scratchpad helpers: `wdis.py` (general VA-range disasm), `xcall.py` (find
  E8 rel32 callers of a target VA).

## Diplomacy bug (separate task, not started)

Web-confirmed: Diplomacy skill is "deactivated/broken" only in Gold (works in
DOS). In DOS, a successful Diplomacy check during NPC/monster parley
(TRUCE/PEACE) raises the target's friendly-attitude toward a truce; in Gold that
effect never fires. No public byte-level fix exists. Not yet located in the exe
(all UI/skill text is in external `.DBS`/`.HDR`, so no string anchor).
"Deactivated" smells like a forced-off branch (same shape as the elite-roll
flag) — plausibly findable and novel.
