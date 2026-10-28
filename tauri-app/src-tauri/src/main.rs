// Wizardry Gold Patcher - Tauri v2 backend.
// Thin command layer over patch.rs; the UI lives in ../ui (vanilla HTML/JS).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod patch;

#[tauri::command]
fn detect() -> Option<String> {
    patch::find_exe().map(|p| p.to_string_lossy().into_owned())
}

#[tauri::command]
fn status(exe: String) -> Result<patch::FullStatus, String> {
    patch::full_status(&exe)
}

#[tauri::command]
fn apply_roll(exe: String, lo: i32, hi: i32) -> String {
    patch::apply_roll_all(&exe, lo, hi)
}

#[tauri::command]
fn revert_roll(exe: String) -> String {
    patch::revert_roll_all(&exe)
}

#[tauri::command]
fn apply_speed(exe: String, factor: u32, search: u8, text_full: u16, text_half: u16) -> String {
    patch::apply_speed_all(&exe, factor, search, text_full, text_half)
}

#[tauri::command]
fn revert_speed(exe: String) -> String {
    patch::revert_speed_all(&exe)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            detect, status, apply_roll, revert_roll, apply_speed, revert_speed
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
