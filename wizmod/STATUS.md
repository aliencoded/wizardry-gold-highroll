# wizmod — status / session log

In-game mod for Wizardry Gold: (Track A) scrollable combat-log popup, (Track B)
one-click combat action presets. Architecture decided: **injected DLL**
(in-process), 32-bit (i686) to match the game. Build order: parallel. Presets:
both a blanket quick-action and named per-character sets.

## Built so far

- `wizmod/` cargo workspace (root `.cargo/config.toml` pins
  `target = i686-pc-windows-msvc`; the game is 32-bit).
- `wizmod/dll` -> **wizmod.dll** (cdylib). `DllMain` spawns a worker thread that
  writes a debug log and installs hooks. Logs to
  `D:\development\wizgold\wizmod_debug.log` (falls back to temp).
- `wizmod/injector` -> **wizmod-inject.exe**. Finds the running `wizard*`
  process and injects the DLL via `OpenProcess` + `VirtualAllocEx` +
  `WriteProcessMemory` + `CreateRemoteThread(LoadLibraryA)`. Both built 32-bit.
- Hooking lib: **minhook** (`minhook = "0.5"`). NOTE: `retour` 0.3 was rejected —
  it needs nightly (`win64` ABI / feature gates) and won't build on stable i686.

Artifacts:
`wizmod\target\i686-pc-windows-msvc\release\{wizmod.dll, wizmod-inject.exe}`.

## Verified

- **Milestone 1 (injection): WORKS.** Injected into the live game; log showed
  `[wizmod] attached to pid <N>; worker thread up`. Game ran normally.

## Where we paused (Track A capture — IN PROGRESS)

- First capture attempt hooked the combat printer **0x421B2E** (cdecl, 4 args;
  builds a line into globals **0x4B6260** [main] + **0x4B62B0** [secondary],
  draws via 0x41994A, wrapped by dwell calls 0x4175AA). Hook enabled fine but
  **0x421B2E never fired** during a normal attack/damage fight -> it's only one
  message category, not the path basic combat lines use.
- Current DLL is a **diagnostic build** that hooks BOTH:
  - universal formatter **0x4197BF** (cdecl 3 args: dest_buf*, idxA, idxB; it
    appends a looked-up string to dest) -> logs `[fmt] <text>` for every
    assembled string (capped at 3000 lines to avoid flooding).
  - 0x421B2E -> logs `[printer fire] sec=.. main=..` if it ever fires.
- Debug log was cleared, ready for a fresh capture run.

### NEXT STEP (resume here)

1. Launch game -> run `wizmod-inject.exe` -> enter a fight, let several
   attack/damage lines show.
2. Read `wizmod_debug.log`. Determine which hook carries combat text and what
   the strings look like.
3. Lock capture to the right hook + buffer; filter to combat (likely by game
   mode `[0x4A8052]`), keep a ring buffer (already scaffolded: `recent()` in
   `capture.rs`). Then build the popup (Track A render).

To rebuild the DLL you MUST fully close the game first (the loaded DLL locks the
file; "Access is denied (os error 5)" on build == game still running).

## Open tracks (see task list)

- Track A capture (0x4197BF/printer) — IN PROGRESS, at the diagnostic step above.
- Track A render — IAT-hook StretchDIBits `[0x6AE370]`, draw GDI panel over the
  frame on the game HDC `[0x4A8180]`; WndProc `0x47C59A` for toggle/scroll keys.
  NOT STARTED.
- Track B RE spike — locate per-character action array / target array / action
  enum / round-commit, around combat tick `0x4231C8` + menu input, using the
  existing debugger (`wizgold_highroll.py`) / `action_trace.py`. NOT STARTED.
- Track B presets + in-game UI — gated on the RE spike. NOT STARTED.

## Key addresses (this build, no ASLR, ImageBase 0x400000)

- Combat printer `0x421B2E`; text formatter `0x4197BF`; string-copy `0x4947C0`;
  draw-line `0x41994A`; text buffers `0x4B6260` (main) / `0x4B62B0` (secondary);
  glyph renderer ~`0x40F4xx` / `0x412A5F`.
- Injection/UI surface: WndProc `0x47C59A`, HWND `[0x4A8180]`, combat tick
  `0x4231C8`, mode selectors `[0x4A8050]`/`[0x4A8052]`, StretchDIBits IAT
  `[0x6AE370]`, GetAsyncKeyState (USER32 IAT).
- Text dwell global `[0x4A0FA8]` (we already split into main/quick tiers via
  patch_speed.py). See `docs/TIMING.md` for the speed/text work.
