//! Start and stop the local engine from the app.
//!
//! The engine is the checkout's Python service. Until now the app could only
//! print the command that starts it. This keeps one child process, the way
//! Ollama's desktop app keeps `ollama serve` (ollama/ollama
//! `app/lifecycle/server.go`: spawn the server, send its output to a log
//! file, stop it when the app quits). The program is the checkout's own
//! interpreter running `-m silkscreen.serve`, resolved by `cli.rs`, never a
//! shell and never a string the user typed.

use serde::Serialize;
use std::fs::{self, OpenOptions};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use crate::cli;

#[derive(Default)]
pub struct EngineProcess(Mutex<Option<(Child, u16)>>);

#[derive(Debug, Serialize)]
pub struct EngineProcessStatus {
    pub running: bool,
    pub pid: Option<u32>,
    pub port: Option<u16>,
    pub log: String,
}

fn log_path() -> PathBuf {
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    home.join(".kaleo").join("engine.log")
}

fn status_of(slot: &mut Option<(Child, u16)>) -> EngineProcessStatus {
    // A child that exited on its own (a port clash, a missing package) is
    // reported as stopped, and its log says why.
    if let Some((child, _)) = slot.as_mut() {
        if matches!(child.try_wait(), Ok(Some(_))) {
            *slot = None;
        }
    }
    EngineProcessStatus {
        running: slot.is_some(),
        pid: slot.as_ref().map(|(c, _)| c.id()),
        port: slot.as_ref().map(|(_, p)| *p),
        log: log_path().display().to_string(),
    }
}

#[tauri::command]
pub fn engine_status(state: tauri::State<'_, EngineProcess>) -> EngineProcessStatus {
    let mut slot = state.0.lock().unwrap_or_else(|e| e.into_inner());
    status_of(&mut slot)
}

#[tauri::command]
pub fn engine_start(
    state: tauri::State<'_, EngineProcess>,
    port: u16,
) -> Result<EngineProcessStatus, String> {
    let mut slot = state.0.lock().unwrap_or_else(|e| e.into_inner());
    if status_of(&mut slot).running {
        return Ok(status_of(&mut slot));
    }
    let root = cli::repository_root()?;
    let python = cli::python_interpreter(&root)?;
    let log = log_path();
    if let Some(dir) = log.parent() {
        fs::create_dir_all(dir).map_err(|e| format!("could not create {}: {e}", dir.display()))?;
    }
    let out = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .map_err(|e| format!("could not open {}: {e}", log.display()))?;
    let err = out.try_clone().map_err(|e| e.to_string())?;
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "silkscreen.serve", "--no-browser", "--port"])
        .arg(port.to_string())
        .current_dir(&root)
        .stdin(Stdio::null())
        .stdout(Stdio::from(out))
        .stderr(Stdio::from(err));
    cli::load_dotenv_into(&mut cmd, &root);
    let child = cmd
        .spawn()
        .map_err(|e| format!("could not start the engine: {e}"))?;
    *slot = Some((child, port));
    Ok(status_of(&mut slot))
}

#[tauri::command]
pub fn engine_stop(state: tauri::State<'_, EngineProcess>) -> EngineProcessStatus {
    let mut slot = state.0.lock().unwrap_or_else(|e| e.into_inner());
    stop(&mut slot);
    status_of(&mut slot)
}

fn stop(slot: &mut Option<(Child, u16)>) {
    if let Some((mut child, _)) = slot.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// Called when the app exits: an engine Ada started does not outlive it.
pub fn stop_on_exit(state: &EngineProcess) {
    let mut slot = state.0.lock().unwrap_or_else(|e| e.into_inner());
    stop(&mut slot);
}
