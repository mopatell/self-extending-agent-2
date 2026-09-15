//! The desktop shell does one thing besides showing the UI: it starts the Python backend
//! (`sea serve`) when the window opens and stops it when the window closes.

use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};

const PORT: &str = "8765";

struct Backend(Mutex<Option<Child>>);

/// The repo root: `SEA_ROOT` if set, else two levels above `src-tauri` (dev layout).
fn repo_root() -> PathBuf {
    std::env::var("SEA_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
}

fn already_running() -> bool {
    TcpStream::connect_timeout(&format!("127.0.0.1:{PORT}").parse().unwrap(), Duration::from_millis(200)).is_ok()
}

/// Prefer the venv's console script (a single process we can kill cleanly); fall back to `uv run`.
fn spawn_backend() -> Option<Child> {
    let root = repo_root();
    let me = std::process::id().to_string();
    let args = ["serve", "--port", PORT, "--watch-parent", &me];
    let venv_bin = root.join(".venv/bin/sea");
    let mut cmd = if venv_bin.exists() {
        let mut c = Command::new(venv_bin);
        c.args(args);
        c
    } else {
        let mut c = Command::new("uv");
        c.arg("run").arg("sea").args(args);
        c
    };
    cmd.current_dir(&root).stdin(Stdio::null()).stdout(Stdio::inherit()).stderr(Stdio::inherit());
    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("sea-desktop: could not start backend from {}: {e}", root.display());
            None
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let child = if already_running() { None } else { spawn_backend() };
            app.manage(Backend(Mutex::new(child)));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut child) = app.state::<Backend>().0.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}
