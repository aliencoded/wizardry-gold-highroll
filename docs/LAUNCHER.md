# GOG `Launch.exe` — what it actually runs

Reverse-engineered 2026-08-11. Answers "which exe do my patches need to be on?"

`Launch.exe` (2862904 B, 2014-08-27) is a **UPX-packed** wxWidgets launcher, not
part of the game. Unpack for analysis:

```
winget install --id=UPX.UPX -e
upx -d -o Launch_unpacked.exe Launch.exe     # 2.8 MB -> 7.1 MB
```

Packed, it has no useful strings. Unpacked, everything is in `.rdata` around
`0x8DA860`.

## Resolution -> exe name mapping

The launcher runs `PATH` + `\` + `GAMEEXENAME`, both read from its config. Five
setter functions each store an exe name at object offset `+0x18` and a tag at
`+0x20`:

| launcher option (UI label)               | setter VA  | exe name         | tag     |
|------------------------------------------|-----------|------------------|---------|
| Desktop resolution (Normal game window)  | `0x401650` | `WIZARD.EXE`     | DESKTOP |
| 1024x768 (Medium game window)            | `0x4015F0` | `WIZARD_768.EXE` | HIGH    |
| 800x600                                  | `0x4015D0` | `WIZARD_600.EXE` | LOW     |
| 1280x720 (Large game window)             | `0x401610` | `WIZARD_720.EXE` | LOW     |
| 1600x900                                 | `0x401630` | `WIZARD_900.EXE` | HIGH    |

Startup logic at `0x402180`: read saved `RESOLUTION` from config, query the
display height, `cmp ax, 0x2d0` (720) — if the desktop is <= 720 tall, override
to `WIZARD.EXE` / DESKTOP (`0x4022AD`).

Config keys (`RESOLUTION`, `PATH`, `GAMEEXENAME`) live in
`HKCU\Software\Local AppWizard-Generated Applications\Launch Tool\Settings`.
On this machine that key exists but is **empty** — no saved selection.

## Consequences for patching

- **Only 2 of the 5 exes exist on disk** (`WIZARD.EXE`, `WIZARD_720.EXE`).
  Picking 800x600 / 1024x768 / 1600x900 points at a file that isn't there.
- **CONFIRMED IN PRACTICE (2026-08-11):** selecting "1280x720 large game window"
  launches `WIZARD_720.EXE`. Verified by catching the live process
  (`Get-Process`, PID 39912, name `WIZARD_720`) while the user played. This is
  why an earlier session's patches "disappeared" — they were on `WIZARD.EXE`
  while the launcher was running `WIZARD_720.EXE`.
- **The two exes are byte-identical apart from our patches.** After applying the
  same four patches to both, they hash the same. So resolution is NOT baked into
  the executable; the launcher distinguishes purely by filename. Whatever picks
  the resolution must be doing it another way (window size at runtime).
- **Therefore: always patch BOTH `WIZARD.EXE` and `WIZARD_720.EXE`.**

## Unresolved

The earlier-noted "launcher renames the exe between runs" was not reproduced or
located. The only `MoveFileW` call site in the unpacked binary is `0x8608D3`,
which is wx's generic `wxRenameFile` — it was not traced back to launcher logic,
so the rename story is unconfirmed. `CopyFileW` (`0x8607F5`) likewise.
