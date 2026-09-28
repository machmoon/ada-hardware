//! The outside tools Ada drives -- KiCad, FreeCAD, ngspice -- found on this
//! machine, or downloaded and installed from the Setup Assistant's Tools step.
//!
//! Three designs are copied rather than invented:
//!
//! * **Progress over a [`Channel`], not `app.emit`.** Tauri's own guide uses a
//!   download as *the* Channel example (`tauri-docs`,
//!   `src/content/docs/develop/calling-frontend.mdx`: `DownloadEvent` with
//!   `Started { content_length }`, `Progress { chunk_length }`, `Finished`,
//!   tagged `event`/`data`, camelCase). `pty.rs` already streams this way. The
//!   event names below extend that enum with the phases a download-and-install
//!   has after the last byte: `Verifying`, `Installing`, `Log` and `Failed`.
//! * **Stream to a partial file, check, then rename.** Jan's download manager
//!   (`janhq/jan`, `src-tauri/src/core/downloads/helpers.rs` and
//!   `commands.rs` on `dev`) writes to `<name>.tmp`, validates size then
//!   SHA-256, cancels through a token, and deletes the partial on a real
//!   cancel. Jan's licence is not a standard SPDX one, so this is its design
//!   written fresh, not its code.
//! * **Install a `.dmg` the way Homebrew Cask does.**
//!   (`Homebrew/brew`, `Library/Homebrew/unpack_strategy/dmg.rb`): `hdiutil
//!   attach -plist -nobrowse -readonly -mountrandom <dir>` with `qn` on stdin,
//!   so an image carrying a licence agreement *fails* to mount instead of
//!   being agreed to on the user's behalf; copy with `ditto`; detach. An image
//!   with a licence is handed to Finder so a person reads and accepts it.
//!
//! ngspice publishes no macOS build, so it installs through Homebrew
//! (`brew install ngspice`) with brew's own output as the progress, and
//! refuses in words when Homebrew itself is missing.
//!
//! Nothing here runs with elevated privileges. `/Applications` is writable by
//! an admin account without sudo; when it is not, the copy lands in
//! `~/Applications` and the tool is still found there.

use futures_util::StreamExt;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use tauri::ipc::Channel;

/// How often a `Progress` event may be sent. A 1.4 GB download arrives in
/// ~64 KiB chunks; one IPC message per chunk would be ~20 000 messages.
const PROGRESS_EVERY: Duration = Duration::from_millis(120);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Method {
    /// Mount, copy `item` out of the image into the Applications folder.
    Dmg { item: &'static str },
    /// `brew install <formula>`.
    Brew { formula: &'static str },
}

#[derive(Clone, Copy, Debug)]
struct Download {
    url: &'static str,
    /// Exact byte count published by the host, checked after the last byte.
    size: u64,
    /// A published `<file>-SHA256.txt` beside the file, when the project has one.
    sha256_url: Option<&'static str>,
}

#[derive(Clone, Copy, Debug)]
struct ToolSpec {
    id: &'static str,
    name: &'static str,
    version: &'static str,
    /// What Ada stops being able to do without it, in one sentence.
    purpose: &'static str,
    method: Method,
    download: Option<Download>,
}

// Pinned releases, each checked against its host on 2026-09-27:
// KiCad's S3 listing (`kicad-downloads.s3.cern.ch/?prefix=osx/stable/`, the
// bucket downloads.kicad.org serves) and FreeCAD's GitHub release 1.1.3.
// KiCad 10.0.6 is the version the engine's ERC/DRC expectations were
// measured on (CLAUDE.md, "Verifying against real KiCad").
const KICAD: ToolSpec = ToolSpec {
    id: "kicad",
    name: "KiCad",
    version: "10.0.6",
    purpose: "Opens every stage Ada draws, and runs the ERC, DRC and 3D export the order step ships.",
    method: Method::Dmg { item: "KiCad" },
    download: Some(Download {
        url: "https://kicad-downloads.s3.cern.ch/osx/stable/kicad-unified-universal-10.0.6.dmg",
        size: 1_404_303_659,
        sha256_url: None,
    }),
};

#[cfg(target_arch = "aarch64")]
const FREECAD_DOWNLOAD: Download = Download {
    url: "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-macOS-arm64-py311.dmg",
    size: 649_856_006,
    sha256_url: Some(
        "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-macOS-arm64-py311.dmg-SHA256.txt",
    ),
};
#[cfg(not(target_arch = "aarch64"))]
const FREECAD_DOWNLOAD: Download = Download {
    url: "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-macOS-x86_64-py311.dmg",
    size: 719_583_151,
    sha256_url: Some(
        "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-macOS-x86_64-py311.dmg-SHA256.txt",
    ),
};

const FREECAD: ToolSpec = ToolSpec {
    id: "freecad",
    name: "FreeCAD",
    version: "1.1.3",
    purpose: "Checks the printed case against the assembled board, the second opinion on the kernel's clauses.",
    method: Method::Dmg { item: "FreeCAD.app" },
    download: Some(FREECAD_DOWNLOAD),
};

const NGSPICE: ToolSpec = ToolSpec {
    id: "ngspice",
    name: "ngspice",
    version: "Homebrew",
    purpose: "Simulates the circuit, so a spec clause gets a pass or fail with a margin instead of a guess.",
    method: Method::Brew { formula: "ngspice" },
    download: None,
};

const TOOLS: [ToolSpec; 3] = [KICAD, FREECAD, NGSPICE];

fn spec(id: &str) -> Result<ToolSpec, String> {
    TOOLS
        .iter()
        .copied()
        .find(|t| t.id == id)
        .ok_or_else(|| format!("unknown tool '{id}'; known: kicad, freecad, ngspice"))
}

#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct ToolStatus {
    id: String,
    name: String,
    version: String,
    purpose: String,
    installed: bool,
    /// Where it was found, or why it cannot be installed from here.
    detail: String,
    /// Bytes to download, or null when the size is not known up front (brew).
    download_bytes: Option<u64>,
    /// False when this machine cannot install it from the app at all.
    installable: bool,
}

fn home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

fn app_dirs() -> Vec<PathBuf> {
    let mut dirs = vec![PathBuf::from("/Applications")];
    if let Some(h) = home() {
        dirs.push(h.join("Applications"));
    }
    dirs
}

fn brew_path() -> Option<PathBuf> {
    ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
        .iter()
        .map(PathBuf::from)
        .find(|p| p.is_file())
        .or_else(|| crate::cli::which("brew"))
}

fn find_installed(spec: &ToolSpec) -> Option<PathBuf> {
    match spec.id {
        "kicad" => crate::cli::kicad_cli_path().or_else(|| {
            app_dirs()
                .into_iter()
                .map(|d| d.join("KiCad/KiCad.app/Contents/MacOS/kicad-cli"))
                .find(|p| p.is_file())
        }),
        "freecad" => crate::cli::which("freecadcmd").or_else(|| {
            app_dirs()
                .into_iter()
                .map(|d| d.join("FreeCAD.app"))
                .find(|p| p.is_dir())
        }),
        "ngspice" => crate::cli::which("ngspice").or_else(|| {
            ["/opt/homebrew/bin/ngspice", "/usr/local/bin/ngspice"]
                .iter()
                .map(PathBuf::from)
                .find(|p| p.is_file())
        }),
        _ => None,
    }
}

fn status_of(spec: &ToolSpec) -> ToolStatus {
    let found = find_installed(spec);
    let (installable, detail) = match (&found, spec.method) {
        (Some(p), _) => (true, p.display().to_string()),
        (None, _) if !cfg!(target_os = "macos") => (
            false,
            "Installing from Ada is macOS only; use your package manager.".to_string(),
        ),
        (None, Method::Brew { formula }) => match brew_path() {
            Some(_) => (true, format!("Installs with `brew install {formula}`.")),
            None => (
                false,
                format!("{} has no macOS download; it needs Homebrew (brew.sh), then `brew install {formula}`.", spec.name),
            ),
        },
        (None, Method::Dmg { .. }) => (true, "Not on this Mac.".to_string()),
    };
    ToolStatus {
        id: spec.id.into(),
        name: spec.name.into(),
        version: spec.version.into(),
        purpose: spec.purpose.into(),
        installed: found.is_some(),
        detail,
        download_bytes: spec.download.map(|d| d.size),
        installable,
    }
}

#[tauri::command]
pub fn tools_status() -> Vec<ToolStatus> {
    TOOLS.iter().map(status_of).collect()
}

#[derive(Clone, Serialize)]
#[serde(
    rename_all = "camelCase",
    rename_all_fields = "camelCase",
    tag = "event",
    content = "data"
)]
pub enum ToolEvent {
    /// `content_length` is 0 when unknown (brew), which the bar shows as
    /// indeterminate rather than as 0 %.
    Started { id: String, content_length: u64 },
    /// Cumulative bytes, not a chunk: a dropped message cannot skew the bar.
    Progress { id: String, transferred: u64, content_length: u64 },
    Verifying { id: String },
    Installing { id: String },
    /// One line of an installer's own output (brew), for the row's caption.
    Log { id: String, line: String },
    Finished { id: String, path: String },
    Failed { id: String, message: String, cancelled: bool },
}

fn cancels() -> &'static Mutex<HashMap<String, Arc<AtomicBool>>> {
    static CANCELS: OnceLock<Mutex<HashMap<String, Arc<AtomicBool>>>> = OnceLock::new();
    CANCELS.get_or_init(Default::default)
}

#[tauri::command]
pub fn tools_cancel(id: String) -> Result<(), String> {
    match cancels().lock().map_err(|e| e.to_string())?.get(&id) {
        Some(flag) => {
            flag.store(true, Ordering::SeqCst);
            Ok(())
        }
        None => Err(format!("{id} is not downloading")),
    }
}

/// Download (when there is a file), verify, and install one tool, reporting
/// every phase on `on_event`. The command's own result mirrors the last
/// event, so a caller that ignores the channel still learns the outcome.
#[tauri::command]
pub async fn tools_install(id: String, on_event: Channel<ToolEvent>) -> Result<String, String> {
    let spec = spec(&id)?;
    let flag = Arc::new(AtomicBool::new(false));
    {
        let mut map = cancels().lock().map_err(|e| e.to_string())?;
        if map.contains_key(&id) {
            return Err(format!("{} is already being installed", spec.name));
        }
        map.insert(id.clone(), flag.clone());
    }
    let result = install(&spec, &on_event, &flag).await;
    cancels().lock().map_err(|e| e.to_string())?.remove(&id);
    match result {
        Ok(path) => {
            let _ = on_event.send(ToolEvent::Finished { id, path: path.clone() });
            Ok(path)
        }
        Err(message) => {
            let cancelled = flag.load(Ordering::SeqCst);
            let _ = on_event.send(ToolEvent::Failed { id, message: message.clone(), cancelled });
            Err(message)
        }
    }
}

async fn install(spec: &ToolSpec, tx: &Channel<ToolEvent>, cancel: &Arc<AtomicBool>) -> Result<String, String> {
    if let Some(found) = find_installed(spec) {
        return Ok(found.display().to_string());
    }
    if !cfg!(target_os = "macos") {
        return Err("Installing from Ada is macOS only; use your package manager.".into());
    }
    match (spec.method, spec.download) {
        (Method::Dmg { item }, Some(download)) => {
            let image = fetch(spec, download, tx, cancel).await?;
            let _ = tx.send(ToolEvent::Installing { id: spec.id.into() });
            let installed = tokio::task::spawn_blocking(move || install_dmg(&image, item))
                .await
                .map_err(|e| e.to_string())??;
            find_installed(spec)
                .map(|p| p.display().to_string())
                .ok_or_else(|| format!("{} was copied to {installed} but Ada still cannot find it", spec.name))
        }
        (Method::Brew { formula }, _) => {
            let id = spec.id.to_string();
            let _ = tx.send(ToolEvent::Started { id: id.clone(), content_length: 0 });
            let _ = tx.send(ToolEvent::Installing { id: id.clone() });
            let brew = brew_path().ok_or_else(|| {
                format!("{} needs Homebrew (brew.sh) first, then `brew install {formula}`.", spec.name)
            })?;
            let tx = tx.clone();
            let cancelled = cancel.clone();
            tokio::task::spawn_blocking(move || brew_install(&brew, formula, &id, &tx, &cancelled))
                .await
                .map_err(|e| e.to_string())??;
            find_installed(spec)
                .map(|p| p.display().to_string())
                .ok_or_else(|| format!("brew finished but `{formula}` is not on PATH"))
        }
        (Method::Dmg { .. }, None) => Err(format!("{} has no download configured", spec.name)),
    }
}

fn downloads_dir() -> Result<PathBuf, String> {
    let dir = home()
        .ok_or("HOME is not set")?
        .join("Library/Caches/Ada/downloads");
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    Ok(dir)
}

fn file_name(url: &str) -> &str {
    url.rsplit('/').next().unwrap_or("download")
}

/// Free bytes on the volume holding `dir`, from `df -k` (no extra crate).
fn free_bytes(dir: &Path) -> Option<u64> {
    let out = Command::new("df").arg("-k").arg(dir).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let line = text.lines().nth(1)?;
    line.split_whitespace().nth(3)?.parse::<u64>().ok().map(|kb| kb * 1024)
}

async fn fetch(
    spec: &ToolSpec,
    download: Download,
    tx: &Channel<ToolEvent>,
    cancel: &AtomicBool,
) -> Result<PathBuf, String> {
    let dir = downloads_dir()?;
    let final_path = dir.join(file_name(download.url));
    let part = dir.join(format!("{}.part", file_name(download.url)));

    // A finished image from an earlier attempt is reused only if its size is
    // exactly right; anything else is downloaded again.
    if std::fs::metadata(&final_path).map(|m| m.len() == download.size).unwrap_or(false) {
        let _ = tx.send(ToolEvent::Started { id: spec.id.into(), content_length: download.size });
        let _ = tx.send(ToolEvent::Progress {
            id: spec.id.into(),
            transferred: download.size,
            content_length: download.size,
        });
        return Ok(final_path);
    }

    // The image and the installed copy are both on disk for a moment.
    if let Some(free) = free_bytes(&dir) {
        let need = download.size * 3;
        if free < need {
            return Err(format!(
                "Not enough disk space: {} needs about {:.1} GB free (image plus install), and this Mac has {:.1} GB.",
                spec.name,
                need as f64 / 1e9,
                free as f64 / 1e9
            ));
        }
    }

    let client = reqwest::Client::builder()
        .user_agent(concat!("Ada/", env!("CARGO_PKG_VERSION")))
        .connect_timeout(Duration::from_secs(20))
        .build()
        .map_err(|e| e.to_string())?;
    let response = client
        .get(download.url)
        .send()
        .await
        .and_then(|r| r.error_for_status())
        .map_err(|e| format!("Could not start the {} download: {e}", spec.name))?;
    let total = response.content_length().unwrap_or(download.size);
    let _ = tx.send(ToolEvent::Started { id: spec.id.into(), content_length: total });

    let mut file = std::fs::File::create(&part).map_err(|e| format!("cannot write {}: {e}", part.display()))?;
    let mut hasher = Sha256::new();
    let mut transferred: u64 = 0;
    let mut last = Instant::now();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        if cancel.load(Ordering::SeqCst) {
            drop(file);
            let _ = std::fs::remove_file(&part);
            return Err("Cancelled.".into());
        }
        let chunk = chunk.map_err(|e| format!("The {} download stopped: {e}", spec.name))?;
        file.write_all(&chunk).map_err(|e| format!("cannot write {}: {e}", part.display()))?;
        hasher.update(&chunk);
        transferred += chunk.len() as u64;
        if last.elapsed() >= PROGRESS_EVERY {
            last = Instant::now();
            let _ = tx.send(ToolEvent::Progress { id: spec.id.into(), transferred, content_length: total });
        }
    }
    file.flush().map_err(|e| e.to_string())?;
    drop(file);
    let _ = tx.send(ToolEvent::Progress { id: spec.id.into(), transferred, content_length: total });

    let _ = tx.send(ToolEvent::Verifying { id: spec.id.into() });
    let fail = |why: String| {
        let _ = std::fs::remove_file(&part);
        Err(why)
    };
    if transferred != download.size {
        return fail(format!(
            "The {} download is the wrong size ({transferred} bytes, expected {}); it was deleted.",
            spec.name, download.size
        ));
    }
    if let Some(sum_url) = download.sha256_url {
        let published = client
            .get(sum_url)
            .send()
            .await
            .and_then(|r| r.error_for_status())
            .map_err(|e| e.to_string());
        let published = match published {
            Ok(r) => r.text().await.unwrap_or_default(),
            Err(e) => return fail(format!("Could not read {}'s published checksum: {e}", spec.name)),
        };
        let expected = published.split_whitespace().next().unwrap_or("").to_ascii_lowercase();
        let actual: String = hasher.finalize().iter().map(|b| format!("{b:02x}")).collect();
        if expected.len() != 64 || expected != actual {
            return fail(format!(
                "The {} download does not match its published SHA-256; it was deleted.",
                spec.name
            ));
        }
    }
    std::fs::rename(&part, &final_path).map_err(|e| e.to_string())?;
    Ok(final_path)
}

/// Homebrew Cask's mount-copy-detach, with its EULA rule: decline on stdin,
/// and treat a failed mount as "this image has a licence a person must read".
fn install_dmg(image: &Path, item: &str) -> Result<String, String> {
    let mount_root = std::env::temp_dir().join(format!("ada-dmg-{}", std::process::id()));
    std::fs::create_dir_all(&mount_root).map_err(|e| e.to_string())?;
    let mut attach = Command::new("hdiutil")
        .args(["attach", "-plist", "-nobrowse", "-readonly", "-mountrandom"])
        .arg(&mount_root)
        .arg(image)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("hdiutil: {e}"))?;
    if let Some(mut stdin) = attach.stdin.take() {
        let _ = stdin.write_all(b"qn\n");
    }
    let out = attach.wait_with_output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        let _ = Command::new("open").arg(image).status();
        return Err(
            "This disk image asks you to accept a licence, so Ada opened it in Finder. Accept it there and drag the app to Applications, then press Check again."
                .into(),
        );
    }
    let plist = String::from_utf8_lossy(&out.stdout);
    let mount = mount_point(&plist).ok_or("hdiutil mounted the image but named no mount point")?;
    let result = copy_out(Path::new(&mount), item);
    let detached = Command::new("hdiutil").args(["detach", "-quiet"]).arg(&mount).status();
    if !matches!(detached, Ok(s) if s.success()) {
        let _ = Command::new("hdiutil").args(["detach", "-force", "-quiet"]).arg(&mount).status();
    }
    let _ = std::fs::remove_dir(&mount_root);
    if result.is_ok() {
        let _ = std::fs::remove_file(image);
    }
    result
}

/// The first `<key>mount-point</key><string>…</string>` in hdiutil's plist.
fn mount_point(plist: &str) -> Option<String> {
    let at = plist.find("<key>mount-point</key>")?;
    let rest = &plist[at..];
    let start = rest.find("<string>")? + "<string>".len();
    let end = rest[start..].find("</string>")?;
    Some(rest[start..start + end].to_string())
}

fn copy_out(mount: &Path, item: &str) -> Result<String, String> {
    let source = mount.join(item);
    if !source.exists() {
        return Err(format!("the disk image has no {item}"));
    }
    let mut last_error = String::new();
    for dir in app_dirs() {
        if std::fs::create_dir_all(&dir).is_err() {
            continue;
        }
        let target = dir.join(item);
        if target.exists() {
            let _ = std::fs::remove_dir_all(&target);
        }
        let status = Command::new("ditto").arg(&source).arg(&target).output();
        match status {
            Ok(o) if o.status.success() => {
                // A copied app keeps the image's quarantine flag; the user
                // chose this download, as they would dragging it from Finder.
                let _ = Command::new("xattr").args(["-dr", "com.apple.quarantine"]).arg(&target).status();
                return Ok(target.display().to_string());
            }
            Ok(o) => last_error = String::from_utf8_lossy(&o.stderr).trim().to_string(),
            Err(e) => last_error = e.to_string(),
        }
    }
    Err(format!("could not copy {item} into Applications: {last_error}"))
}

fn brew_install(
    brew: &Path,
    formula: &str,
    id: &str,
    tx: &Channel<ToolEvent>,
    cancel: &AtomicBool,
) -> Result<(), String> {
    use std::io::{BufRead, BufReader};
    let mut child = Command::new(brew)
        .args(["install", formula])
        .env("HOMEBREW_NO_AUTO_UPDATE", "1")
        .env("HOMEBREW_NO_ENV_HINTS", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("brew: {e}"))?;
    let stderr = child.stderr.take();
    let err_thread = std::thread::spawn(move || {
        let mut text = String::new();
        if let Some(s) = stderr {
            for line in BufReader::new(s).lines().map_while(Result::ok) {
                text.push_str(&line);
                text.push('\n');
            }
        }
        text
    });
    if let Some(out) = child.stdout.take() {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            if cancel.load(Ordering::SeqCst) {
                let _ = child.kill();
                return Err("Cancelled.".into());
            }
            let line = line.trim().to_string();
            if !line.is_empty() {
                let _ = tx.send(ToolEvent::Log { id: id.into(), line });
            }
        }
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    let stderr = err_thread.join().unwrap_or_default();
    if status.success() {
        Ok(())
    } else {
        let last = stderr.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("no output");
        Err(format!("`brew install {formula}` failed: {last}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_mount_point_from_hdiutils_plist() {
        let plist = "<dict><key>dev-entry</key><string>/dev/disk4</string></dict>\
            <dict><key>mount-point</key><string>/tmp/ada-dmg-1/dmg.Ab12</string></dict>";
        assert_eq!(mount_point(plist).as_deref(), Some("/tmp/ada-dmg-1/dmg.Ab12"));
        assert_eq!(mount_point("<dict></dict>"), None);
    }

    #[test]
    fn every_tool_has_a_unique_id_and_a_download_or_brew() {
        let mut ids: Vec<_> = TOOLS.iter().map(|t| t.id).collect();
        ids.dedup();
        assert_eq!(ids.len(), TOOLS.len());
        for t in TOOLS {
            match t.method {
                Method::Dmg { .. } => assert!(t.download.is_some(), "{} needs a download", t.id),
                Method::Brew { .. } => assert!(t.download.is_none()),
            }
        }
        assert!(spec("nope").is_err());
    }

    #[test]
    fn file_names_come_from_the_url() {
        assert_eq!(file_name(KICAD.download.unwrap().url), "kicad-unified-universal-10.0.6.dmg");
    }
}
