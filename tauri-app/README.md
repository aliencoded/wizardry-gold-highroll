# Wizardry Gold Patcher (Tauri)

A web-UI version of the patcher, built with Tauri v2 (Rust backend + a
vanilla HTML/CSS/JS frontend in `ui/`). Same patches as the CLI and the eframe
GUI: the character-creation **roll** patch plus the **game-speed** patches
(global speed, message/combat text dwell, search-wipe animation). See
`../docs/TIMING.md` and `../WRITEUP.md` for the reverse-engineering.

## Layout

- `ui/` — frontend (`index.html`, `styles.css`, `main.js`). No bundler; uses
  `window.__TAURI__.core.invoke` (config has `withGlobalTauri: true`).
- `src-tauri/` — Rust. `src/patch.rs` holds the pure byte-patch logic (mirrors
  `patch_wizgold.py` / `patch_speed.py`); `src/main.rs` exposes it as Tauri
  commands (`detect`, `status`, `apply_roll`, `revert_roll`, `apply_speed`,
  `revert_speed`). The frontend is embedded into the exe at build time.

## Build

Requires Rust and the WebView2 runtime (present by default on Windows 11).
No Node/npm and no Tauri CLI needed — the frontend is static, bundling is off
(`bundle.active: false`), so a plain cargo build produces the exe:

```
cd src-tauri
cargo build --release
```

Output: `src-tauri/target/release/wizgold-patcher-tauri.exe` (~5 MB standalone).

Run `cargo test` for the patch round-trip check (apply -> describe -> revert
must equal the pristine exe; asserts 27 text-dwell sites).

## Notes

- Patches every game exe in the folder (`WIZARD.EXE` + `WIZARD_720.EXE`) since
  the launcher renames between runs.
- Close the game before patching (the exe is file-locked while it runs).
- Backend logic is unit-tested; the WebView UI itself is not automated-tested.
