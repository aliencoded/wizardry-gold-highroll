// Wizardry Gold patch logic (roll + speed). Pure byte ops over the exe;
// mirrors patch_wizgold.py / patch_speed.py and the eframe GUI. No UI here.
// See ../../docs/TIMING.md and ../../WRITEUP.md for the reverse-engineering.

use serde::Serialize;
use std::fs;
use std::path::{Path, PathBuf};

pub const EXE_NAMES: &[&str] = &["WIZARD_720.EXE", "WIZARD.EXE"];

// --- Roll patch -------------------------------------------------------------
const GUARD_PREFIX: &[u8] = &[0x0f, 0xbf, 0x05, 0xa0, 0x20, 0x4a, 0x00, 0x83, 0xf8, 0x01];
const ORIG_JNE: &[u8] = &[0x0f, 0x85, 0x16, 0x00, 0x00, 0x00];
const NOP6: &[u8] = &[0x90; 6];
const ORIG_SPREAD: u8 = 0x0a;
const ORIG_BASE: u8 = 0x15;
const SPREAD_REL: usize = 7;
const BASE_REL: usize = 0x15;

// --- Speed patch ------------------------------------------------------------
const SHR_PREFIX: &[u8] = &[0xc1, 0x6d, 0xfc];
const SAR_PREFIX: &[u8] = &[0xc1, 0x3d, 0x5c, 0x0b, 0x4a, 0x00];
const STOCK_SHIFT: u8 = 7;
const FACTORS: &[(u32, u8)] = &[(1, 7), (2, 6), (4, 5), (8, 4), (16, 3)];
const SEARCH_PREFIX: &[u8] = &[0xff, 0x15, 0x6c, 0xe4, 0x6a, 0x00, 0x83, 0xc0];
const STOCK_SEARCH_MS: u8 = 0x21;
// Text dwell loads (stock 50 ms), TWO forms, both followed by push+call 0x4175AA.
//   FULL (27): mov ax,[0x4A0FA8]  -> mov ax,<MS>; nop; nop          (6B)
//   HALF (19): movsx eax,[0x4A0FA8]; cdq; sub eax,edx; sar eax,1
//                                  -> mov eax,<MS>; nop x8           (13B)
const TEXT_FULL_STOCK: &[u8] = &[0x66, 0xa1, 0xa8, 0x0f, 0x4a, 0x00];
const TEXT_HALF_STOCK: &[u8] = &[0x0f, 0xbf, 0x05, 0xa8, 0x0f, 0x4a, 0x00, 0x99, 0x2b, 0xc2, 0xc1, 0xf8, 0x01];

// --- helpers ----------------------------------------------------------------
fn find_subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    hay.windows(needle.len()).position(|w| w == needle)
}

fn all_offsets(hay: &[u8], needle: &[u8]) -> Vec<usize> {
    let mut out = Vec::new();
    if needle.is_empty() {
        return out;
    }
    let mut i = 0;
    while i + needle.len() <= hay.len() {
        if &hay[i..i + needle.len()] == needle {
            out.push(i);
            i += needle.len();
        } else {
            i += 1;
        }
    }
    out
}

fn shift_to_factor(s: u8) -> Option<u32> {
    FACTORS.iter().find(|(_, sh)| *sh == s).map(|(f, _)| *f)
}
fn factor_to_shift(f: u32) -> u8 {
    FACTORS.iter().find(|(fa, _)| *fa == f).map(|(_, s)| *s).unwrap_or(STOCK_SHIFT)
}
fn text_full_patched(ms: u16) -> Vec<u8> {
    let mut v = vec![0x66u8, 0xb8];
    v.extend_from_slice(&ms.to_le_bytes());
    v.extend_from_slice(&[0x90, 0x90]);
    v
}
fn text_half_patched(ms: u16) -> Vec<u8> {
    let mut v = vec![0xb8u8];
    v.extend_from_slice(&(ms as u32).to_le_bytes());
    v.extend_from_slice(&[0x90; 8]);
    v
}
fn text_full_sites(data: &[u8]) -> Vec<(usize, u16)> {
    let mut out = Vec::new();
    let mut i = 0;
    while i + 6 <= data.len() {
        if data[i] == 0x66 && data[i + 1] == 0xb8 && data[i + 4] == 0x90 && data[i + 5] == 0x90 {
            out.push((i, u16::from_le_bytes([data[i + 2], data[i + 3]])));
            i += 6;
        } else {
            i += 1;
        }
    }
    out
}
fn text_half_sites(data: &[u8]) -> Vec<(usize, u16)> {
    let mut out = Vec::new();
    let mut i = 0;
    while i + 13 <= data.len() {
        if data[i] == 0xb8 && data[i + 5..i + 13].iter().all(|&b| b == 0x90) {
            out.push((i, u16::from_le_bytes([data[i + 1], data[i + 2]])));
            i += 13;
        } else {
            i += 1;
        }
    }
    out
}

// --- exe discovery ----------------------------------------------------------
fn candidate_dirs() -> Vec<PathBuf> {
    let libs = [
        r"Program Files (x86)\Steam\steamapps\common",
        r"Program Files\Steam\steamapps\common",
        r"SteamLibrary\steamapps\common",
        r"Steam\steamapps\common",
        "GOG Games",
        "GOG",
        "Games",
        "",
    ];
    let mut out = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            out.push(d.to_path_buf());
        }
    }
    for drive in ["C:", "D:", "E:", "F:", "G:"] {
        for lib in libs {
            let base = if lib.is_empty() { format!("{}\\", drive) } else { format!("{}\\{}", drive, lib) };
            let p = PathBuf::from(base);
            if p.is_dir() {
                out.push(p);
            }
        }
    }
    out
}
fn dir_has_exe(dir: &Path) -> Option<PathBuf> {
    for name in EXE_NAMES {
        let p = dir.join(name);
        if p.is_file() {
            return Some(p);
        }
    }
    None
}
pub fn find_exe() -> Option<PathBuf> {
    if let Ok(v) = std::env::var("WIZGOLD_EXE") {
        let p = PathBuf::from(v);
        if p.is_file() {
            return Some(p);
        }
    }
    for base in candidate_dirs() {
        if let Some(p) = dir_has_exe(&base) {
            return Some(p);
        }
        if let Ok(rd) = fs::read_dir(&base) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_dir() {
                    if let Some(f) = dir_has_exe(&p) {
                        return Some(f);
                    }
                }
            }
        }
    }
    None
}
pub fn sibling_exes(path: &str) -> Vec<String> {
    let mut out = Vec::new();
    if let Some(dir) = Path::new(path).parent() {
        for name in EXE_NAMES {
            let p = dir.join(name);
            if p.is_file() {
                out.push(p.to_string_lossy().into_owned());
            }
        }
    }
    if out.is_empty() {
        out.push(path.to_string());
    }
    out
}

// --- status structs (serialized to the frontend) ----------------------------
#[derive(Serialize, Clone)]
pub struct RollState {
    pub health: String, // "patched" | "vanilla" | "bad"
    pub line: String,
    pub lo: Option<i32>,
    pub hi: Option<i32>,
}
#[derive(Serialize, Clone)]
pub struct SpeedState {
    pub found: bool,
    pub factor: Option<u32>,
    pub search_ms: u8,
    pub text_full_ms: Option<u16>, // MAIN tier common ms (None = stock/mixed)
    pub text_half_ms: Option<u16>, // QUICK (half) tier common ms
    pub text_full_done: usize,
    pub text_half_done: usize,
}
#[derive(Serialize, Clone)]
pub struct FullStatus {
    pub roll: RollState,
    pub speed: SpeedState,
}

fn locate_roll(data: &[u8]) -> Option<(usize, bool)> {
    if let Some(i) = find_subseq(data, &[GUARD_PREFIX, ORIG_JNE].concat()) {
        return Some((i + GUARD_PREFIX.len(), false));
    }
    if let Some(i) = find_subseq(data, &[GUARD_PREFIX, NOP6].concat()) {
        return Some((i + GUARD_PREFIX.len(), true));
    }
    None
}

pub fn roll_describe(data: &[u8]) -> RollState {
    match locate_roll(data) {
        Some((off, false)) => RollState {
            health: "vanilla".into(),
            line: format!("Vanilla - normal rolls ~5-11 (site 0x{:X})", off),
            lo: None,
            hi: None,
        },
        Some((off, true)) => {
            let spread = data[off + SPREAD_REL] as i32;
            let base = data[off + BASE_REL] as i32;
            RollState {
                health: "patched".into(),
                line: format!("Patched - elite roll active (site 0x{:X})", off),
                lo: Some(base),
                hi: Some(base + spread - 1),
            }
        }
        None => RollState {
            health: "bad".into(),
            line: "Signature not found - unsupported game build.".into(),
            lo: None,
            hi: None,
        },
    }
}

pub fn speed_describe(data: &[u8]) -> SpeedState {
    let shr = find_subseq(data, SHR_PREFIX).map(|o| o + SHR_PREFIX.len());
    let search = find_subseq(data, SEARCH_PREFIX).map(|o| o + SEARCH_PREFIX.len());
    let full_p = text_full_sites(data);
    let half_p = text_half_sites(data);
    let uniform = |v: &[(usize, u16)]| -> Option<u16> {
        if v.is_empty() {
            return None;
        }
        let set: std::collections::BTreeSet<u16> = v.iter().map(|(_, m)| *m).collect();
        if set.len() == 1 { Some(*set.iter().next().unwrap()) } else { None }
    };
    SpeedState {
        found: shr.is_some(),
        factor: shr.and_then(|o| shift_to_factor(data[o])),
        search_ms: search.map(|o| data[o]).unwrap_or(STOCK_SEARCH_MS),
        text_full_ms: uniform(&full_p),
        text_half_ms: uniform(&half_p),
        text_full_done: full_p.len(),
        text_half_done: half_p.len(),
    }
}

pub fn full_status(path: &str) -> Result<FullStatus, String> {
    let data = fs::read(path).map_err(|e| format!("Cannot read exe: {e}"))?;
    Ok(FullStatus { roll: roll_describe(&data), speed: speed_describe(&data) })
}

// --- mutations (single exe) -------------------------------------------------
fn roll_apply(path: &str, lo: i32, hi: i32) -> String {
    let (mut lo, mut hi) = (lo, hi);
    if lo > hi {
        std::mem::swap(&mut lo, &mut hi);
    }
    let spread = hi - lo + 1;
    if !(0..=127).contains(&lo) || !(1..=127).contains(&spread) {
        return "Range invalid: min 0..127 and span 1..127.".into();
    }
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let off = match locate_roll(&data) {
        Some((o, _)) => o,
        None => return "Unsupported build (signature not found).".into(),
    };
    let bak = format!("{}.bak", path);
    if matches!(locate_roll(&data), Some((_, false))) && !Path::new(&bak).exists() {
        if let Err(e) = fs::copy(path, &bak) {
            return format!("Backup failed: {e}");
        }
    }
    data[off..off + 6].copy_from_slice(NOP6);
    data[off + SPREAD_REL] = spread as u8;
    data[off + BASE_REL] = lo as u8;
    match fs::write(path, &data) {
        Ok(_) => format!("Rolls {}-{}.", lo, hi),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

fn roll_revert(path: &str) -> String {
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let off = match locate_roll(&data) {
        Some((_, false)) => return "Already vanilla.".into(),
        Some((o, true)) => o,
        None => return "Signature not found.".into(),
    };
    data[off..off + 6].copy_from_slice(ORIG_JNE);
    data[off + SPREAD_REL] = ORIG_SPREAD;
    data[off + BASE_REL] = ORIG_BASE;
    match fs::write(path, &data) {
        Ok(_) => "Reverted to vanilla rolls.".into(),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

fn speed_apply(path: &str, factor: u32, search: u8, text_full: u16, text_half: u16) -> String {
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let (Some(shr0), Some(sar0)) = (find_subseq(&data, SHR_PREFIX), find_subseq(&data, SAR_PREFIX)) else {
        return "Speed signature not found.".into();
    };
    let sh = factor_to_shift(factor);
    data[shr0 + SHR_PREFIX.len()] = sh;
    data[sar0 + SAR_PREFIX.len()] = sh;
    if let Some(o) = find_subseq(&data, SEARCH_PREFIX) {
        data[o + SEARCH_PREFIX.len()] = search;
    }
    if (1..=32767).contains(&text_full) {
        let new = text_full_patched(text_full);
        for o in all_offsets(&data, TEXT_FULL_STOCK) {
            data[o..o + 6].copy_from_slice(&new);
        }
        for (o, cur) in text_full_sites(&data) {
            if cur != text_full {
                data[o..o + 6].copy_from_slice(&new);
            }
        }
    }
    if (1..=32767).contains(&text_half) {
        let new = text_half_patched(text_half);
        for o in all_offsets(&data, TEXT_HALF_STOCK) {
            data[o..o + 13].copy_from_slice(&new);
        }
        for (o, cur) in text_half_sites(&data) {
            if cur != text_half {
                data[o..o + 13].copy_from_slice(&new);
            }
        }
    }
    match fs::write(path, &data) {
        Ok(_) => format!("Speed {factor}x, search {search}ms, text {text_full}/{text_half}ms."),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

fn speed_revert(path: &str) -> String {
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    if find_subseq(&data, SHR_PREFIX).is_none() {
        return "Speed signature not found.".into();
    }
    if let Some(o) = find_subseq(&data, SHR_PREFIX) {
        data[o + SHR_PREFIX.len()] = STOCK_SHIFT;
    }
    if let Some(o) = find_subseq(&data, SAR_PREFIX) {
        data[o + SAR_PREFIX.len()] = STOCK_SHIFT;
    }
    if let Some(o) = find_subseq(&data, SEARCH_PREFIX) {
        data[o + SEARCH_PREFIX.len()] = STOCK_SEARCH_MS;
    }
    for (o, _) in text_full_sites(&data) {
        data[o..o + 6].copy_from_slice(TEXT_FULL_STOCK);
    }
    for (o, _) in text_half_sites(&data) {
        data[o..o + 13].copy_from_slice(TEXT_HALF_STOCK);
    }
    match fs::write(path, &data) {
        Ok(_) => "Speed reverted to stock.".into(),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

// --- apply across sibling exes (combined report) ----------------------------
fn over_siblings(path: &str, op: impl Fn(&str) -> String) -> String {
    let targets = sibling_exes(path);
    let n = targets.len();
    let mut lines = Vec::new();
    for t in &targets {
        let name = Path::new(t).file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| t.clone());
        lines.push(format!("{name}: {}", op(t)));
    }
    if n > 1 { format!("{n} copies.\n{}", lines.join("\n")) } else { lines.join("\n") }
}

pub fn apply_roll_all(path: &str, lo: i32, hi: i32) -> String {
    over_siblings(path, |t| roll_apply(t, lo, hi))
}
pub fn revert_roll_all(path: &str) -> String {
    over_siblings(path, roll_revert)
}
pub fn apply_speed_all(path: &str, factor: u32, search: u8, text_full: u16, text_half: u16) -> String {
    over_siblings(path, |t| speed_apply(t, factor, search, text_full, text_half))
}
pub fn revert_speed_all(path: &str) -> String {
    over_siblings(path, speed_revert)
}

#[cfg(test)]
mod tests {
    use super::*;
    const PRISTINE: &str = r"D:/Wizardry 7 Gold/WIZARD_720.EXE.bak";

    #[test]
    fn roundtrip_matches_pristine() {
        let pristine = fs::read(PRISTINE).expect("pristine present");
        let tmp = std::env::temp_dir().join("wz_tauri_test.exe");
        let tp = tmp.to_string_lossy().to_string();
        fs::write(&tp, &pristine).unwrap();

        let s = speed_describe(&fs::read(&tp).unwrap());
        assert_eq!(s.text_full_ms, None);
        assert_eq!(s.text_half_ms, None);
        assert_eq!(s.factor, Some(1));

        speed_apply(&tp, 4, 8, 1500, 700);
        roll_apply(&tp, 21, 30);
        let s = full_status(&tp).unwrap();
        assert_eq!(s.speed.factor, Some(4));
        assert_eq!(s.speed.search_ms, 8);
        assert_eq!(s.speed.text_full_ms, Some(1500));
        assert_eq!(s.speed.text_half_ms, Some(700));
        assert_eq!(s.speed.text_full_done, 27);
        assert_eq!(s.speed.text_half_done, 19);
        assert_eq!(s.roll.health, "patched");

        speed_revert(&tp);
        roll_revert(&tp);
        // .bak may have been created next to tmp; ignore it.
        assert_eq!(fs::read(&tp).unwrap(), pristine, "revert must match pristine");
        let _ = fs::remove_file(&tp);
        let _ = fs::remove_file(format!("{tp}.bak"));
    }
}
