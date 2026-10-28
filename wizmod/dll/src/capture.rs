//! Track A capture (diagnostic build).
//!
//! First attempt hooked the combat printer 0x421B2E, but it never fired for
//! basic attack/damage lines — it's only one message category. So we now hook
//! the UNIVERSAL text formatter 0x4197BF (every text string is assembled
//! through it: dest buffer ptr = arg0, two table indices = arg1/arg2). After
//! the original runs, arg0 holds the (accumulated) ASCII text; we log it. We
//! also keep an always-on entry log on 0x421B2E to confirm whether it fires.
//! Once we see which path carries combat lines, we lock capture to it.

use core::ffi::c_void;
use std::collections::VecDeque;
use std::mem::transmute;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};

use minhook::MinHook;

use crate::log;

const COMBAT_PRINTER: usize = 0x0042_1B2E; // specific message printer (4 args)
const FORMATTER: usize = 0x0041_97BF; // universal text formatter (3 args)
const MAX_LINES: usize = 500;
const FMT_LOG_CAP: usize = 3000; // stop flooding the log after this many

type Print4 = unsafe extern "cdecl" fn(u32, u32, u32, u32) -> u32;
type Fmt3 = unsafe extern "cdecl" fn(u32, u32, u32) -> u32;

static ORIG_PRINT: OnceLock<usize> = OnceLock::new();
static ORIG_FMT: OnceLock<usize> = OnceLock::new();
static FMT_LOGGED: AtomicUsize = AtomicUsize::new(0);
static LINES: OnceLock<Mutex<VecDeque<String>>> = OnceLock::new();

fn store() -> &'static Mutex<VecDeque<String>> {
    LINES.get_or_init(|| Mutex::new(VecDeque::with_capacity(MAX_LINES)))
}

#[allow(dead_code)]
pub fn recent() -> Vec<String> {
    store().lock().map(|g| g.iter().cloned().collect()).unwrap_or_default()
}

fn push(line: String) {
    if let Ok(mut g) = store().lock() {
        if g.len() >= MAX_LINES {
            g.pop_front();
        }
        g.push_back(line);
    }
}

unsafe fn read_cstr(addr: usize) -> String {
    if addr == 0 {
        return String::new();
    }
    let p = addr as *const u8;
    let mut out = String::new();
    for i in 0..256isize {
        let b = *p.offset(i);
        if b == 0 {
            break;
        }
        out.push(if (0x20..0x7f).contains(&b) { b as char } else { ' ' });
    }
    out.trim_end().to_string()
}

unsafe extern "cdecl" fn on_print(a: u32, b: u32, c: u32, d: u32) -> u32 {
    let orig: Print4 = transmute(*ORIG_PRINT.get().unwrap());
    let ret = orig(a, b, c, d);
    // Always log that it fired, plus both candidate buffers.
    let main = read_cstr(0x004B_6260);
    let sec = read_cstr(0x004B_62B0);
    log(&format!("[printer fire] sec='{sec}' main='{main}'"));
    ret
}

unsafe extern "cdecl" fn on_fmt(dest: u32, a: u32, b: u32) -> u32 {
    let orig: Fmt3 = transmute(*ORIG_FMT.get().unwrap());
    let ret = orig(dest, a, b);
    if FMT_LOGGED.load(Ordering::Relaxed) < FMT_LOG_CAP {
        let text = read_cstr(dest as usize);
        // Only log lines with real letters (skip empties / single glyphs).
        if text.chars().filter(|c| c.is_ascii_alphabetic()).count() >= 3 {
            FMT_LOGGED.fetch_add(1, Ordering::Relaxed);
            log(&format!("[fmt] {text}"));
            push(text);
        }
    }
    ret
}

fn hook(addr: usize, detour: *mut c_void, orig: &OnceLock<usize>, name: &str) {
    unsafe {
        let target = addr as *mut c_void;
        match MinHook::create_hook(target, detour) {
            Ok(tramp) => {
                let _ = orig.set(tramp as usize);
                match MinHook::enable_hook(target) {
                    Ok(()) => log(&format!("[wizmod] hook enabled: {name} @0x{addr:X}")),
                    Err(e) => log(&format!("[wizmod] enable {name} failed: {e:?}")),
                }
            }
            Err(e) => log(&format!("[wizmod] create {name} failed: {e:?}")),
        }
    }
}

/// Install diagnostic hooks. Call once from the worker thread.
pub fn install() {
    hook(FORMATTER, on_fmt as *mut c_void, &ORIG_FMT, "formatter");
    hook(COMBAT_PRINTER, on_print as *mut c_void, &ORIG_PRINT, "printer");
}
