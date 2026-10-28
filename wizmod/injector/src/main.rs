//! Inject wizmod.dll into the running Wizardry Gold process.
//!
//! Usage:
//!   wizmod-inject [path\to\wizmod.dll] [process-name-substring]
//! Defaults: dll = "wizmod.dll" next to this exe; process name contains "wizard".
//! Run while the game is already running.

use std::env;
use std::ffi::CString;
use std::mem::{size_of, transmute};
use std::path::PathBuf;

use windows::core::{PCSTR, PCWSTR};
use windows::Win32::Foundation::{CloseHandle, HANDLE, HMODULE};
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
    TH32CS_SNAPPROCESS,
};
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Memory::{
    VirtualAllocEx, MEM_COMMIT, MEM_RESERVE, PAGE_READWRITE,
};
use windows::Win32::System::Threading::{
    CreateRemoteThread, OpenProcess, WaitForSingleObject, LPTHREAD_START_ROUTINE,
    PROCESS_ALL_ACCESS,
};

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Find the first process whose exe name contains `needle` (case-insensitive).
fn find_pid(needle: &str) -> Option<u32> {
    let needle = needle.to_lowercase();
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0).ok()?;
        let mut entry = PROCESSENTRY32W {
            dwSize: size_of::<PROCESSENTRY32W>() as u32,
            ..Default::default()
        };
        let mut found = None;
        if Process32FirstW(snap, &mut entry).is_ok() {
            loop {
                let len = entry.szExeFile.iter().position(|&c| c == 0).unwrap_or(0);
                let name = String::from_utf16_lossy(&entry.szExeFile[..len]).to_lowercase();
                if name.contains(&needle) {
                    found = Some(entry.th32ProcessID);
                    break;
                }
                if Process32NextW(snap, &mut entry).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snap);
        found
    }
}

fn inject(pid: u32, dll_path: &str) -> Result<(), String> {
    let path_c = CString::new(dll_path).map_err(|e| e.to_string())?;
    let bytes = path_c.as_bytes_with_nul();
    unsafe {
        let hproc: HANDLE =
            OpenProcess(PROCESS_ALL_ACCESS, false, pid).map_err(|e| format!("OpenProcess: {e}"))?;

        // Allocate remote memory for the DLL path string.
        let remote = VirtualAllocEx(
            hproc,
            None,
            bytes.len(),
            MEM_COMMIT | MEM_RESERVE,
            PAGE_READWRITE,
        );
        if remote.is_null() {
            let _ = CloseHandle(hproc);
            return Err("VirtualAllocEx failed".into());
        }

        // Write the path.
        let mut written = 0usize;
        windows::Win32::System::Diagnostics::Debug::WriteProcessMemory(
            hproc,
            remote,
            bytes.as_ptr() as *const _,
            bytes.len(),
            Some(&mut written),
        )
        .map_err(|e| format!("WriteProcessMemory: {e}"))?;

        // Resolve LoadLibraryA in kernel32 (same address in the target — system DLL).
        let kernel32: HMODULE =
            GetModuleHandleW(PCWSTR(wide("kernel32.dll").as_ptr())).map_err(|e| e.to_string())?;
        let load_library = GetProcAddress(kernel32, PCSTR(b"LoadLibraryA\0".as_ptr()))
            .ok_or("GetProcAddress(LoadLibraryA) failed")?;
        let start: LPTHREAD_START_ROUTINE = Some(transmute(load_library));

        // Run LoadLibraryA(remote_path) in the target.
        let thread = CreateRemoteThread(hproc, None, 0, start, Some(remote), 0, None)
            .map_err(|e| format!("CreateRemoteThread: {e}"))?;
        WaitForSingleObject(thread, 5000);

        let _ = CloseHandle(thread);
        let _ = CloseHandle(hproc);
    }
    Ok(())
}

fn default_dll_path() -> PathBuf {
    // Prefer wizmod.dll next to this injector exe.
    if let Ok(exe) = env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("wizmod.dll");
            if p.is_file() {
                return p;
            }
        }
    }
    PathBuf::from("wizmod.dll")
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let dll_path = args
        .get(1)
        .map(PathBuf::from)
        .unwrap_or_else(default_dll_path);
    let needle = args.get(2).cloned().unwrap_or_else(|| "wizard".into());

    let dll_abs = dll_path
        .canonicalize()
        .unwrap_or(dll_path.clone())
        .to_string_lossy()
        .trim_start_matches(r"\\?\")
        .to_string();
    if !PathBuf::from(&dll_abs).is_file() {
        eprintln!("DLL not found: {dll_abs}");
        std::process::exit(1);
    }

    match find_pid(&needle) {
        Some(pid) => {
            println!("Found game process (pid {pid}). Injecting {dll_abs} ...");
            match inject(pid, &dll_abs) {
                Ok(()) => println!("Injected. Check wizmod_debug.log."),
                Err(e) => {
                    eprintln!("Injection failed: {e}");
                    std::process::exit(1);
                }
            }
        }
        None => {
            eprintln!("No process matching '{needle}' is running. Start the game first.");
            std::process::exit(1);
        }
    }
}
