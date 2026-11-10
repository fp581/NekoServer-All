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
/// than `total`, ends it.
pub struct Limits {
    pub first: Duration,
    pub next: Duration,
    pub total: Duration,
}
pub const LIMITS: Limits = Limits {
    first: Duration::from_secs(150),
    next: Duration::from_secs(60),
    total: Duration::from_secs(300),
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
            // name=value, name: value and "name": "value" for names that say secret.
            (
                r#"(?i)\b([a-z0-9_.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|private[_-]?key|auth[a-z_]*|session|cookie)[a-z0-9_.-]*)("?\s*[=:]\s*"?)[^\s",;&]+"#,
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

/// NekoServer's own secrets, to take out of anything the model sees: every saved app
/// password and login, every value in an app's private Compose .env, and the settings
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
            if let Some((_, value)) = line.split_once('=') {
                found.push(value.trim().trim_matches('"').to_string());
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
    let kept: String = text.chars().rev().take(LOG_CHARS).collect();
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

/* ---------- the answer, streamed ---------- */

/// One question at a time: a second one waits for the first (the page says so).
static ASKING: AtomicBool = AtomicBool::new(false);

/// Holds the "one question at a time" slot while an answer is streamed.
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

/// Ask Ollama at `address` (always OLLAMA, a test's own) and pass the answer on to
/// `out` as it comes, one JSON line per piece: {"text": ...}, then {"done": true}; or
/// {"error": "Neko is not available right now: <why>"}. Returns the whole answer.
pub fn stream(
    address: &str,
    model: &str,
    messages: &Value,
    out: &mut dyn Write,
    limits: &Limits,
) -> std::result::Result<String, String> {
    let fail = |out: &mut dyn Write, why: String| {
        let _ = writeln!(
            out,
            "{}",
            json!({"error": format!("{NOT_AVAILABLE}: {why}")})
        );
        Err(why)
    };
    let body = json!({"model": model, "messages": messages, "stream": true,
        "options": {"num_predict": ANSWER_TOKENS}})
    .to_string();
    let Ok(at) = address.parse::<SocketAddr>() else {
        return fail(out, "its address is not one".into());
    };
    let Ok(mut ollama) = TcpStream::connect_timeout(&at, Duration::from_secs(3)) else {
        return fail(
            out,
            "the AI assistant does not answer (it may be stopped, or still starting)".into(),
        );
    };
    let _ = ollama.set_write_timeout(Some(Duration::from_secs(5)));
    let _ = ollama.set_read_timeout(Some(limits.first));
    // HTTP/1.0: Ollama then sends the answer as it comes and closes, without chunks.
    let request = format!("POST /api/chat HTTP/1.0\r\nHost: {address}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len());
    if ollama.write_all(request.as_bytes()).is_err() {
        return fail(out, "the AI assistant did not take the question".into());
    }
    let mut reader = BufReader::new(ollama.take(4_000_000));
    let mut line = String::new();
    if reader.read_line(&mut line).map_or(true, |n| n == 0) {
        return fail(
            out,
            "the AI assistant took too long to start answering".into(),
        );
    }
    let code = line.split_whitespace().nth(1).unwrap_or("").to_string();
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) | Err(_) => return fail(out, "the AI assistant stopped answering".into()),
            Ok(_) if line.trim().is_empty() => break,
            Ok(_) => {}
        }
    }
    if code != "200" {
        let mut text = String::new();
        let _ = reader.read_to_string(&mut text);
        let said = serde_json::from_str::<Value>(text.trim())
            .ok()
            .and_then(|v| v["error"].as_str().map(str::to_string))
            .unwrap_or_else(|| format!("it answered {code}"));
        return fail(out, plain_error(&said));
    }
    let started = Instant::now();
    let mut answer = String::new();
    loop {
        if started.elapsed() > limits.total {
            return fail(out, "the answer took too long, so it was stopped".into());
        }
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {}
            Err(_) => {
                return fail(
                    out,
                    "the AI assistant stopped answering (out of memory, perhaps)".into(),
                )
            }
        }
        let _ = reader
            .get_ref()
            .get_ref()
            .set_read_timeout(Some(limits.next));
        let Ok(piece) = serde_json::from_str::<Value>(line.trim()) else {
            continue;
        };
        if let Some(why) = piece["error"].as_str() {
            return fail(out, plain_error(why));
        }
        if let Some(text) = piece["message"]["content"].as_str() {
            answer.push_str(text);
            if writeln!(out, "{}", json!({"text": text})).is_err() {
                return Err("the page went away".into());
            }
        }
        if piece["done"] == true {
            break;
        }
    }
    let _ = writeln!(out, "{}", json!({"done": true}));
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
    } else {
        format!("it said \"{}\"", chars(&redact(said, &[])))
    }
}

/// The repair the answer's last ACTION line names, or None: only an exact id from the
/// list given (anything else in the answer is ignored).
pub fn suggested(answer: &str, offered: &[(String, String)]) -> Option<String> {
    let named = answer
        .lines()
        .rev()
        .find_map(|l| l.trim().strip_prefix("ACTION:"))?
        .trim()
        .trim_matches(|c| c == '`' || c == '"' || c == '.');
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
        let mut page = vec![];
        let answer = stream(
            &address,
            "qwen3.5:4b",
            &json!([{"role": "user", "content": "why"}]),
            &mut page,
            &LIMITS,
        )
        .unwrap();
        assert_eq!(answer, "The disk is full.\nACTION: update_check");
        let shown = String::from_utf8(page).unwrap();
        assert!(shown.contains(r#"{"text":"The disk "}"#), "{shown}");
        assert!(shown.trim_end().ends_with(r#"{"done":true}"#), "{shown}");
        let asked = server.join().unwrap();
        assert!(asked.starts_with("POST /api/chat HTTP/1.0"), "{asked}");
        assert!(asked.contains(r#""model":"qwen3.5:4b""#) && asked.contains(r#""stream":true"#));
    }

    #[test]
    fn ollama_stopped_is_not_available_right_now() {
        // Nothing listens there: a port just taken and let go.
        let free = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .to_string();
        let mut page = vec![];
        let why = stream(&free, "m", &json!([]), &mut page, &LIMITS).unwrap_err();
        assert!(why.contains("does not answer"), "{why}");
        assert!(String::from_utf8(page).unwrap().contains(NOT_AVAILABLE));
    }

    #[test]
    fn a_missing_model_says_it_is_not_there_yet() {
        let (address, _) = fake_ollama(Some(
            "HTTP/1.0 404 Not Found\r\n\r\n{\"error\":\"model 'qwen3.5:4b' not found\"}".into(),
        ));
        let mut page = vec![];
        let why = stream(&address, "qwen3.5:4b", &json!([]), &mut page, &LIMITS).unwrap_err();
        assert!(why.contains("not there yet"), "{why}");
    }

    #[test]
    fn an_out_of_memory_model_is_said_as_such() {
        // The model's container was killed for its memory: Ollama says so, or the answer stops.
        let (address, _) = fake_ollama(Some("HTTP/1.0 500 Internal Server Error\r\n\r\n{\"error\":\"model runner has unexpectedly stopped, this may be due to resource limitations (out of memory)\"}".into()));
        let mut page = vec![];
        assert!(stream(&address, "m", &json!([]), &mut page, &LIMITS)
            .unwrap_err()
            .contains("more memory"));
        let body = r#"{"message":{"content":"The "},"done":false}"#;
        let (address, _) = fake_ollama(Some(format!(
            "HTTP/1.0 200 OK\r\n\r\n{body}\n{{\"error\":\"signal: killed\"}}\n"
        )));
        let mut page = vec![];
        assert!(stream(&address, "m", &json!([]), &mut page, &LIMITS)
            .unwrap_err()
            .contains("more memory"));
    }

    #[test]
    fn a_garbage_answer_is_only_text() {
        let body =
            "not json at all\n{\"message\":{\"content\":\"ACTION: rm -rf /\"},\"done\":true}\n";
        let (address, _) = fake_ollama(Some(format!("HTTP/1.0 200 OK\r\n\r\n{body}")));
        let mut page = vec![];
        let answer = stream(&address, "m", &json!([]), &mut page, &LIMITS).unwrap();
        assert_eq!(
            suggested(&answer, &[("update_check".into(), String::new())]),
            None
        );
    }

    #[test]
    fn a_model_too_slow_to_start_is_stopped() {
        let (address, _) = fake_ollama(None);
        let mut page = vec![];
        let quick = Limits {
            first: Duration::from_millis(300),
            next: Duration::from_millis(300),
            total: Duration::from_secs(1),
        };
        let why = stream(&address, "m", &json!([]), &mut page, &quick).unwrap_err();
        assert!(why.contains("too long"), "{why}");
        assert!(String::from_utf8(page).unwrap().contains(NOT_AVAILABLE));
    }

    #[test]
    fn a_folder_path_and_a_service_name_are_kept() {
        let said = redact(
            "nekoserver-maintenance-resume.service could not write /mnt/nekoserver-media/library/movies/a-long-film-name",
            &[],
        );
        assert!(
            said.contains("/mnt/nekoserver-media/library/movies/a-long-film-name"),
            "{said}"
        );
        assert!(
            said.contains("nekoserver-maintenance-resume.service"),
            "{said}"
        );
        let key = redact("key 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a", &[]);
        assert!(!key.contains("3f9a8b7c"), "{key}");
    }

    #[test]
    fn one_question_at_a_time() {
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
