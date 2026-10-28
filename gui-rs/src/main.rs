// Wizardry Gold - High Roll Patcher (GUI)
// One-click, no Python. Forces the character-creation bonus roll into a chosen
// range so high-requirement classes (e.g. Faery Ninja) are selectable.
//
// The patch NOPs the conditional jump that guards the game's dormant "elite
// roll" branch, then optionally rewrites the two immediate bytes that define
// the roll range (`random(spread) + base`). See WRITEUP.md for the analysis.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use eframe::egui;
use egui::{Color32, FontId, Pos2, Rect, RichText, Rounding, Stroke, Vec2};
use std::fs;
use std::path::{Path, PathBuf};

// Signature of the elite-roll guard (movsx eax,[0x4A20A0]; cmp eax,1; jne ...).
const GUARD_PREFIX: &[u8] = &[0x0f, 0xbf, 0x05, 0xa0, 0x20, 0x4a, 0x00, 0x83, 0xf8, 0x01];
const ORIG_JNE: &[u8] = &[0x0f, 0x85, 0x16, 0x00, 0x00, 0x00];
const NOP6: &[u8] = &[0x90; 6];
// Vanilla dev "elite roll" immediates (push 0x0A; add eax,0x15 => random(10)+21).
// Restored on revert so the file becomes byte-identical to the pristine exe.
const ORIG_SPREAD: u8 = 0x0a;
const ORIG_BASE: u8 = 0x15;
const SPREAD_REL: usize = 7; // jne_site + 7   -> push <spread>  (random arg)
const BASE_REL: usize = 0x15; // jne_site + 0x15 -> add eax, <base>
const EXE_NAMES: &[&str] = &["WIZARD_720.EXE", "WIZARD.EXE"];

// --- Game-speed patch (see docs/TIMING.md) ---------------------------------
// Global step-timer time unit: shr/sar [..],7 (128 ms). Lower shift = faster.
const SHR_PREFIX: &[u8] = &[0xc1, 0x6d, 0xfc]; // shr dword [ebp-4], <shift>
const SAR_PREFIX: &[u8] = &[0xc1, 0x3d, 0x5c, 0x0b, 0x4a, 0x00]; // sar [0x4A0B5C], <shift>
const STOCK_SHIFT: u8 = 7;
// (factor, shift): 1x..16x
const FACTORS: &[(u32, u8)] = &[(1, 7), (2, 6), (4, 5), (8, 4), (16, 3)];
// Eyeball/search screen-wipe per-frame delay: GetTickCount; add eax,<ms>.
const SEARCH_PREFIX: &[u8] = &[0xff, 0x15, 0x6c, 0xe4, 0x6a, 0x00, 0x83, 0xc0];
const STOCK_SEARCH_MS: u8 = 0x21; // 33 ms
// Message/combat text dwell loads (stock 50 ms). TWO forms, both followed by
// push+call the text-wait 0x4175AA; patch BOTH or text speed is uneven:
//   FULL (27 sites): mov ax,[0x4A0FA8]    -> mov ax,<MS>; nop; nop      (6B)
//   HALF (19 sites): movsx eax,[0x4A0FA8]; cdq; sub eax,edx; sar eax,1
//                                          -> mov eax,<MS>; nop x8       (13B)
const TEXT_FULL_STOCK: &[u8] = &[0x66, 0xa1, 0xa8, 0x0f, 0x4a, 0x00];
const TEXT_HALF_STOCK: &[u8] = &[0x0f, 0xbf, 0x05, 0xa8, 0x0f, 0x4a, 0x00, 0x99, 0x2b, 0xc2, 0xc1, 0xf8, 0x01];
const STOCK_TEXT_MS: u16 = 0x32;

fn shift_to_factor(s: u8) -> Option<u32> {
    FACTORS.iter().find(|(_, sh)| *sh == s).map(|(f, _)| *f)
}
fn factor_to_shift(f: u32) -> u8 {
    FACTORS.iter().find(|(fa, _)| *fa == f).map(|(_, s)| *s).unwrap_or(STOCK_SHIFT)
}

/// FULL patched form: `mov ax,<ms>; nop; nop` (6 bytes).
fn text_full_patched(ms: u16) -> Vec<u8> {
    let mut v = vec![0x66u8, 0xb8];
    v.extend_from_slice(&ms.to_le_bytes());
    v.extend_from_slice(&[0x90, 0x90]);
    v
}
/// HALF patched form: `mov eax,<ms>; nop x8` (13 bytes).
fn text_half_patched(ms: u16) -> Vec<u8> {
    let mut v = vec![0xb8u8];
    v.extend_from_slice(&(ms as u32).to_le_bytes());
    v.extend_from_slice(&[0x90; 8]);
    v
}

/// Offsets of patched FULL sites (`66 b8 imm16 90 90`) + their ms.
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
/// Offsets of patched HALF sites (`b8 imm32 90*8`) + their ms (low 16 bits).
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

/// Speed-patch state of one exe. Text has two tiers (see docs/TIMING.md):
/// MAIN (full message-speed) and QUICK (half). Each can differ.
#[derive(Clone)]
struct SpeedState {
    found: bool,
    factor: Option<u32>,         // None = unknown shift
    search_ms: u8,
    text_full_ms: Option<u16>,   // common ms of patched MAIN sites (None = stock/mixed)
    text_half_ms: Option<u16>,   // common ms of patched QUICK sites
    text_full_done: usize,
    text_half_done: usize,
}

fn speed_describe(data: &[u8]) -> SpeedState {
    let shr = find_subseq(data, SHR_PREFIX).map(|o| o + SHR_PREFIX.len());
    let search = find_subseq(data, SEARCH_PREFIX).map(|o| o + SEARCH_PREFIX.len());
    let full_p = text_full_sites(data);
    let half_p = text_half_sites(data);
    let uniform = |v: &[(usize, u16)]| -> Option<u16> {
        let set: std::collections::BTreeSet<u16> = v.iter().map(|(_, m)| *m).collect();
        if set.len() == 1 { Some(*set.iter().next().unwrap()) } else { None }
    };
    SpeedState {
        found: shr.is_some(),
        factor: shr.and_then(|o| shift_to_factor(data[o])),
        search_ms: search.map(|o| data[o]).unwrap_or(STOCK_SEARCH_MS),
        text_full_ms: if full_p.is_empty() { None } else { uniform(&full_p) },
        text_half_ms: if half_p.is_empty() { None } else { uniform(&half_p) },
        text_full_done: full_p.len(),
        text_half_done: half_p.len(),
    }
}

/// Apply global speed factor (both shift bytes) to one exe.
fn speed_set_factor(path: &str, factor: u32) -> String {
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let shr = match find_subseq(&data, SHR_PREFIX) {
        Some(o) => o + SHR_PREFIX.len(),
        None => return "Speed signature not found (unsupported build).".into(),
    };
    let sar = match find_subseq(&data, SAR_PREFIX) {
        Some(o) => o + SAR_PREFIX.len(),
        None => return "Speed signature not found (unsupported build).".into(),
    };
    let sh = factor_to_shift(factor);
    data[shr] = sh;
    data[sar] = sh;
    match fs::write(path, &data) {
        Ok(_) => format!("Global speed -> {factor}x."),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

fn speed_set_search(path: &str, ms: u8) -> String {
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let off = match find_subseq(&data, SEARCH_PREFIX) {
        Some(o) => o + SEARCH_PREFIX.len(),
        None => return "Search signature not found.".into(),
    };
    data[off] = ms;
    match fs::write(path, &data) {
        Ok(_) => format!("Search sweep -> {ms} ms/frame."),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

/// Set text dwell per tier (ms). `full`/`half` = None leaves that tier alone.
fn speed_set_text(path: &str, full: Option<u16>, half: Option<u16>) -> String {
    for ms in [full, half].into_iter().flatten() {
        if !(1..=32767).contains(&ms) {
            return "Text ms must be 1..32767.".into();
        }
    }
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let mut n = 0;
    if let Some(ms) = full {
        let new = text_full_patched(ms);
        for o in all_offsets(&data, TEXT_FULL_STOCK) {
            data[o..o + 6].copy_from_slice(&new);
            n += 1;
        }
        for (o, cur) in text_full_sites(&data) {
            if cur != ms {
                data[o..o + 6].copy_from_slice(&new);
                n += 1;
            }
        }
    }
    if let Some(ms) = half {
        let new = text_half_patched(ms);
        for o in all_offsets(&data, TEXT_HALF_STOCK) {
            data[o..o + 13].copy_from_slice(&new);
            n += 1;
        }
        for (o, cur) in text_half_sites(&data) {
            if cur != ms {
                data[o..o + 13].copy_from_slice(&new);
                n += 1;
            }
        }
    }
    match fs::write(path, &data) {
        Ok(_) => format!("Text dwell set (main {:?}, quick {:?}); {n} sites. Enter skips.", full, half),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

/// Revert all speed patches (global 1x, search 33, text 50) on one exe.
fn speed_revert(path: &str) -> String {
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let mut did = false;
    if let Some(o) = find_subseq(&data, SHR_PREFIX) {
        data[o + SHR_PREFIX.len()] = STOCK_SHIFT;
        did = true;
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
    if !did {
        return "Speed signature not found.".into();
    }
    match fs::write(path, &data) {
        Ok(_) => "Speed reverted to stock (1x, search 33, text 50).".into(),
        Err(e) => format!("Write failed (close game / run as admin): {e}"),
    }
}

/// Run a per-exe speed op across all sibling exes; combined report.
fn speed_all(path: &str, op: impl Fn(&str) -> String) -> String {
    let targets = sibling_exes(path);
    let n = targets.len();
    let mut lines = Vec::new();
    for t in &targets {
        let name = Path::new(t).file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| t.clone());
        lines.push(format!("{name}: {}", op(t)));
    }
    if n > 1 {
        format!("{n} copies.\n{}", lines.join("\n"))
    } else {
        lines.join("\n")
    }
}

// --- Wizardry-themed palette ------------------------------------------------
const GOLD: Color32 = Color32::from_rgb(0xD4, 0xAF, 0x37);
const GOLD_DIM: Color32 = Color32::from_rgb(0x9c, 0x80, 0x2a);
const PARCH: Color32 = Color32::from_rgb(0xE8, 0xDC, 0xC0);
const BG: Color32 = Color32::from_rgb(0x16, 0x12, 0x1d);
const PANEL: Color32 = Color32::from_rgb(0x22, 0x1c, 0x2b);
const PANEL_HI: Color32 = Color32::from_rgb(0x2e, 0x26, 0x3a);
const GREEN: Color32 = Color32::from_rgb(0x5a, 0xc8, 0x6a);
const AMBER: Color32 = Color32::from_rgb(0xd8, 0x9a, 0x3a);
const RED: Color32 = Color32::from_rgb(0xd0, 0x4a, 0x4a);
const INK: Color32 = Color32::from_rgb(0xb6, 0xa8, 0xc4);

fn sig_orig() -> Vec<u8> {
    [GUARD_PREFIX, ORIG_JNE].concat()
}
fn sig_patched() -> Vec<u8> {
    [GUARD_PREFIX, NOP6].concat()
}

fn find_subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    hay.windows(needle.len()).position(|w| w == needle)
}

/// Returns (jne_offset, is_patched) if the guard signature is found.
fn locate(data: &[u8]) -> Option<(usize, bool)> {
    if let Some(i) = find_subseq(data, &sig_orig()) {
        return Some((i + GUARD_PREFIX.len(), false));
    }
    if let Some(i) = find_subseq(data, &sig_patched()) {
        return Some((i + GUARD_PREFIX.len(), true));
    }
    None
}

fn candidate_dirs() -> Vec<PathBuf> {
    let libs = [
        r"Program Files (x86)\Steam\steamapps\common",
        r"Program Files\Steam\steamapps\common",
        r"SteamLibrary\steamapps\common",
        r"Steam\steamapps\common",
        "GOG Games",
        "GOG",
        "Games",
        "", // drive root (folders like "Wizardry 7 Gold")
    ];
    let mut out = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            out.push(d.to_path_buf());
        }
    }
    for drive in ["C:", "D:", "E:", "F:", "G:"] {
        for lib in libs {
            let base = if lib.is_empty() {
                format!("{}\\", drive)
            } else {
                format!("{}\\{}", drive, lib)
            };
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

/// Auto-detect the game exe: $WIZGOLD_EXE, then common locations (depth 1).
fn find_exe() -> Option<PathBuf> {
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

/// State of the located exe, used to drive the status badge.
#[derive(Clone, Copy, PartialEq)]
enum Health {
    Patched,
    Unpatched,
    Bad,
}

struct Status {
    health: Health,
    line: String,
    range: Option<(i32, i32)>,
}

/// All game-exe copies in the same folder as `path` (e.g. both WIZARD.EXE and
/// WIZARD_720.EXE). The launcher swaps which name it runs, so we patch every
/// copy present to avoid patching the wrong one.
fn sibling_exes(path: &str) -> Vec<String> {
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

/// Apply to every game exe in the folder; returns a combined report.
fn apply_all(path: &str, lo: i32, hi: i32) -> String {
    let targets = sibling_exes(path);
    let n = targets.len();
    let mut lines = Vec::new();
    for t in &targets {
        let name = Path::new(t).file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| t.clone());
        lines.push(format!("{name}: {}", apply(t, lo, hi)));
    }
    if n > 1 {
        format!("Patched {n} copies.\n{}", lines.join("\n"))
    } else {
        lines.join("\n")
    }
}

/// Revert every game exe in the folder; returns a combined report.
fn revert_all(path: &str) -> String {
    let targets = sibling_exes(path);
    let n = targets.len();
    let mut lines = Vec::new();
    for t in &targets {
        let name = Path::new(t).file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| t.clone());
        lines.push(format!("{name}: {}", revert(t)));
    }
    if n > 1 {
        format!("Reverted {n} copies.\n{}", lines.join("\n"))
    } else {
        lines.join("\n")
    }
}

fn describe(path: &str) -> Status {
    match fs::read(path) {
        Ok(data) => match locate(&data) {
            Some((off, false)) => Status {
                health: Health::Unpatched,
                line: format!("Vanilla - normal rolls ~5-11  (site 0x{:X})", off),
                range: None,
            },
            Some((off, true)) => {
                let spread = data[off + SPREAD_REL] as i32;
                let base = data[off + BASE_REL] as i32;
                Status {
                    health: Health::Patched,
                    line: format!("Patched - elite roll active  (site 0x{:X})", off),
                    range: Some((base, base + spread - 1)),
                }
            }
            None => Status {
                health: Health::Bad,
                line: "Signature not found - unsupported game build.".into(),
                range: None,
            },
        },
        Err(e) => Status {
            health: Health::Bad,
            line: format!("Cannot read exe: {e}"),
            range: None,
        },
    }
}

fn apply(path: &str, lo: i32, hi: i32) -> String {
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
    let off = match locate(&data) {
        Some((o, _)) => o,
        None => return "Unsupported build (signature not found).".into(),
    };
    // Only back up a PRISTINE exe. Never save a patched file as the backup
    // (that made revert restore patched bytes - the original bug).
    let bak = format!("{}.bak", path);
    let is_pristine = matches!(locate(&data), Some((_, false)));
    if is_pristine && !Path::new(&bak).exists() {
        if let Err(e) = fs::copy(path, &bak) {
            return format!("Backup failed: {e}");
        }
    }
    data[off..off + 6].copy_from_slice(NOP6);
    data[off + SPREAD_REL] = spread as u8;
    data[off + BASE_REL] = lo as u8;
    match fs::write(path, &data) {
        Ok(_) => format!("Patched. Every creation now rolls {}-{}.", lo, hi),
        Err(e) => format!("Write failed (close the game / run as admin): {e}"),
    }
}

fn revert(path: &str) -> String {
    // Restore original bytes directly. We do NOT copy back <exe>.bak: that
    // backup can itself be a patched file, in which case the "revert" would
    // restore patched bytes and nothing would change.
    let mut data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => return format!("Read error: {e}"),
    };
    let off = match locate(&data) {
        Some((_, false)) => return "Already vanilla - nothing to revert.".into(),
        Some((o, true)) => o,
        None => return "Signature not found - unsupported build.".into(),
    };
    data[off..off + 6].copy_from_slice(ORIG_JNE);
    data[off + SPREAD_REL] = ORIG_SPREAD;
    data[off + BASE_REL] = ORIG_BASE;
    match fs::write(path, &data) {
        Ok(_) => "Reverted to vanilla rolls (~5-11). Restored original bytes.".into(),
        Err(e) => format!("Write failed (close the game / run as admin): {e}"),
    }
}

// --- Painted graphics -------------------------------------------------------

/// Draw a stylized d20 (hexagon silhouette with inner facets) showing `num`.
fn draw_die(ui: &mut egui::Ui, size: f32, num: i32, accent: Color32) {
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(size), egui::Sense::hover());
    let p = ui.painter();
    let c = rect.center();
    let r = size * 0.46;
    // Six outer vertices (pointy-top hexagon).
    let mut outer = [Pos2::ZERO; 6];
    for i in 0..6 {
        let a = std::f32::consts::PI / 2.0 + i as f32 * std::f32::consts::PI / 3.0;
        outer[i] = c + Vec2::new(a.cos() * r, -a.sin() * r);
    }
    // Body fill.
    p.add(egui::Shape::convex_polygon(
        outer.to_vec(),
        PANEL_HI,
        Stroke::new(2.0, accent),
    ));
    // Inner triangle facets for a gem-cut look.
    let inner_r = r * 0.42;
    let mut inner = [Pos2::ZERO; 3];
    for i in 0..3 {
        let a = std::f32::consts::PI / 2.0 + i as f32 * 2.0 * std::f32::consts::PI / 3.0;
        inner[i] = c + Vec2::new(a.cos() * inner_r, -a.sin() * inner_r);
    }
    let facet = Stroke::new(1.0, GOLD_DIM);
    for i in 0..6 {
        p.line_segment([c, outer[i]], facet);
    }
    p.add(egui::Shape::closed_line(inner.to_vec(), facet));
    // Number.
    p.text(
        c,
        egui::Align2::CENTER_CENTER,
        num.to_string(),
        FontId::proportional(size * 0.34),
        PARCH,
    );
}

/// Horizontal bar 0..40 with the lo..hi window highlighted in gold.
fn draw_range_bar(ui: &mut egui::Ui, lo: i32, hi: i32) {
    let h = 16.0;
    let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), h), egui::Sense::hover());
    let p = ui.painter();
    let max = 40.0;
    p.rect_filled(rect, Rounding::same(5.0), PANEL);
    let x = |v: f32| rect.left() + (v / max) * rect.width();
    let lo = lo.clamp(0, 40) as f32;
    let hi = (hi.clamp(0, 40) as f32 + 1.0).min(max);
    let win = Rect::from_min_max(
        Pos2::new(x(lo), rect.top()),
        Pos2::new(x(hi), rect.bottom()),
    );
    p.rect_filled(win, Rounding::same(5.0), GOLD);
    // "vanilla" zone marker (~5-11) drawn as a faint tick band.
    let van = Rect::from_min_max(
        Pos2::new(x(5.0), rect.center().y - 1.0),
        Pos2::new(x(11.0), rect.center().y + 1.0),
    );
    p.rect_filled(van, Rounding::ZERO, INK.gamma_multiply(0.5));
    p.rect_stroke(rect, Rounding::same(5.0), Stroke::new(1.0, GOLD_DIM));
}

/// Distance from point `p` to segment `a`-`b`.
fn dist_seg(p: Pos2, a: Pos2, b: Pos2) -> f32 {
    let ab = b - a;
    let t = ((p - a).dot(ab) / ab.length_sq()).clamp(0.0, 1.0);
    (p - (a + ab * t)).length()
}

/// Rasterize a gold d20 gem on a transparent field for the window/taskbar icon.
/// Pure pixels, no image file, no extra deps.
fn gen_icon() -> egui::IconData {
    let n = 64usize;
    let c = Pos2::new(n as f32 / 2.0 - 0.5, n as f32 / 2.0 - 0.5);
    let r = n as f32 * 0.46;
    // Pointy-top hexagon vertices.
    let mut o = [Pos2::ZERO; 6];
    for i in 0..6 {
        let a = std::f32::consts::PI / 2.0 + i as f32 * std::f32::consts::PI / 3.0;
        o[i] = c + Vec2::new(a.cos() * r, -a.sin() * r);
    }
    let mut rgba = vec![0u8; n * n * 4];
    let gold = [0xD4u8, 0xAF, 0x37];
    let lite = [0xF2u8, 0xD9, 0x77];
    let dim = [0x6du8, 0x57, 0x1d];
    for y in 0..n {
        for x in 0..n {
            let p = Pos2::new(x as f32, y as f32);
            // Inside convex hexagon: all edge cross-products share a sign.
            let mut inside = true;
            for i in 0..6 {
                let a = o[i];
                let b = o[(i + 1) % 6];
                let cross = (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x);
                if cross > 0.0 {
                    inside = false;
                    break;
                }
            }
            if !inside {
                continue;
            }
            // Nearest distance to a spoke (center -> each vertex) => facet lines.
            let mut spoke = f32::MAX;
            for v in &o {
                spoke = spoke.min(dist_seg(p, c, *v));
            }
            // Edge proximity.
            let mut edge = f32::MAX;
            for i in 0..6 {
                edge = edge.min(dist_seg(p, o[i], o[(i + 1) % 6]));
            }
            let col = if edge < 1.6 {
                dim
            } else if spoke < 1.0 {
                lite
            } else {
                // Subtle top-light shading.
                let t = ((c.y - p.y) / r * 0.35 + 0.65).clamp(0.0, 1.0);
                [
                    (gold[0] as f32 * t) as u8,
                    (gold[1] as f32 * t) as u8,
                    (gold[2] as f32 * t) as u8,
                ]
            };
            let idx = (y * n + x) * 4;
            rgba[idx] = col[0];
            rgba[idx + 1] = col[1];
            rgba[idx + 2] = col[2];
            rgba[idx + 3] = 0xff;
        }
    }
    egui::IconData {
        rgba,
        width: n as u32,
        height: n as u32,
    }
}

struct App {
    exe: String,
    status: Status,
    speed: SpeedState,
    log: String,
    lo: i32,
    hi: i32,
    // Speed controls (edited in the UI before Apply).
    sp_factor: u32,
    sp_search: i32,
    sp_text_full: i32,
    sp_text_half: i32,
}

impl Default for App {
    fn default() -> Self {
        let exe = find_exe()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        let (status, speed) = if exe.is_empty() {
            (
                Status {
                    health: Health::Bad,
                    line: "Game not found - paste the path to WIZARD_720.EXE below.".into(),
                    range: None,
                },
                stock_speed(),
            )
        } else {
            (describe(&exe), fs::read(&exe).map(|d| speed_describe(&d)).unwrap_or(stock_speed()))
        };
        // Seed the editable speed controls from the current on-disk state.
        let sp_factor = speed.factor.unwrap_or(1);
        let sp_search = speed.search_ms as i32;
        let sp_text_full = speed.text_full_ms.map(|m| m as i32).unwrap_or(STOCK_TEXT_MS as i32);
        let sp_text_half = speed.text_half_ms.map(|m| m as i32).unwrap_or(STOCK_TEXT_MS as i32);
        Self {
            exe,
            status,
            speed,
            log: String::new(),
            lo: 21,
            hi: 30,
            sp_factor,
            sp_search,
            sp_text_full,
            sp_text_half,
        }
    }
}

/// Default "nothing found" speed state.
fn stock_speed() -> SpeedState {
    SpeedState {
        found: false,
        factor: None,
        search_ms: STOCK_SEARCH_MS,
        text_full_ms: None,
        text_half_ms: None,
        text_full_done: 0,
        text_half_done: 0,
    }
}

impl App {
    fn refresh(&mut self) {
        if self.exe.is_empty() {
            self.status = Status { health: Health::Bad, line: "No exe path set.".into(), range: None };
            self.speed = stock_speed();
            return;
        }
        self.status = describe(&self.exe);
        if let Ok(d) = fs::read(&self.exe) {
            self.speed = speed_describe(&d);
        }
    }
}

/// A rounded panel "card" with a heading, used for each step.
fn card<R>(ui: &mut egui::Ui, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
    egui::Frame::none()
        .fill(PANEL)
        .rounding(Rounding::same(8.0))
        .inner_margin(egui::Margin::same(12.0))
        .stroke(Stroke::new(1.0, PANEL_HI))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        })
        .inner
}

fn step_label(ui: &mut egui::Ui, n: u32, title: &str) {
    ui.horizontal(|ui| {
        ui.label(RichText::new(format!("{n}")).color(BG).background_color(GOLD).strong());
        ui.label(RichText::new(title).color(GOLD).size(15.0).strong());
    });
    ui.add_space(4.0);
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        egui::CentralPanel::default()
            .frame(egui::Frame::default().fill(BG).inner_margin(egui::Margin::same(16.0)))
            .show(ctx, |ui| {
                egui::ScrollArea::vertical().show(ui, |ui| {
                // --- Banner -------------------------------------------------
                egui::Frame::none()
                    .fill(PANEL)
                    .rounding(Rounding::same(10.0))
                    .inner_margin(egui::Margin::symmetric(14.0, 12.0))
                    .stroke(Stroke::new(1.0, GOLD_DIM))
                    .show(ui, |ui| {
                        ui.horizontal(|ui| {
                            draw_die(ui, 56.0, self.hi.clamp(0, 40), GOLD);
                            ui.add_space(8.0);
                            ui.vertical(|ui| {
                                ui.label(
                                    RichText::new("WIZARDRY GOLD")
                                        .color(GOLD)
                                        .size(22.0)
                                        .strong(),
                                );
                                ui.label(
                                    RichText::new("High-Roll Patcher  -  unlock elite character rolls")
                                        .color(INK)
                                        .size(12.0),
                                );
                            });
                        });
                    });

                ui.add_space(10.0);

                // --- Status badge ------------------------------------------
                let (col, tag) = match self.status.health {
                    Health::Patched => (GREEN, "PATCHED"),
                    Health::Unpatched => (AMBER, "VANILLA"),
                    Health::Bad => (RED, "NO GAME"),
                };
                egui::Frame::none()
                    .fill(col.gamma_multiply(0.16))
                    .rounding(Rounding::same(8.0))
                    .inner_margin(egui::Margin::same(10.0))
                    .stroke(Stroke::new(1.0, col))
                    .show(ui, |ui| {
                        ui.horizontal(|ui| {
                            let (r, _) = ui.allocate_exact_size(Vec2::splat(12.0), egui::Sense::hover());
                            ui.painter().circle_filled(r.center(), 6.0, col);
                            ui.label(RichText::new(tag).color(col).strong());
                            ui.label(RichText::new(&self.status.line).color(PARCH));
                        });
                        if let Some((lo, hi)) = self.status.range {
                            ui.label(
                                RichText::new(format!("Currently forcing rolls {lo}-{hi}."))
                                    .color(GREEN)
                                    .size(12.0),
                            );
                        }
                    });

                ui.add_space(10.0);

                // --- Step 1: game --------------------------------------------
                card(ui, |ui| {
                    step_label(ui, 1, "Find the game");
                    ui.add(
                        egui::TextEdit::singleline(&mut self.exe)
                            .hint_text("path to WIZARD_720.EXE")
                            .desired_width(f32::INFINITY),
                    );
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        if ui.button("Auto-detect").clicked() {
                            if let Some(p) = find_exe() {
                                self.exe = p.to_string_lossy().into_owned();
                                self.refresh();
                                self.log = "Detected game.".into();
                            } else {
                                self.log = "Could not auto-detect - paste the path.".into();
                            }
                        }
                        if ui.button("Refresh status").clicked() {
                            self.refresh();
                        }
                    });
                });

                ui.add_space(10.0);

                // --- Step 2: range -------------------------------------------
                card(ui, |ui| {
                    step_label(ui, 2, "Choose the roll range");
                    draw_range_bar(ui, self.lo, self.hi);
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("min").color(INK));
                        ui.add(egui::DragValue::new(&mut self.lo).clamp_range(0..=40));
                        ui.add_space(8.0);
                        ui.label(RichText::new("max").color(INK));
                        ui.add(egui::DragValue::new(&mut self.hi).clamp_range(0..=40));
                        ui.add_space(12.0);
                        let (a, b) = if self.lo <= self.hi {
                            (self.lo, self.hi)
                        } else {
                            (self.hi, self.lo)
                        };
                        ui.label(
                            RichText::new(format!("rolls {a}-{b}"))
                                .color(GOLD)
                                .size(15.0)
                                .strong(),
                        );
                    });
                    ui.add_space(6.0);
                    ui.horizontal_wrapped(|ui| {
                        if ui.button("Faery Ninja (21-30)").clicked() {
                            self.lo = 21;
                            self.hi = 30;
                        }
                        if ui.button("Always 30").clicked() {
                            self.lo = 30;
                            self.hi = 30;
                        }
                        if ui.button("Generous (15-30)").clicked() {
                            self.lo = 15;
                            self.hi = 30;
                        }
                    });
                });

                ui.add_space(10.0);

                // --- Step 3: apply -------------------------------------------
                card(ui, |ui| {
                    step_label(ui, 3, "Patch  (close the game first)");
                    ui.horizontal(|ui| {
                        let can = !self.exe.is_empty();
                        let apply_btn = egui::Button::new(
                            RichText::new("Apply patch").color(BG).strong(),
                        )
                        .fill(GOLD)
                        .min_size(Vec2::new(140.0, 30.0));
                        if ui.add_enabled(can, apply_btn).clicked() {
                            self.log = apply_all(&self.exe, self.lo, self.hi);
                            self.refresh();
                        }
                        let revert_btn = egui::Button::new(RichText::new("Revert").color(PARCH))
                            .min_size(Vec2::new(100.0, 30.0));
                        if ui.add_enabled(can, revert_btn).clicked() {
                            self.log = revert_all(&self.exe);
                            self.refresh();
                        }
                    });
                });

                ui.add_space(10.0);

                // --- Step 4: game speed --------------------------------------
                card(ui, |ui| {
                    step_label(ui, 4, "Game speed  (independent of rolls)");

                    // Current on-disk state readout.
                    let cur = if !self.speed.found {
                        RichText::new("speed signature not found in this build").color(RED)
                    } else {
                        let fac = self.speed.factor.map(|f| format!("{f}x")).unwrap_or_else(|| "?x".into());
                        let tier = |ms: Option<u16>, done: usize| match ms {
                            Some(m) => format!("{m} ({done})"),
                            None => format!("{}", STOCK_TEXT_MS),
                        };
                        let txt = format!("main {} / quick {}",
                            tier(self.speed.text_full_ms, self.speed.text_full_done),
                            tier(self.speed.text_half_ms, self.speed.text_half_done));
                        let search = if self.speed.search_ms == STOCK_SEARCH_MS {
                            format!("{} (stock)", self.speed.search_ms)
                        } else {
                            self.speed.search_ms.to_string()
                        };
                        let any = self.speed.factor != Some(1)
                            || self.speed.search_ms != STOCK_SEARCH_MS
                            || self.speed.text_full_ms.is_some()
                            || self.speed.text_half_ms.is_some();
                        let col = if any { GREEN } else { INK };
                        RichText::new(format!(
                            "now:  speed {fac}   ·   search {search} ms/frame   ·   text {txt} ms"
                        )).color(col)
                    };
                    ui.label(cur);
                    ui.add_space(8.0);

                    // Global speed factor.
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Game speed").color(INK));
                        for (f, _) in FACTORS {
                            ui.selectable_value(&mut self.sp_factor, *f, format!("{f}x"));
                        }
                    });
                    ui.label(RichText::new("movement, animation, combat pacing").color(INK).size(11.0));
                    ui.add_space(6.0);

                    // Text dwell - two tiers (the game's design: main + quick lines).
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Text main ").color(INK));
                        ui.add(egui::DragValue::new(&mut self.sp_text_full).clamp_range(50..=5000).suffix(" ms"));
                        ui.add_space(6.0);
                        ui.label(RichText::new("quick").color(INK));
                        ui.add(egui::DragValue::new(&mut self.sp_text_half).clamp_range(50..=5000).suffix(" ms"));
                    });
                    ui.label(RichText::new("two tiers: main lines / quick lines. Enter skips a line.").color(INK).size(11.0));
                    ui.horizontal_wrapped(|ui| {
                        if ui.button("Uniform 1000").clicked() { self.sp_text_full = 1000; self.sp_text_half = 1000; }
                        if ui.button("Split 1000/500").clicked() { self.sp_text_full = 1000; self.sp_text_half = 500; }
                        if ui.button("Stock 50/50").clicked() { self.sp_text_full = 50; self.sp_text_half = 50; }
                    });
                    ui.add_space(6.0);

                    // Search-wipe delay.
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Search wipe").color(INK));
                        ui.add(egui::DragValue::new(&mut self.sp_search).clamp_range(0..=127).suffix(" ms/frame"));
                        ui.label(RichText::new("0 = instant").color(INK).size(11.0));
                    });
                    ui.add_space(8.0);

                    ui.horizontal(|ui| {
                        let can = !self.exe.is_empty();
                        let apply_btn = egui::Button::new(RichText::new("Apply speed").color(BG).strong())
                            .fill(GOLD).min_size(Vec2::new(130.0, 28.0));
                        if ui.add_enabled(can, apply_btn).clicked() {
                            let (f, s) = (self.sp_factor, self.sp_search as u8);
                            let (tf, th) = (self.sp_text_full as u16, self.sp_text_half as u16);
                            self.log = speed_all(&self.exe, |p| {
                                let a = speed_set_factor(p, f);
                                let b = speed_set_search(p, s);
                                let c = speed_set_text(p, Some(tf), Some(th));
                                format!("{a}  {b}  {c}")
                            });
                            self.refresh();
                        }
                        let rev = egui::Button::new(RichText::new("Revert speed").color(PARCH))
                            .min_size(Vec2::new(110.0, 28.0));
                        if ui.add_enabled(can, rev).clicked() {
                            self.log = speed_all(&self.exe, speed_revert);
                            self.refresh();
                            self.sp_factor = 1;
                            self.sp_search = STOCK_SEARCH_MS as i32;
                            self.sp_text_full = STOCK_TEXT_MS as i32;
                            self.sp_text_half = STOCK_TEXT_MS as i32;
                        }
                    });
                });

                if !self.log.is_empty() {
                    ui.add_space(10.0);
                    egui::Frame::none()
                        .fill(PANEL_HI)
                        .rounding(Rounding::same(6.0))
                        .inner_margin(egui::Margin::same(8.0))
                        .show(ui, |ui| {
                            ui.label(RichText::new(&self.log).color(PARCH));
                        });
                }

                ui.add_space(8.0);
                ui.label(
                    RichText::new(
                        "Backs up to <exe>.bak before the first patch. Fully reversible.",
                    )
                    .color(INK)
                    .size(11.0),
                );
                }); // ScrollArea
            });
    }
}

/// Apply the dark/gold Wizardry theme to egui widgets.
fn theme(ctx: &egui::Context) {
    let mut v = egui::Visuals::dark();
    v.override_text_color = Some(PARCH);
    v.widgets.noninteractive.bg_fill = PANEL;
    v.widgets.inactive.bg_fill = PANEL_HI;
    v.widgets.inactive.weak_bg_fill = PANEL_HI;
    v.widgets.hovered.bg_fill = GOLD_DIM;
    v.widgets.hovered.weak_bg_fill = GOLD_DIM;
    v.widgets.active.bg_fill = GOLD;
    v.widgets.inactive.fg_stroke = Stroke::new(1.0, PARCH);
    v.widgets.hovered.fg_stroke = Stroke::new(1.0, BG);
    v.widgets.inactive.rounding = Rounding::same(6.0);
    v.widgets.hovered.rounding = Rounding::same(6.0);
    v.widgets.active.rounding = Rounding::same(6.0);
    v.selection.bg_fill = GOLD_DIM;
    ctx.set_visuals(v);
}

#[cfg(test)]
mod tests {
    use super::*;
    const PRISTINE: &str = r"D:/Wizardry 7 Gold/WIZARD_720.EXE.bak";

    #[test]
    fn speed_roundtrip_matches_pristine() {
        let pristine = fs::read(PRISTINE).expect("pristine exe present");
        let tmp = std::env::temp_dir().join("wz_gui_test.exe");
        let tp = tmp.to_string_lossy().to_string();
        fs::write(&tp, &pristine).unwrap();

        // Stock state.
        let s0 = speed_describe(&fs::read(&tp).unwrap());
        assert_eq!(s0.factor, Some(1));
        assert_eq!(s0.search_ms, STOCK_SEARCH_MS);
        assert_eq!(s0.text_full_ms, None);
        assert_eq!(s0.text_half_ms, None);

        // Apply 4x + search 8 + split text (main 1500, quick 700).
        speed_set_factor(&tp, 4);
        speed_set_search(&tp, 8);
        speed_set_text(&tp, Some(1500), Some(700));
        let s1 = speed_describe(&fs::read(&tp).unwrap());
        assert_eq!(s1.factor, Some(4));
        assert_eq!(s1.search_ms, 8);
        assert_eq!(s1.text_full_ms, Some(1500));
        assert_eq!(s1.text_half_ms, Some(700));
        assert_eq!(s1.text_full_done, 27);
        assert_eq!(s1.text_half_done, 19);

        // Revert -> byte-identical to pristine.
        speed_revert(&tp);
        assert_eq!(fs::read(&tp).unwrap(), pristine, "revert must match pristine");
        let _ = fs::remove_file(&tp);
    }
}

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([520.0, 740.0])
            .with_icon(gen_icon()),
        ..Default::default()
    };
    eframe::run_native(
        "Wizardry Gold - High Roll Patcher",
        options,
        Box::new(|cc| {
            theme(&cc.egui_ctx);
            Box::new(App::default())
        }),
    )
}
