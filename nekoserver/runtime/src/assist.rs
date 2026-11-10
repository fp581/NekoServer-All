//! Ask Neko (ASSISTANT.md): the dashboard's assistant page asks the AI assistant, a
//! language model that Ollama runs on this PC (modules/assistant.py installs it).
//!
//! How it stays out of the way: Ollama is asked on 127.0.0.1 only, one question at a
//! time, with a time limit; anything wrong with it ("not available right now", and why)
//! is said on the page and nowhere else. Nothing here is called by setup, the backups,
//! the updates or the recovery menu.
//!
//! What the model is given: the question, a short summary of the dashboard's readings,
//! and for an attention item its own text and the last lines of its service's log, with
//! passwords, keys, tokens, cookies, e-mail addresses and NekoServer's own secrets taken
//! out first (redact). It can change nothing: it may name one repair from a fixed list
//! (REPAIRS), which the runtime checks again, the page shows, and the person approves
//! with the PIN; for a NekoServer bug it writes a report for the person to read.

use crate::{apps, collect, common};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// Ollama, on this PC only (compose/assistant.yaml publishes it on 127.0.0.1).
pub const OLLAMA: &str = "127.0.0.1:11434";
/// What setup wrote about the assistant (modules/assistant.py report), on the app SSD.
const REPORT: &str = "reports/assistant.json";
/// The collector's readings, which the summary is made from.
pub const STATUS: &str = "/run/nekoserver-dashboard/status.json";
/// The longest question taken, and the longest answer asked for.
const QUESTION_CHARS: usize = 500;
const ANSWER_TOKENS: u64 = 700;
/// How many log lines of an attention item's service are read, and how much of them is kept.
const LOG_LINES: &str = "40";
const LOG_CHARS: usize = 6000;
const SUMMARY_CHARS: usize = 3000;
/// How long an answer may take: the first words may take a while (the model is loaded
/// into memory first); after that a pause of `next`, or the whole answer taking longer
/// than `total`, ends it. When the page has not asked for it for `abandon` (it was
/// closed), it is given up too, so the model stops working.
pub struct Limits {
    pub first: Duration,
    pub next: Duration,
    pub total: Duration,
    pub abandon: Duration,
}
pub const LIMITS: Limits = Limits {
    first: Duration::from_secs(150),
    next: Duration::from_secs(60),
    total: Duration::from_secs(180),
    abandon: Duration::from_secs(15),
};
/// Said when Ollama cannot answer, before the reason.
pub const NOT_AVAILABLE: &str = "Neko is not available right now";

/// The repairs Neko may suggest (the user's list, 0.49.0): each one an operation the
/// dashboard already has, run only after the person approves it with the PIN.
pub const REPAIRS: [&str; 4] = ["restart_app", "update_check", "update_app", "run_job"];
/// The backups "run_job" may start: (the dashboard's id, its service, a warning to show).
pub const JOBS: [(&str, &str, &str); 4] = [
    (
        "applications",
        "nekoserver-app-backup.service",
        "The media apps pause for a few minutes while they are copied.",
    ),
    ("host", "nekoserver-host-backup.service", ""),
    ("database", "nekoserver-immich-db-backup.service", ""),
    ("external", "nekoserver-photo-backup.service", ""),
];

/* ---------- secrets out ---------- */

/// `text` with every secret taken out: values of settings named like a password, key,
/// token, secret or cookie; "Bearer ..." credentials; Tailscale keys; a password in an
/// address; e-mail addresses; long random strings (keys, tokens, IDs); and each of
/// `known` (NekoServer's own secrets, see known_secrets) wherever it appears.
pub fn redact(text: &str, known: &[String]) -> String {
    let mut out = text.to_string();
    for value in known.iter().filter(|v| v.chars().count() >= 6) {
        out = out.replace(value.as_str(), "<removed>");
    }
    for (pattern, with) in PATTERNS.iter() {
        out = pattern.replace_all(&out, *with).into_owned();
    }
    LONG.replace_all(&out, |m: &regex::Captures| {
        let found = &m[0];
        // Keys, tokens and IDs are random: many digits among the letters. Long words
        // with dashes (a service's name) have few or none, and stay.
        if found.bytes().filter(u8::is_ascii_digit).count() >= 6 {
            "<removed>".to_string()
        } else {
            found.to_string()
        }
    })
    .into_owned()
}
/// Long strings that may be keys or tokens (never with "/", so a folder path stays).
static LONG: std::sync::LazyLock<regex::Regex> =
    std::sync::LazyLock::new(|| regex::Regex::new(r"\b[A-Za-z0-9+=_-]{32,}").unwrap());
static PATTERNS: std::sync::LazyLock<Vec<(regex::Regex, &'static str)>> = std::sync::LazyLock::new(
    || {
        [
            // A password in an address: https://user:password@host
            (
                r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@",
                "${1}<removed>@",
            ),
            // Authorization: Bearer ..., Basic ...
            (
                r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}",
                "$1 <removed>",
            ),
            // Cookie and Set-Cookie headers, whole.
            (r"(?im)\b((?:set-)?cookie)\s*[:=][^\n]*", "$1: <removed>"),
            // Tailscale auth keys and API keys.
            (r"\btskey-[A-Za-z0-9-]+", "<removed>"),
            // name=value, name: value and "name": "value" for names that say secret. Not
            // "Auth" or "Session" (log lines say "Auth: Auth-Success", "SessionManager:
            // Sending..."): their secret values are caught by Bearer, cookies, the
            // random-string rule and NekoServer's own saved secrets.
            (
                r#"(?i)\b([a-z0-9_.-]*(?:pass(?:word|wd|phrase)?|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)[a-z0-9_.-]*)("?\s*[=:]\s*"?)[^\s",;&]+"#,
                "$1$2<removed>",
            ),
            // E-mail addresses.
            (
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                "<e-mail removed>",
            ),
            // Login tokens made of three dotted parts (JWT).
            (
                r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                "<removed>",
            ),
        ]
        .into_iter()
        .map(|(p, with)| (regex::Regex::new(p).unwrap(), with))
        .collect()
    },
);

/// A setting's name that says its value is a secret.
static SECRET_NAME: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
    regex::Regex::new(r"(?i)(pass|secret|key|token|salt|api)").unwrap()
});

/// NekoServer's own secrets, to take out of anything the model sees: every saved app
/// password and login, every secret value in an app's private Compose .env, and the settings
/// that hold an address with a key in it (the phone alerts' topic, the outage monitor).
pub fn known_secrets(cfg: &Value, data: &Path) -> Vec<String> {
    let mut found = vec![];
    let logins = common::json(&data.join("secrets/accounts/credentials.json"), 128_000);
    for entry in logins["apps"].as_object().into_iter().flatten() {
        for key in ["password", "username"] {
            if let Some(v) = entry.1[key].as_str() {
                found.push(v.to_string());
            }
        }
    }
    for env in std::fs::read_dir(data.join("compose"))
        .into_iter()
        .flatten()
        .flatten()
    {
        let text = std::fs::read_to_string(env.path().join(".env")).unwrap_or_default();
        for line in text.lines() {
            // Only the values whose name says secret: the rest (DB_USERNAME=postgres, the
            // folders, the timezone, the image version) are what a log line names and a
            // person needs to read.
            if let Some((key, value)) = line.split_once('=') {
                if SECRET_NAME.is_match(key) {
                    found.push(value.trim().trim_matches('"').to_string());
                }
            }
        }
    }
    for (section, key) in [
        ("notifications", "endpoint"),
        ("heartbeat", "url"),
        ("heartbeat", "endpoint"),
    ] {
        if let Some(v) = cfg[section][key].as_str() {
            found.push(v.to_string());
        }
    }
    found.retain(|v| v.chars().count() >= 6);
    found
}

/* ---------- what the model is given ---------- */

/// The assistant is set up here: chosen in setup, and its model named in its report.
pub fn model(cfg: &Value, data: &Path) -> Option<String> {
    if cfg["assistant"]["wanted"] != true {
        return None;
    }
    let report = common::json(&data.join(REPORT), 64000);
    report["model"].as_str().map(str::to_string)
}

/// How many threads the model may answer with: the processor share setup gave it ("cpus"
/// in its report, half the cores). Ollama would otherwise start one per core, and with
/// only half of them allowed its threads wait for each other at every step: on the 0.49.0
/// gate the answers crawled at a fraction of their speed.
pub fn threads(data: &Path) -> u64 {
    let report = common::json(&data.join(REPORT), 64000);
    let cpus = &report["cpus"];
    cpus.as_u64()
        .or_else(|| cpus.as_str().and_then(|c| c.trim().parse().ok()))
        .unwrap_or(1)
        .clamp(1, 256)
}

/// Ollama's own store on the app SSD: compose/assistant.yaml mounts
/// appdata/assistant/models as /root/.ollama, and Ollama keeps its models in "models"
/// there (found on the 0.49.0 gate).
const STORE: &str = "appdata/assistant/models/models";
/// A download that has written nothing for this long is not running any more (cut off:
/// no network, a restart, the download stopped).
const DOWNLOAD_QUIET: Duration = Duration::from_secs(120);

/// The models Ollama has, by name ("qwen3.5:4b"), from Ollama itself (its /api/tags on
/// 127.0.0.1): its own files' layout changes between its versions. None when it does not
/// answer.
pub fn ollama_models(address: &str) -> Option<Vec<String>> {
    let at = address.parse::<SocketAddr>().ok()?;
    let mut ollama = TcpStream::connect_timeout(&at, Duration::from_secs(3)).ok()?;
    let _ = ollama.set_read_timeout(Some(Duration::from_secs(10)));
    let _ = ollama.set_write_timeout(Some(Duration::from_secs(5)));
    // The whole request at once: write! would send it in pieces.
    let request = format!("GET /api/tags HTTP/1.0\r\nHost: {address}\r\n\r\n");
    ollama.write_all(request.as_bytes()).ok()?;
    let mut answer = String::new();
    ollama.take(2_000_000).read_to_string(&mut answer).ok()?;
    let (head, body) = answer.split_once("\r\n\r\n")?;
    if head.split_whitespace().nth(1) != Some("200") {
        return None;
    }
    let listed: Value = serde_json::from_str(body.trim()).ok()?;
    Some(
        listed["models"]
            .as_array()?
            .iter()
            .filter_map(|m| m["name"].as_str().map(str::to_string))
            .collect(),
    )
}

/// A model download is running: one of Ollama's unfinished download files was written
/// within DOWNLOAD_QUIET.
pub fn downloading(store: &Path) -> bool {
    std::fs::read_dir(store.join("blobs"))
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().ends_with("-partial"))
        .filter_map(|e| e.metadata().ok()?.modified().ok())
        .any(|at| at.elapsed().is_ok_and(|age| age < DOWNLOAD_QUIET))
}

/// Why the model cannot answer yet, or None: not downloaded yet (setup downloads it from
/// the Ollama library, so it needs the internet), or still downloading. `have` is what
/// Ollama says it has; None (Ollama not answering) is left to the answer to say.
pub fn model_missing(data: &Path, model: &str, have: Option<&[String]>) -> Option<String> {
    let have = have?;
    let full = if model.contains(':') {
        model.to_string()
    } else {
        format!("{model}:latest")
    };
    if have.contains(&full) {
        return None;
    }
    Some(if downloading(&data.join(STORE)) {
        format!("its model {model} is still downloading; ask again once it is in")
    } else {
        format!("its model {model} is not downloaded yet; run  sudo bash install.sh  again (it needs the internet)")
    })
}

/// A question as the page sent it: plain text, at most QUESTION_CHARS characters.
pub fn question(value: &Value) -> Option<String> {
    let text = value.as_str()?.trim();
    if text.is_empty() {
        return None;
    }
    Some(
        text.chars()
            .filter(|c| !c.is_control() || *c == '\n')
            .take(QUESTION_CHARS)
            .collect(),
    )
}

/// An attention item's id as the page sent it ("app:jellyfin", "backup:host").
pub fn item_id(value: &Value) -> Option<&str> {
    value.as_str().filter(|id| {
        !id.is_empty()
            && id.len() <= 80
            && id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b":_.-".contains(&b))
    })
}

/// A short summary of the readings: what needs attention, the backups, the drives, the
/// apps. Plain lines, at most SUMMARY_CHARS characters.
pub fn summary(status: &Value) -> String {
    let rows = |key: &str| status[key].as_array().cloned().unwrap_or_default();
    let text = |v: &Value| v.as_str().unwrap_or("").to_string();
    let mut lines = vec![format!(
        "NekoServer {}",
        status["version"].as_str().unwrap_or("?")
    )];
    let attention = rows("alerts");
    if attention.is_empty() {
        lines.push("Nothing needs attention.".into());
    }
    for a in &attention {
        lines.push(format!(
            "Needs attention: {} ({})",
            text(&a["title"]),
            text(&a["level"])
        ));
    }
    for b in rows("backups") {
        lines.push(format!(
            "Backup {}: {}{}",
            text(&b["name"]),
            text(&b["status"]),
            b["reason"]
                .as_str()
                .map(|r| format!(" ({r})"))
                .unwrap_or_default()
        ));
    }
    for s in rows("storage") {
        lines.push(format!(
            "Drive {}: {}, {} free",
            text(&s["name"]),
            text(&s["status"]),
            s["free"]
                .as_f64()
                .map(|f| format!("{:.1} GB", f / 1e9))
                .unwrap_or_else(|| "?".into())
        ));
    }
    for a in rows("applications") {
        lines.push(format!(
            "App {}: {}{}",
            text(&a["name"]),
            text(&a["state"]),
            match a["health"].as_str() {
                Some(h @ ("unhealthy" | "starting")) => format!(", {h}"),
                _ => String::new(),
            }
        ));
    }
    common::shortened(&lines.join("\n"), SUMMARY_CHARS)
}

/// The attention item `id` from the readings: (its title and detail, the service or app
/// whose log explains it).
pub fn item(status: &Value, id: &str) -> Option<(String, Source)> {
    let a = status["alerts"]
        .as_array()?
        .iter()
        .find(|a| a["id"] == id)?;
    let words = format!(
        "{}: {}",
        a["title"].as_str().unwrap_or(""),
        a["detail"].as_str().unwrap_or("")
    );
    let source = match id.split_once(':') {
        Some(("backup", job)) => collect::JOBS
            .iter()
            .find(|j| j.0 == job)
            .map_or(Source::None, |j| Source::Unit(format!("{}.service", j.2))),
        Some(("app", app)) if collect::APPS.iter().any(|a| a.0 == app) => {
            Source::App(app.to_string())
        }
        _ => Source::None,
    };
    Some((words, source))
}

/// The terminal's colour codes ("\x1b[32m").
static COLOURS: std::sync::LazyLock<regex::Regex> =
    std::sync::LazyLock::new(|| regex::Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").unwrap());

/// Where an attention item's log lines come from.
#[derive(Debug, PartialEq)]
pub enum Source {
    Unit(String),
    App(String),
    None,
}

/// The last LOG_LINES lines of that log, as the system has them (journalctl for a
/// service, docker logs for an app's main container).
pub fn log_lines(source: &Source) -> String {
    let (code, text) = match source {
        Source::Unit(unit) => common::probe(
            &[
                "journalctl",
                "-u",
                unit,
                "-n",
                LOG_LINES,
                "-o",
                "cat",
                "--no-pager",
            ],
            15,
        ),
        Source::App(app) => {
            let service = apps::restart_plan(app).first().copied().unwrap_or("");
            let (_, id) = common::probe(
                &[
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    &format!("label=com.docker.compose.project=neko-{app}"),
                    "--filter",
                    &format!("label=com.docker.compose.service={service}"),
                ],
                15,
            );
            match id.split_whitespace().next() {
                Some(id) => common::probe(&["docker", "logs", "--tail", LOG_LINES, id], 15),
                None => (1, String::new()),
            }
        }
        Source::None => (1, String::new()),
    };
    if code != 0 {
        return String::new();
    }
    plain_log(&text)
}

/// A log's last LOG_CHARS characters, without the terminal's colour codes.
fn plain_log(text: &str) -> String {
    let plain = COLOURS.replace_all(text, "");
    let kept: String = plain.chars().rev().take(LOG_CHARS).collect();
    kept.chars().rev().collect()
}

/// The repairs open to this question, as the model is told them: one per installed app,
/// app with a version waiting, and backup that runs here.
pub fn offered(cfg: &Value, status: &Value, data: &Path) -> Vec<(String, String)> {
    let mut out = vec![];
    for app in apps::installed(cfg) {
        out.push((
            format!("restart_app:{app}"),
            format!("restart {}", app_name(app)),
        ));
    }
    out.push((
        "update_check".into(),
        "look for new app versions now".into(),
    ));
    for app in apps::installed(cfg) {
        if waiting_update(data, app).is_some() {
            out.push((
                format!("update_app:{app}"),
                format!(
                    "update {} now (a snapshot first, undone if it fails)",
                    app_name(app)
                ),
            ));
        }
    }
    for (job, _, _) in JOBS {
        if job_runs_here(status, job) {
            out.push((
                format!("run_job:{job}"),
                format!("run the backup \"{}\" now", job_name(job)),
            ));
        }
    }
    out
}

/// The messages for Ollama: what Neko is and may do, then the question with the readings
/// and the item. Every part that comes from the server has had its secrets taken out.
pub fn messages(
    question: &str,
    summary: &str,
    about: Option<(&str, &str)>,
    repairs: &[(String, String)],
    known: &[String],
) -> Value {
    let list = repairs
        .iter()
        .map(|(id, what)| format!("- {id}: {what}"))
        .collect::<Vec<_>>()
        .join("\n");
    let system = format!(
        "You are Neko, the helper in the dashboard of a NekoServer home server. Answer in plain, short words for a \
         person who is not a computer expert. Use only the readings and log lines given; when they do not say, say \
         you do not know. You cannot run commands or change anything yourself.\n\
         If one of these fixes would help, end your answer with exactly one line \"ACTION: <id>\" naming it, and \
         nothing else on that line; otherwise write no ACTION line:\n{list}\n\
         If the problem looks like a bug in NekoServer itself, end with a part that starts \"BUG REPORT:\": what \
         failed, what the log shows, and a fix you suggest in words."
    );
    let mut user = format!(
        "{}\n\nThe dashboard now:\n{}",
        redact(question, known),
        redact(summary, known)
    );
    if let Some((words, log)) = about {
        user.push_str(&format!(
            "\n\nThe problem asked about:\n{}",
            redact(words, known)
        ));
        if !log.trim().is_empty() {
            user.push_str(&format!("\n\nIts last log lines:\n{}", redact(log, known)));
        }
    }
    json!([{"role": "system", "content": system}, {"role": "user", "content": user}])
}

/* ---------- repairs ---------- */

/// A repair the person may approve: its id, exactly what it does, and a warning when
/// there is one.
#[derive(Debug, PartialEq)]
pub struct Repair {
    pub id: String,
    pub does: String,
    pub warning: String,
}

/// The repair `id` names, checked: one of REPAIRS, for an app installed here (and, to
/// update, with a version waiting) or a backup that runs here. Anything else is refused
/// with the reason.
pub fn repair(
    cfg: &Value,
    status: &Value,
    data: &Path,
    id: &str,
) -> std::result::Result<Repair, String> {
    let (kind, name) = id.split_once(':').unwrap_or((id, ""));
    if !REPAIRS.contains(&kind) {
        return Err(format!("\"{}\" is not one of Neko's repairs", chars(id)));
    }
    let installed = || apps::installed(cfg).into_iter().find(|a| *a == name);
    let made = |does: String, warning: &str| Repair {
        id: id.to_string(),
        does,
        warning: warning.to_string(),
    };
    match kind {
        "update_check" if name.is_empty() => Ok(made(
            "Looks for new versions of your apps now (the daily check). It changes nothing; the Updates page shows what it finds.".into(),
            "",
        )),
        "restart_app" => installed()
            .map(|app| {
                made(
                    format!("Restarts {}: its containers stop and start again, as Restart app on Your apps does. Its data stays as it is.", app_name(app)),
                    "",
                )
            })
            .ok_or_else(|| "That app is not installed on this server".to_string()),
        "update_app" => {
            let app = installed().ok_or("That app is not installed on this server")?;
            let (version, _) = waiting_update(data, app)
                .ok_or("No new version of that app is waiting; Check for updates looks again")?;
            Ok(made(
                format!("Updates {} to {version} now: a snapshot first, then the update, then a check; if the check fails, it is undone.", app_name(app)),
                "The app is unavailable for a few minutes while it updates.",
            ))
        }
        "run_job" => {
            let (job, _, warning) = JOBS
                .iter()
                .find(|j| j.0 == name && job_runs_here(status, name))
                .ok_or("That backup does not run on this server")?;
            Ok(made(
                format!("Runs the backup \"{}\" now, as its timer would. The Backups page shows how it ends.", job_name(job)),
                warning,
            ))
        }
        _ => Err(format!("\"{}\" is not one of Neko's repairs", chars(id))),
    }
}

/// Why a backup cannot be started now, or None: setup is running, an upgrade or a drive
/// move is under way, an interrupted job waits to be finished, or another NekoServer job
/// is running (each job holds its own lock while it runs).
pub fn job_blocked(operation_lock: &Path, active: &dyn Fn(&str) -> bool) -> Option<String> {
    if common::setup_running() {
        return Some("Setup is running; try again once it has finished".into());
    }
    if held(operation_lock) {
        return Some("A NekoServer upgrade or a move to a new drive is under way".into());
    }
    if let Some(job) = apps::unfinished_job(&apps::journals()) {
        return Some(format!("{job} is still waiting to be finished"));
    }
    collect::JOBS
        .iter()
        .find(|j| active(&format!("{}.service", j.2)))
        .map(|j| format!("{} is running now", j.1))
}

/// Another program holds this lock file now (tried without waiting, let go at once).
fn held(path: &Path) -> bool {
    use std::os::unix::io::AsRawFd;
    let Ok(file) = std::fs::File::open(path) else {
        return false;
    };
    let fd = file.as_raw_fd();
    // SAFETY: flock on a descriptor this function owns; released before it returns.
    let got = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } == 0;
    if got {
        unsafe { libc::flock(fd, libc::LOCK_UN) };
    }
    !got
}

/// A backup's service is starting or running.
pub fn unit_active(unit: &str) -> bool {
    matches!(
        common::probe(&["systemctl", "is-active", unit], 10)
            .1
            .trim(),
        "active" | "activating" | "reloading"
    )
}

/// The backup `job` is set up here: the dashboard lists it and it is not switched off.
fn job_runs_here(status: &Value, job: &str) -> bool {
    status["backups"]
        .as_array()
        .into_iter()
        .flatten()
        .any(|b| b["id"] == job && b["status"] != "disabled")
}

/// (version, signature) of the newest version of `app` the daily check found waiting.
fn waiting_update(data: &Path, app: &str) -> Option<(String, String)> {
    let pending = crate::updates::pending(data);
    let newest = pending["apps"][app].as_array()?.last()?;
    Some((
        newest["version"].as_str()?.to_string(),
        newest["sig"].as_str()?.to_string(),
    ))
}

/// The signature "update_app" passes to "Update now".
pub fn update_signature(data: &Path, app: &str) -> Option<String> {
    waiting_update(data, app).map(|(_, sig)| sig)
}

fn app_name(app: &str) -> &'static str {
    collect::APPS
        .iter()
        .find(|a| a.0 == app)
        .map_or("the app", |a| a.1)
}
fn job_name(job: &str) -> &'static str {
    collect::JOBS
        .iter()
        .find(|j| j.0 == job)
        .map_or("backup", |j| j.1)
}
fn chars(text: &str) -> String {
    text.chars().take(60).collect()
}

/* ---------- the answer, asked for in the background ---------- */
// The dashboard's web service has a few workers, each held for at most a few seconds
// (the audit of 0.45.6). An answer takes minutes on a processor, so it is never waited
// for on a connection: one thread outside the workers asks Ollama and keeps the answer
// so far here, and the page asks for it about once a second (progress).

/// One question at a time (Ollama answers one at a time too, OLLAMA_NUM_PARALLEL=1).
static ASKING: AtomicBool = AtomicBool::new(false);

/// Holds the "one question at a time" slot until its answer has ended.
pub struct Turn;
impl Turn {
    pub fn take() -> Option<Turn> {
        ASKING
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .ok()
            .map(|_| Turn)
    }
}
impl Drop for Turn {
    fn drop(&mut self) {
        ASKING.store(false, Ordering::SeqCst);
    }
}

/// Said when a second question comes while Neko answers one.
pub const BUSY: &str = "Neko is busy with another question; ask again once it has answered";
/// How long an answer is kept for the page after it has ended.
const KEEP_AFTER: Duration = Duration::from_secs(300);

/// An answer being written, or just written.
struct Ask {
    /// Who asked (their PIN session, or the server itself): only they may read it.
    owner: String,
    text: String,
    failed: Option<String>,
    done: bool,
    /// The repair its last ACTION line names, when it is one from the list offered.
    repair: Option<String>,
    started: Instant,
    ended: Option<Instant>,
    total: Duration,
    /// When the page last asked for it.
    polled: Instant,
}
static ASKS: std::sync::LazyLock<std::sync::Mutex<std::collections::HashMap<String, Ask>>> =
    std::sync::LazyLock::new(Default::default);

fn asks() -> std::sync::MutexGuard<'static, std::collections::HashMap<String, Ask>> {
    ASKS.lock().unwrap_or_else(|e| e.into_inner())
}

/// A random id for an ask: 32 hex characters from the kernel's random numbers.
fn new_id() -> std::result::Result<String, String> {
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|e| format!("no random numbers ({e})"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// Start answering in the background (the turn is held until the answer has ended) and
/// return the ask's id. `owner` is who may read it.
#[allow(clippy::too_many_arguments)]
pub fn begin(
    turn: Turn,
    owner: String,
    address: String,
    model: String,
    threads: u64,
    messages: Value,
    repairs: Vec<(String, String)>,
    limits: Limits,
) -> std::result::Result<String, String> {
    let id = new_id()?;
    {
        let mut all = asks();
        all.retain(|_, a| a.ended.is_none_or(|t| t.elapsed() < KEEP_AFTER));
        all.insert(
            id.clone(),
            Ask {
                owner,
                text: String::new(),
                failed: None,
                done: false,
                repair: None,
                started: Instant::now(),
                ended: None,
                total: limits.total,
                polled: Instant::now(),
            },
        );
    }
    let key = id.clone();
    std::thread::Builder::new()
        .name("ask-neko".into())
        .spawn(move || {
            let _turn = turn;
            let keep = || wanted(&key, limits.abandon);
            let result = stream(
                &address,
                &model,
                threads,
                &messages,
                &limits,
                &keep,
                &mut |piece| {
                    if let Some(a) = asks().get_mut(&key) {
                        a.text.push_str(piece);
                    }
                },
            );
            let mut all = asks();
            if let Some(a) = all.get_mut(&key) {
                match result {
                    Ok(answer) => {
                        a.done = true;
                        a.repair = suggested(&answer, &repairs);
                    }
                    Err(why) => a.failed = a.failed.take().or(Some(why)),
                }
                a.ended = a.ended.or(Some(Instant::now()));
            }
        })
        .map_err(|e| format!("could not start ({e})"))?;
    Ok(id)
}

/// The answer so far, for its owner only: {"text", "done", "failed", "repair"}. None for
/// an unknown or expired id, or someone else's. An answer past its time limit is ended
/// here even while Ollama is still silent.
pub fn progress(id: &str, owner: &str) -> Option<Value> {
    let mut all = asks();
    let a = all.get_mut(id).filter(|a| a.owner == owner)?;
    a.polled = Instant::now();
    if a.ended.is_some_and(|t| t.elapsed() >= KEEP_AFTER) {
        all.remove(id);
        return None;
    }
    if a.ended.is_none() && a.started.elapsed() > a.total {
        a.failed = Some("the answer took too long, so it was stopped".into());
        a.ended = Some(Instant::now());
    }
    Some(json!({
        "text": a.text,
        "done": a.done,
        "failed": a.failed.as_ref().map(|why| format!("{NOT_AVAILABLE}: {why}")),
        "repair": a.repair,
    }))
}

/// The answer is still wanted: not ended (its time limit ends it in progress), and its
/// page asked for it within `abandon`.
fn wanted(id: &str, abandon: Duration) -> bool {
    asks()
        .get(id)
        .is_some_and(|a| a.ended.is_none() && a.polled.elapsed() < abandon)
}

/// Why a line from Ollama did not come.
enum Waited {
    TooLong,
    NotWanted,
    Broken,
}

/// One line from Ollama (as bytes: a line cut by a wake-up is kept whole), waiting at
/// most `limit`, and giving up as soon as `keep` says the answer is not wanted any more.
/// The connection's read time limit is short, so `keep` is asked about once a second.
fn next_line(
    reader: &mut BufReader<std::io::Take<TcpStream>>,
    line: &mut Vec<u8>,
    limit: Duration,
    keep: &dyn Fn() -> bool,
) -> std::result::Result<usize, Waited> {
    let start = Instant::now();
    loop {
        match reader.read_until(b'\n', line) {
            Ok(n) => return Ok(n),
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                if !keep() {
                    return Err(Waited::NotWanted);
                }
                if start.elapsed() >= limit {
                    return Err(Waited::TooLong);
                }
            }
            Err(_) => return Err(Waited::Broken),
        }
    }
}

/// Ask Ollama at `address` (OLLAMA, or a test's own) and hand each piece of the answer
/// to `piece` as it comes. Returns the whole answer, or why there is none. When `keep`
/// says it is not wanted any more, the connection is closed, and Ollama stops.
pub fn stream(
    address: &str,
    model: &str,
    threads: u64,
    messages: &Value,
    limits: &Limits,
    keep: &dyn Fn() -> bool,
    piece: &mut dyn FnMut(&str),
) -> std::result::Result<String, String> {
    // No thinking aloud first: Qwen3.5 thinks before it answers unless told not to, and
    // on a small processor that took the whole time limit before the first word (the
    // 0.49.0 gate). Its thinking would not be shown either.
    let body = json!({"model": model, "messages": messages, "stream": true, "think": false,
        "options": {"num_predict": ANSWER_TOKENS, "num_thread": threads}})
    .to_string();
    let at = address
        .parse::<SocketAddr>()
        .map_err(|_| "its address is not one".to_string())?;
    let mut ollama = TcpStream::connect_timeout(&at, Duration::from_secs(3)).map_err(|_| {
        "the AI assistant does not answer (it may be stopped, or still starting)".to_string()
    })?;
    let _ = ollama.set_write_timeout(Some(Duration::from_secs(5)));
    let tick = Duration::from_secs(1).min(limits.next).min(limits.first);
    let _ = ollama.set_read_timeout(Some(tick));
    // HTTP/1.0: Ollama then sends the answer as it comes and closes, without chunks.
    let request = format!("POST /api/chat HTTP/1.0\r\nHost: {address}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len());
    ollama
        .write_all(request.as_bytes())
        .map_err(|_| "the AI assistant did not take the question".to_string())?;
    let mut reader = BufReader::new(ollama.take(4_000_000));
    let mut line = vec![];
    let waited = |why: Waited, slow: &str| match why {
        Waited::TooLong => slow.to_string(),
        Waited::NotWanted => "the page stopped asking for the answer, so it was stopped".into(),
        Waited::Broken => "the AI assistant stopped answering (out of memory, perhaps)".into(),
    };
    let slow_start = "the AI assistant took too long to start answering";
    match next_line(&mut reader, &mut line, limits.first, keep) {
        Ok(0) => return Err(slow_start.into()),
        Ok(_) => {}
        Err(why) => return Err(waited(why, slow_start)),
    }
    let status = String::from_utf8_lossy(&line).to_string();
    let code = status.split_whitespace().nth(1).unwrap_or("").to_string();
    loop {
        line.clear();
        match next_line(&mut reader, &mut line, limits.next, keep) {
            Ok(0) => return Err("the AI assistant stopped answering".into()),
            Ok(_) if line.iter().all(u8::is_ascii_whitespace) => break,
            Ok(_) => {}
            Err(why) => return Err(waited(why, "the AI assistant stopped answering")),
        }
    }
    if code != "200" {
        let mut text = String::new();
        let _ = reader.read_to_string(&mut text);
        let said = serde_json::from_str::<Value>(text.trim())
            .ok()
            .and_then(|v| v["error"].as_str().map(str::to_string))
            .unwrap_or_else(|| format!("it answered {code}"));
        return Err(plain_error(&said));
    }
    let started = Instant::now();
    let mut answer = String::new();
    let too_long = "the answer took too long, so it was stopped";
    loop {
        if started.elapsed() > limits.total {
            return Err(too_long.into());
        }
        line.clear();
        // The first words may take a while (the model is loaded first), the next ones less.
        let limit = if answer.is_empty() {
            limits.first
        } else {
            limits.next
        };
        match next_line(&mut reader, &mut line, limit, keep) {
            Ok(0) => break,
            Ok(_) => {}
            Err(why) => return Err(waited(why, too_long)),
        }
        let Ok(part) = serde_json::from_slice::<Value>(line.trim_ascii()) else {
            continue;
        };
        if let Some(why) = part["error"].as_str() {
            return Err(plain_error(why));
        }
        if let Some(text) = part["message"]["content"].as_str() {
            answer.push_str(text);
            piece(text);
        }
        if part["done"] == true {
            break;
        }
    }
    Ok(answer)
}

/// Ollama's error in words a person can act on.
fn plain_error(said: &str) -> String {
    let lower = said.to_lowercase();
    if lower.contains("not found") {
        "its model is not there yet (it may still be downloading; setup run again fetches it)"
            .into()
    } else if lower.contains("memory") || lower.contains("killed") {
        "the model needs more memory than it may have here, so it was stopped".into()
    } else if lower.contains("while running the model") || lower.contains("unexpectedly stopped") {
        // Its runner was ended under it: on the 0.49.0 gate the kernel took the memory
        // back for the apps (the model goes first), and Ollama said only "unexpected".
        "the model was stopped while it answered, most likely because your apps needed the memory; ask again in a while".into()
    } else {
        format!("it said \"{}\"", chars(&redact(said, &[])))
    }
}

/// The repair the answer's last ACTION line names, or None: only an exact id from the
/// list given (anything else in the answer is ignored).
pub fn suggested(answer: &str, offered: &[(String, String)]) -> Option<String> {
    // Small models dress the line up: "**ACTION: restart_app:jellyfin:**" (the 0.49.0 gate).
    let dressing = |c: char| c.is_whitespace() || matches!(c, '*' | '_' | '`' | '"' | '.' | ':');
    let named = answer
        .lines()
        .rev()
        .find_map(|l| l.trim_start_matches(dressing).strip_prefix("ACTION:"))?
        .trim_matches(dressing);
    offered
        .iter()
        .find(|(id, _)| id == named)
        .map(|(id, _)| id.clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    const PLANTED: [&str; 9] = [
        "S3cret-Passw0rd!",
        "tskey-auth-kAbCdEf1CNTRL-0123456789abcdef",
        "me@example.org",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
        "AKIA0123456789ABCDEFGHIJKLMNOPQRSTUV",
        "hunter2hunter2",
        "cookie-value-123",
        "bearer-value-987654",
        "envsecret-555",
    ];

    fn log_with_secrets() -> String {
        [
            "Connecting to https://admin:S3cret-Passw0rd!@nas.local/share",
            "tailscale up --auth-key=tskey-auth-kAbCdEf1CNTRL-0123456789abcdef",
            "Mail sent to me@example.org",
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
            "AWS key AKIA0123456789ABCDEFGHIJKLMNOPQRSTUV refused",
            "db login failed for neko with PASSWORD=hunter2hunter2",
            "Set-Cookie: session=cookie-value-123; Path=/",
            "Authorization: Bearer bearer-value-987654",
            "the saved app password envsecret-555 did not work",
            "No space left on device while writing /mnt/nekoserver-media/backups",
        ]
        .join("\n")
    }

    #[test]
    fn no_planted_secret_reaches_the_model() {
        let known = vec!["envsecret-555".to_string()];
        let m = messages(
            "Why did my backup fail? My mail is me@example.org",
            "Needs attention: Host and user snapshot (critical)",
            Some(("Host and user snapshot: failed", &log_with_secrets())),
            &[(
                "update_check".into(),
                "look for new app versions now".into(),
            )],
            &known,
        );
        let sent = m.to_string();
        for secret in PLANTED {
            assert!(!sent.contains(secret), "{secret} reached the model: {sent}");
        }
        // What explains the problem is still there.
        assert!(sent.contains("No space left on device"), "{sent}");
        assert!(sent.contains("<removed>"));
    }

    /// The partner of the planted-secret test: real lines from the apps' logs on the test
    /// machine keep the words a person needs to understand them.
    #[test]
    fn the_words_that_explain_a_problem_stay() {
        let lines = [
            "2026-11-09 17:17:17.202 UTC [55] LOG:  database system is ready to accept connections",
            "[Info] Auth: Auth-Success ip 172.18.0.1 username 'neko_a0e91dadffeb'",
            "[16:37:17.505] [INF] [19] Emby.Server.Implementations.Session.SessionManager: Sending shutdown notifications",
            "Authentication: failed",
            "session: started",
            "[Fatal] ConsoleApp: EPIC FAIL!",
            "2026-11-09T17:19:46.297Z [error][Auth]: Failed to delete Jellyfin device {\"error\":\"Request failed with status code 400\",\"userId\":1}",
            "FATAL:  password authentication failed for user \"postgres\"",
            "Installing with PostgreSQL database",
            "No space left on device while writing /srv/nekoserver/photos/immich-library/upload",
        ];
        for line in lines {
            assert_eq!(redact(line, &[]), line, "{line}");
        }
        // The secrets in such lines still go: an e-mail address and a long random id.
        let seerr = "[warn][API]: Failed sign-in attempt using invalid Seerr password {\"ip\":\"::ffff:172.18.0.1\",\"email\":\"neko_1a3d5940d158@server.invalid\"} jellyfinUserId 59cb2ea705e94733ab0ee31809a1a24c";
        let said = redact(seerr, &[]);
        assert!(
            !said.contains("neko_1a3d5940d158@server.invalid")
                && !said.contains("59cb2ea705e94733ab0ee31809a1a24c"),
            "{said}"
        );
        assert!(
            said.contains("Failed sign-in attempt using invalid Seerr password"),
            "{said}"
        );
    }

    #[test]
    fn an_apps_env_gives_up_its_secrets_and_keeps_the_rest() {
        let root = std::env::temp_dir().join(format!("neko-assist-env-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("compose/immich")).unwrap();
        std::fs::write(
            root.join("compose/immich/.env"),
            "UPLOAD_LOCATION=/srv/nekoserver/photos/immich-library\nDB_DATA_LOCATION=/srv/nekoserver/appdata/immich/postgres\n\
             TZ=Europe/London\nIMMICH_VERSION=v2.7.4\nDB_USERNAME=postgres\nDB_DATABASE_NAME=immich\nDB_PASSWORD=Pw-from-immich-env-77\n",
        )
        .unwrap();
        let known = known_secrets(&json!({}), &root);
        assert_eq!(known, ["Pw-from-immich-env-77"]);
        let line = "connection to postgres at /srv/nekoserver/appdata/immich/postgres refused (password Pw-from-immich-env-77)";
        let said = redact(line, &known);
        assert!(!said.contains("Pw-from-immich-env-77"), "{said}");
        assert!(
            said.contains("postgres at /srv/nekoserver/appdata/immich/postgres refused"),
            "{said}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn colour_codes_are_taken_out_of_a_log() {
        let shown =
            plain_log("\x1b[32m[Nest] 24  - \x1b[39m11/10/2026 \x1b[31mERROR\x1b[39m no space");
        assert_eq!(shown, "[Nest] 24  - 11/10/2026 ERROR no space");
    }

    #[test]
    fn known_secrets_come_from_the_logins_the_env_files_and_the_settings() {
        let root = std::env::temp_dir().join(format!("neko-assist-known-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("secrets/accounts")).unwrap();
        std::fs::create_dir_all(root.join("compose/immich")).unwrap();
        std::fs::write(
            root.join("secrets/accounts/credentials.json"),
            r#"{"format":1,"apps":{"jellyfin":{"username":"neko_ab12cd","password":"Pw-from-the-store-1"}}}"#,
        )
        .unwrap();
        std::fs::write(
            root.join("compose/immich/.env"),
            "DB_PASSWORD=\"db-pass-from-env\"\nX=1\n",
        )
        .unwrap();
        let cfg = json!({"notifications": {"endpoint": "https://ntfy.sh/my-secret-topic"}});
        let known = known_secrets(&cfg, &root);
        for want in [
            "Pw-from-the-store-1",
            "neko_ab12cd",
            "db-pass-from-env",
            "https://ntfy.sh/my-secret-topic",
        ] {
            assert!(known.iter().any(|k| k == want), "{want}: {known:?}");
        }
        assert!(
            !known.iter().any(|k| k == "1"),
            "short values are not secrets"
        );
        let said = redact(
            "alert sent to https://ntfy.sh/my-secret-topic for neko_ab12cd",
            &known,
        );
        assert!(
            !said.contains("my-secret-topic") && !said.contains("neko_ab12cd"),
            "{said}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_summary_names_what_needs_attention_the_backups_drives_and_apps() {
        let status = json!({"version": "0.49.0",
            "alerts": [{"id": "backup:host", "title": "Host and user snapshot failed", "level": "critical"}],
            "backups": [{"id": "host", "name": "Host and user snapshot", "status": "fail", "reason": "No space left"}],
            "storage": [{"name": "Media HDD", "status": "mounted", "free": 12_000_000_000u64}],
            "applications": [{"name": "Jellyfin", "state": "running", "health": "unhealthy"}]});
        let s = summary(&status);
        assert!(s.contains("NekoServer 0.49.0"));
        assert!(s.contains("Needs attention: Host and user snapshot failed (critical)"));
        assert!(s.contains("Backup Host and user snapshot: fail (No space left)"));
        assert!(s.contains("Drive Media HDD: mounted, 12.0 GB free"));
        assert!(s.contains("App Jellyfin: running, unhealthy"));
        assert!(summary(&json!({})).contains("Nothing needs attention"));
    }

    #[test]
    fn an_item_names_the_log_that_explains_it() {
        let status = json!({"alerts": [
            {"id": "backup:host", "title": "Host and user snapshot", "detail": "failed"},
            {"id": "app:jellyfin", "title": "Jellyfin needs attention", "detail": "exited"},
            {"id": "mount:media_hdd", "title": "Media HDD", "detail": "missing"}]});
        assert_eq!(
            item(&status, "backup:host").unwrap().1,
            Source::Unit("nekoserver-host-backup.service".into())
        );
        assert_eq!(
            item(&status, "app:jellyfin").unwrap().1,
            Source::App("jellyfin".into())
        );
        assert_eq!(item(&status, "mount:media_hdd").unwrap().1, Source::None);
        assert!(item(&status, "app:unknown").is_none());
        assert_eq!(
            item(&status, "backup:host").unwrap().0,
            "Host and user snapshot: failed"
        );
    }

    #[test]
    fn a_question_and_an_item_id_are_checked() {
        assert_eq!(question(&json!("  why?  ")).as_deref(), Some("why?"));
        assert!(question(&json!("")).is_none());
        assert!(question(&json!(42)).is_none());
        assert_eq!(
            question(&json!("x".repeat(900))).unwrap().len(),
            QUESTION_CHARS
        );
        assert_eq!(question(&json!("a\u{7}b")).as_deref(), Some("ab"));
        assert_eq!(item_id(&json!("backup:host")), Some("backup:host"));
        for bad in [json!("../etc"), json!("a b"), json!(""), json!(1)] {
            assert!(item_id(&bad).is_none(), "{bad}");
        }
    }

    fn installed() -> (Value, Value) {
        let cfg = json!({"media": {"services": ["jellyfin", "sonarr"]}, "photos": {"deploy_immich": true},
            "nextcloud": {"enabled": false}, "assistant": {"wanted": true}});
        let status = json!({"backups": [
            {"id": "applications", "status": "pass"}, {"id": "host", "status": "fail"},
            {"id": "external", "status": "disabled"}, {"id": "maintenance", "status": "pass"}]});
        (cfg, status)
    }

    #[test]
    fn only_repairs_from_the_list_for_what_is_installed_are_offered() {
        let (cfg, status) = installed();
        let data = Path::new("/nonexistent/neko-assist");
        let ids: Vec<String> = offered(&cfg, &status, data)
            .into_iter()
            .map(|r| r.0)
            .collect();
        assert_eq!(
            ids,
            [
                "restart_app:jellyfin",
                "restart_app:immich",
                "restart_app:sonarr",
                "update_check",
                "run_job:applications",
                "run_job:host"
            ]
        );
    }

    #[test]
    fn a_repair_is_checked_against_the_list_and_what_is_installed() {
        let (cfg, status) = installed();
        let data = Path::new("/nonexistent/neko-assist");
        let ok = |id: &str| repair(&cfg, &status, data, id);
        assert!(ok("restart_app:jellyfin")
            .unwrap()
            .does
            .starts_with("Restarts Jellyfin"));
        assert!(ok("update_check").is_ok());
        let backup = ok("run_job:applications").unwrap();
        assert!(backup.warning.contains("pause"), "{backup:?}");
        assert!(ok("run_job:host").unwrap().warning.is_empty());
        for refused in [
            "restart_app:radarr",    // not installed
            "restart_app:../../etc", // not an app
            "run_job:external",      // switched off
            "run_job:maintenance",   // not one run_job may start
            "update_app:jellyfin",   // no version waiting
            "update_check:now",      // update_check takes nothing
            "format_disk:sda",       // not a repair at all
            "rm -rf /",
            "",
        ] {
            assert!(ok(refused).is_err(), "{refused}");
        }
    }

    #[test]
    fn a_backup_is_refused_while_another_job_runs_or_a_lock_is_held() {
        let lock = std::env::temp_dir().join(format!("neko-assist-lock-{}", std::process::id()));
        std::fs::write(&lock, "").unwrap();
        assert_eq!(job_blocked(&lock, &|_| false), None);
        let busy = job_blocked(&lock, &|unit| unit == "nekoserver-photo-backup.service");
        assert_eq!(
            busy.as_deref(),
            Some("USB copy (photos and files) is running now")
        );
        let held_open = std::fs::File::open(&lock).unwrap();
        use std::os::unix::io::AsRawFd;
        assert_eq!(
            unsafe { libc::flock(held_open.as_raw_fd(), libc::LOCK_EX) },
            0
        );
        // flock locks belong to the open file: this function's own open is another one.
        let said = job_blocked(&lock, &|_| false);
        assert_eq!(
            said.as_deref(),
            Some("A NekoServer upgrade or a move to a new drive is under way")
        );
        drop(held_open);
        assert_eq!(job_blocked(&lock, &|_| false), None);
        let _ = std::fs::remove_file(&lock);
    }

    #[test]
    fn only_an_exact_action_line_from_the_list_counts() {
        let offered = vec![
            ("restart_app:jellyfin".to_string(), String::new()),
            ("update_check".to_string(), String::new()),
        ];
        assert_eq!(
            suggested("It stopped.\nACTION: restart_app:jellyfin", &offered).as_deref(),
            Some("restart_app:jellyfin")
        );
        assert_eq!(
            suggested("ACTION: `update_check`.", &offered).as_deref(),
            Some("update_check")
        );
        // Dressed up in Markdown, as a small model wrote it on the 0.49.0 gate.
        assert_eq!(
            suggested(
                "**ACTION: restart_app:jellyfin:**  \nJellyfin stopped.",
                &offered
            )
            .as_deref(),
            Some("restart_app:jellyfin")
        );
        assert_eq!(
            suggested("_ACTION:_ `update_check`", &offered).as_deref(),
            Some("update_check")
        );
        assert_eq!(suggested("**ACTION: restart_app:radarr**", &offered), None);
        assert_eq!(suggested("ACTION: restart_app:radarr", &offered), None);
        assert_eq!(suggested("ACTION: sudo rm -rf /", &offered), None);
        assert_eq!(suggested("restart_app:jellyfin would help", &offered), None);
        assert_eq!(suggested("", &offered), None);
    }

    #[test]
    fn the_prompt_lists_only_the_repairs_offered() {
        let m = messages(
            "why",
            "",
            None,
            &[("update_check".into(), "look now".into())],
            &[],
        );
        let system = m[0]["content"].as_str().unwrap();
        assert!(system.contains("- update_check: look now"));
        assert!(system.contains("ACTION: <id>"));
        assert!(system.contains("BUG REPORT:"));
        assert!(system.contains("cannot run commands"));
        assert!(!system.contains("restart_app"));
    }

    /// A stand-in for Ollama: answers one request with `reply` (status line, headers and
    /// body), or holds the connection without a word when `reply` is None.
    fn fake_ollama(reply: Option<String>) -> (String, std::thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let handle = std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut got = vec![0u8; 65536];
            let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
            let n = s.read(&mut got).unwrap_or(0);
            if let Some(r) = reply {
                let _ = s.write_all(r.as_bytes());
            } else {
                std::thread::sleep(Duration::from_secs(3));
            }
            String::from_utf8_lossy(&got[..n]).to_string()
        });
        (address, handle)
    }

    fn collect(
        address: &str,
        limits: &Limits,
    ) -> (std::result::Result<String, String>, Vec<String>) {
        let mut pieces = vec![];
        let result = stream(
            address,
            "qwen3.5:4b",
            2,
            &json!([{"role": "user", "content": "why"}]),
            limits,
            &|| true,
            &mut |p| pieces.push(p.to_string()),
        );
        (result, pieces)
    }

    #[test]
    fn the_answer_is_passed_on_as_it_comes() {
        let body = [
            r#"{"message":{"content":"The disk "},"done":false}"#,
            r#"{"message":{"content":"is full.\nACTION: update_check"},"done":false}"#,
            r#"{"message":{"content":""},"done":true}"#,
        ]
        .join("\n");
        let (address, server) = fake_ollama(Some(format!(
            "HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\n\r\n{body}\n"
        )));
        let (answer, pieces) = collect(&address, &LIMITS);
        assert_eq!(answer.unwrap(), "The disk is full.\nACTION: update_check");
        assert_eq!(pieces[0], "The disk ");
        let asked = server.join().unwrap();
        assert!(asked.starts_with("POST /api/chat HTTP/1.0"), "{asked}");
        assert!(asked.contains(r#""model":"qwen3.5:4b""#) && asked.contains(r#""stream":true"#));
        // Asked to answer at once, without thinking aloud first (the 0.49.0 gate).
        assert!(asked.contains(r#""think":false"#), "{asked}");
        // With the threads of its processor share, never one per core (the 0.49.0 gate).
        assert!(asked.contains(r#""num_thread":2"#), "{asked}");
    }

    #[test]
    fn the_threads_are_its_processor_share() {
        let data = std::env::temp_dir().join(format!("neko-assist-threads-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&data);
        std::fs::create_dir_all(data.join("reports")).unwrap();
        assert_eq!(threads(&data), 1); // no report: one, never all of them
        for (cpus, want) in [
            (r#""4""#, 4),
            ("2", 2),
            (r#""0""#, 1),
            (r#""lots""#, 1),
            ("99999", 256),
        ] {
            std::fs::write(
                data.join(REPORT),
                format!(r#"{{"model":"m","cpus":{cpus}}}"#),
            )
            .unwrap();
            assert_eq!(threads(&data), want, "{cpus}");
        }
        let _ = std::fs::remove_dir_all(&data);
    }

    #[test]
    fn ollama_stopped_is_not_available_right_now() {
        // Nothing listens there: a port just taken and let go.
        let free = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .to_string();
        let why = collect(&free, &LIMITS).0.unwrap_err();
        assert!(why.contains("does not answer"), "{why}");
    }

    #[test]
    fn a_missing_model_says_it_is_not_there_yet() {
        let (address, _) = fake_ollama(Some(
            "HTTP/1.0 404 Not Found\r\n\r\n{\"error\":\"model 'qwen3.5:4b' not found\"}".into(),
        ));
        assert!(collect(&address, &LIMITS)
            .0
            .unwrap_err()
            .contains("not there yet"));
    }

    #[test]
    fn an_out_of_memory_model_is_said_as_such() {
        // The model's container was killed for its memory: Ollama says so, or the answer stops.
        let (address, _) = fake_ollama(Some("HTTP/1.0 500 Internal Server Error\r\n\r\n{\"error\":\"model runner has unexpectedly stopped, this may be due to resource limitations (out of memory)\"}".into()));
        assert!(collect(&address, &LIMITS)
            .0
            .unwrap_err()
            .contains("more memory"));
        let body = r#"{"message":{"content":"The "},"done":false}"#;
        let (address, _) = fake_ollama(Some(format!(
            "HTTP/1.0 200 OK\r\n\r\n{body}\n{{\"error\":\"signal: killed\"}}\n"
        )));
        assert!(collect(&address, &LIMITS)
            .0
            .unwrap_err()
            .contains("more memory"));
        // Killed by the kernel for the apps' sake, as on the 0.49.0 gate.
        let (address, _) = fake_ollama(Some(format!(
            "HTTP/1.0 200 OK\r\n\r\n{body}\n{{\"error\":\"an error was encountered while running the model: unexpected EOF\"}}\n"
        )));
        let why = collect(&address, &LIMITS).0.unwrap_err();
        assert!(why.contains("your apps needed the memory"), "{why}");
    }

    #[test]
    fn a_garbage_answer_is_only_text() {
        let body =
            "not json at all\n{\"message\":{\"content\":\"ACTION: rm -rf /\"},\"done\":true}\n";
        let (address, _) = fake_ollama(Some(format!("HTTP/1.0 200 OK\r\n\r\n{body}")));
        let answer = collect(&address, &LIMITS).0.unwrap();
        assert_eq!(
            suggested(&answer, &[("update_check".into(), String::new())]),
            None
        );
    }

    fn quick() -> Limits {
        Limits {
            first: Duration::from_millis(300),
            next: Duration::from_millis(300),
            total: Duration::from_secs(1),
            abandon: Duration::from_secs(10),
        }
    }

    #[test]
    fn a_model_too_slow_to_start_is_stopped() {
        let (address, _) = fake_ollama(None);
        let why = collect(&address, &quick()).0.unwrap_err();
        assert!(why.contains("too long"), "{why}");
    }

    /// The asks share one slot: these tests take it in turn.
    static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn wait_until(mut ended: impl FnMut() -> bool) {
        let start = Instant::now();
        while !ended() {
            assert!(start.elapsed() < Duration::from_secs(10), "it never ended");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn an_answer_is_read_back_by_its_owner_while_it_is_written() {
        let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
        let body = "{\"message\":{\"content\":\"Free some space.\\nACTION: update_check\"},\"done\":true}\n";
        let (address, _) = fake_ollama(Some(format!("HTTP/1.0 200 OK\r\n\r\n{body}")));
        let repairs = vec![("update_check".to_string(), String::new())];
        let id = begin(
            Turn::take().unwrap(),
            "token:abc".into(),
            address,
            "m".into(),
            2,
            json!([]),
            repairs,
            LIMITS,
        )
        .unwrap();
        assert_eq!(id.len(), 32);
        wait_until(|| progress(&id, "token:abc").unwrap()["done"] == true);
        let answer = progress(&id, "token:abc").unwrap();
        assert_eq!(answer["text"], "Free some space.\nACTION: update_check");
        assert_eq!(answer["repair"], "update_check");
        assert_eq!(answer["failed"], Value::Null);
        // Someone else's session, or an id never given, gets nothing.
        assert!(progress(&id, "token:other").is_none());
        assert!(progress(&id, "local").is_none());
        assert!(progress("0123456789abcdef0123456789abcdef", "token:abc").is_none());
        // The slot is free again once the answer has ended.
        wait_until(|| Turn::take().is_some());
    }

    #[test]
    fn a_poll_answers_at_once_while_the_model_is_slow() {
        // The web service's workers are held for at most a few seconds: a poll must never
        // wait for the model, even while it has not said a word.
        let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
        let (address, _) = fake_ollama(None);
        let id = begin(
            Turn::take().unwrap(),
            "local".into(),
            address,
            "m".into(),
            2,
            json!([]),
            vec![],
            quick(),
        )
        .unwrap();
        let asked = Instant::now();
        let first = progress(&id, "local").unwrap();
        assert!(asked.elapsed() < Duration::from_millis(200));
        assert_eq!(
            (first["done"].clone(), first["failed"].clone()),
            (json!(false), Value::Null)
        );
        // A second question while this one runs: refused.
        assert!(Turn::take().is_none());
        // The time limit ends it, said as "not available right now".
        wait_until(|| progress(&id, "local").unwrap()["failed"].is_string());
        let ended = progress(&id, "local").unwrap();
        assert!(
            ended["failed"].as_str().unwrap().starts_with(NOT_AVAILABLE),
            "{ended}"
        );
        wait_until(|| Turn::take().is_some());
    }

    #[test]
    fn a_closed_page_ends_the_answer_and_lets_the_model_go() {
        let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
        let (address, server) = fake_ollama(None);
        let limits = Limits {
            first: Duration::from_secs(5),
            next: Duration::from_secs(5),
            total: Duration::from_secs(5),
            abandon: Duration::from_millis(400),
        };
        let id = begin(
            Turn::take().unwrap(),
            "local".into(),
            address,
            "m".into(),
            2,
            json!([]),
            vec![],
            limits,
        )
        .unwrap();
        // Nobody polls: the ask ends long before its five seconds.
        let asked = Instant::now();
        wait_until(|| asks().get(&id).is_some_and(|a| a.ended.is_some()));
        assert!(
            asked.elapsed() < Duration::from_secs(3),
            "{:?}",
            asked.elapsed()
        );
        let why = asks().get(&id).and_then(|a| a.failed.clone()).unwrap();
        assert!(why.contains("stopped asking"), "{why}");
        // The connection to Ollama is closed: its stand-in reads the end at once.
        let _ = server.join();
        wait_until(|| Turn::take().is_some());
    }

    #[test]
    fn a_model_not_downloaded_says_so_and_never_downloading_for_ever() {
        let data = std::env::temp_dir().join(format!("neko-assist-models-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&data);
        let store = data.join(STORE);
        std::fs::create_dir_all(store.join("blobs")).unwrap();
        let none: Vec<String> = vec![];
        // Ollama has nothing and nothing is being written: not downloaded (cut off).
        let said = model_missing(&data, "qwen3.5:4b", Some(&none)).unwrap();
        assert!(
            said.contains("not downloaded yet") && said.contains("needs the internet"),
            "{said}"
        );
        // An unfinished download file written a moment ago: still downloading.
        std::fs::write(store.join("blobs/sha256-0123-partial"), b"x").unwrap();
        assert!(model_missing(&data, "qwen3.5:4b", Some(&none))
            .unwrap()
            .contains("still downloading"));
        // One left from a download that stopped long ago: not downloaded, never "downloading".
        let old = std::time::SystemTime::now() - Duration::from_secs(3600);
        std::fs::File::options()
            .write(true)
            .open(store.join("blobs/sha256-0123-partial"))
            .unwrap()
            .set_modified(old)
            .unwrap();
        assert!(model_missing(&data, "qwen3.5:4b", Some(&none))
            .unwrap()
            .contains("not downloaded yet"));
        // Ollama has it: nothing missing; Ollama not answering: the answer will say so.
        let have = vec!["qwen3.5:2b-q4_K_M".to_string(), "gemma4:latest".to_string()];
        assert_eq!(model_missing(&data, "qwen3.5:2b-q4_K_M", Some(&have)), None);
        assert_eq!(model_missing(&data, "gemma4", Some(&have)), None);
        assert!(model_missing(&data, "qwen3.5:4b", Some(&have)).is_some());
        assert_eq!(model_missing(&data, "qwen3.5:4b", None), None);
        let _ = std::fs::remove_dir_all(&data);
    }

    #[test]
    fn the_models_come_from_ollama_itself() {
        let body = r#"{"models":[{"name":"qwen3.5:2b-q4_K_M","model":"qwen3.5:2b-q4_K_M","size":1900000000}]}"#;
        let (address, _) = fake_ollama(Some(format!(
            "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{body}"
        )));
        assert_eq!(
            ollama_models(&address),
            Some(vec!["qwen3.5:2b-q4_K_M".to_string()])
        );
        let free = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .to_string();
        assert_eq!(ollama_models(&free), None);
    }

    #[test]
    fn one_question_at_a_time() {
        let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
        let first = Turn::take().expect("free at first");
        assert!(Turn::take().is_none());
        drop(first);
        assert!(Turn::take().is_some());
    }

    #[test]
    fn ollama_is_asked_on_this_pc_only() {
        assert!(OLLAMA.starts_with("127.0.0.1:"));
        let template = include_str!("../../compose/assistant.yaml");
        assert!(template.contains("\"127.0.0.1:11434:11434\""));
        assert_eq!(
            template.matches("11434:").count(),
            1,
            "published once, on 127.0.0.1 only"
        );
    }
}
