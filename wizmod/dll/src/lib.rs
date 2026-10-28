//! Wizardry Gold in-game mod (injected DLL).
//!
//! Milestone 1: prove injection. `DllMain` spawns a worker thread that writes a
//! debug log so we can confirm the DLL loaded into the game process. Hooks
//! (combat-text capture, overlay render, action presets) are added in later
//! milestones — see the plan and docs/COMBAT.md.

#![cfg(windows)]

mod capture;

use core::ffi::c_void;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;

const DLL_PROCESS_ATTACH: u32 = 1;

/// Append a line to the mod's debug log (next to the DLL, falling back to temp).
pub fn log(msg: &str) {
    let path = log_path();
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{msg}");
    }
}

fn log_path() -> PathBuf {
    // Prefer a fixed dev location for visibility; fall back to the temp dir.
    let dev = PathBuf::from(r"D:\development\wizgold\wizmod_debug.log");
    if dev.parent().map(|p| p.is_dir()).unwrap_or(false) {
        return dev;
    }
    std::env::temp_dir().join("wizmod_debug.log")
}

#[no_mangle]
pub extern "system" fn DllMain(_hinst: *mut c_void, reason: u32, _reserved: *mut c_void) -> i32 {
    if reason == DLL_PROCESS_ATTACH {
        // Do not do heavy work under the loader lock — hand off to a thread.
        std::thread::spawn(|| {
            let pid = std::process::id();
            log(&format!("[wizmod] attached to pid {pid}; worker thread up"));
            capture::install();
        });
    }
    1 // TRUE
}
