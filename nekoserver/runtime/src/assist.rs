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
/// token, secret or cookie; a password written out in a sentence ("my password is X");
/// "Bearer ..." credentials; Tailscale keys; a password in an address; e-mail
/// addresses; long random strings (keys, tokens, IDs); and each of
/// `known` (NekoServer's own secrets, see known_secrets) wherever it appears.
pub fn redact(text: &str, known: &[String]) -> String {
    let mut out = text.to_string();
    // The longest first: a secret that holds a shorter one (an app's login inside another
    // app's password) would otherwise be cut where the shorter one was, its rest left in.
    let mut known: Vec<&String> = known.iter().filter(|v| v.chars().count() >= 6).collect();
    known.sort_by_key(|v| std::cmp::Reverse(v.len()));
    for value in known {
        out = out.replace(value.as_str(), "<removed>");
    }
    for (pattern, with) in PATTERNS.iter() {
        out = pattern.replace_all(&out, *with).into_owned();
    }
    let out = said_in_words(&out);
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
/// A word that names a secret a person may write out in a sentence.
static SECRET_WORD: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
    regex::Regex::new(r"(?i)\b(?:pass(?:word|wd|phrase|code)s?|pin)\b").unwrap()
});
static WORDS: std::sync::LazyLock<regex::Regex> =
    std::sync::LazyLock::new(|| regex::Regex::new(r"\S+").unwrap());
/// Words that follow "the password is" (or "to") in ordinary sentences and log lines,
/// and so are not the secret itself, as in "is wrong", "is required", "to Jellyfin", "to log in".
/// Anything else there is taken out: a word too many removed costs little, a password
/// left in a report that is shared costs much.
#[rustfmt::skip]
const ORDINARY: &[&str] = &[
    "a", "an", "the", "my", "your", "our", "his", "her", "their", "its", "this", "that", "it",
    "not", "no", "never", "wrong", "incorrect", "invalid", "correct", "right", "valid",
    "required", "needed", "missing", "empty", "blank", "set", "unset", "saved", "stored", "kept",
    "shown", "hidden", "changed", "reset", "expired", "old", "new", "different", "same", "too",
    "very", "also", "still", "now", "there", "here", "in", "on", "at", "for", "to", "from", "by",
    "with", "of", "and", "or", "but", "if", "when", "what", "where", "which", "who", "why", "how",
    "refused", "rejected", "accepted", "denied", "working", "broken", "fine", "ok", "okay",
    "good", "bad", "weak", "strong", "short", "long", "unknown", "known", "lost", "forgotten",
    "gone", "being", "been", "asked", "used", "safe", "secure", "secret", "something", "nothing",
    "anything", "protected", "encrypted", "hashed", "written", "printed", "given", "sent",
    "removed", "log", "sign", "get", "access", "open", "enter", "continue", "login", "use",
    "jellyfin", "immich", "nextcloud", "navidrome", "sonarr", "radarr", "prowlarr", "seerr",
    "qbittorrent", "tailscale", "nekoserver", "neko", "dashboard", "server", "nas", "app",
    "apps", "account", "admin", "user", "root", "wifi", "router",
];

/// `text` without a password (passphrase, passcode, PIN) written out in a sentence:
/// "my password is X", "I changed the password to X", "the password to Jellyfin is X",
/// "the PIN was 1234". The settings form ("password: X", "password=X") is one of
/// PATTERNS. Ordinary sentences stay as they are ("the password is wrong").
fn said_in_words(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut done = 0;
    for name in SECRET_WORD.find_iter(text) {
        if name.start() < done {
            continue; // inside a value already taken out
        }
        // "pin" in small letters is also a verb ("pin to core 3"): only "PIN is ...".
        let noun_only = name.as_str() == "pin";
        let line = text[name.end()..]
            .find('\n')
            .unwrap_or(text.len() - name.end());
        let tail = &text[name.end()..name.end() + line];
        if let Some((from, to)) = value_in_words(tail, noun_only) {
            out.push_str(&text[done..name.end() + from]);
            out.push_str("<removed>");
            done = name.end() + to;
        }
    }
    out.push_str(&text[done..]);
    out
}

/// Where the secret's value is in the words after its name (`tail`, to the end of the
/// line), or None when what follows is an ordinary sentence.
fn value_in_words(tail: &str, noun_only: bool) -> Option<(usize, usize)> {
    let words: Vec<regex::Match> = WORDS.find_iter(tail).take(8).collect();
    // A word without its quotes and punctuation, in small letters.
    let plain = |i: usize| {
        words.get(i).map(|w| {
            w.as_str()
                .trim_matches(|c: char| !c.is_alphanumeric())
                .to_lowercase()
        })
    };
    // A word in quotation marks is a value, whatever it says.
    let quoted = |i: usize| {
        words
            .get(i)
            .is_some_and(|w| w.as_str().starts_with(['"', '\'', '\u{201c}']))
    };
    let ordinary = |i: usize| {
        !quoted(i) && plain(i).is_none_or(|w| w.is_empty() || ORDINARY.contains(&w.as_str()))
    };
    let is = |i: usize| plain(i).is_some_and(|w| matches!(w.as_str(), "is" | "was" | "are" | "as"));
    let first = plain(0)?;
    let mut at = if is(0) {
        1
    } else if !noun_only && matches!(first.as_str(), "for" | "to" | "of" | "on" | "in" | "at") {
        if first == "to" && !ordinary(1) {
            // "I changed the password to X". With a name nobody knows here ("the password
            // to plex is X") either word may be the password: both go.
            let (from, to) = whole_value(tail, &words, 1)?;
            let after = (is(2) && !ordinary(3))
                .then(|| whole_value(tail, &words, 3))
                .flatten();
            return Some((from, after.map_or(to, |(_, end)| end)));
        }
        // "the password to Jellyfin is X", "the password for the NAS was X"
        (1..=4).find(|i| is(*i))? + 1
    } else {
        return None;
    };
    while plain(at).is_some_and(|w| {
        matches!(
            w.as_str(),
            "now" | "still" | "currently" | "just" | "set" | "to"
        )
    }) {
        at += 1;
    }
    if ordinary(at) {
        return None;
    }
    whole_value(tail, &words, at)
}

/// The value that starts at word `at`: that word, or, when it opens a quotation that
/// closes later on the line, everything up to the closing mark (a passphrase of several
/// words).
fn whole_value(tail: &str, words: &[regex::Match], at: usize) -> Option<(usize, usize)> {
    let word = words.get(at)?;
    let mut end = word.end();
    let opens = word
        .as_str()
        .chars()
        .next()
        .filter(|c| matches!(c, '"' | '\'' | '\u{201c}'));
    if let Some(open) = opens {
        let close = if open == '\u{201c}' { '\u{201d}' } else { open };
        let closed_here = word.as_str()[open.len_utf8()..].contains(close);
        if let (false, Some(n)) = (closed_here, tail[word.end()..].find(close)) {
            end = word.end() + n + close.len_utf8();
        }
    }
    Some((word.start(), end))
}

/// Long strings that may be keys or tokens (never with "/", so a folder path stays).
static LONG: std::sync::LazyLock<regex::Regex> =
    std::sync::LazyLock::new(|| regex::Regex::new(r"\b[A-Za-z0-9+=_-]{32,}").unwrap());
static PATTERNS: std::sync::LazyLock<Vec<(regex::Regex, &str)>> = std::sync::LazyLock::new(|| {
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
            // random-string rule and NekoServer's own saved secrets. A value in quotes is
            // taken out up to its closing quote (or the line's end): a passphrase of several
            // words ("password: \"correct horse battery\"") lost only its first word before.
            (
                r#"(?i)\b([a-z0-9_.-]*(?:pass(?:word|wd|phrase)?|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)[a-z0-9_.-]*)("?\s*[=:]\s*)(?:"[^"\n]*"?|'[^'\n]*'?|\x{201c}[^\x{201d}\n]*\x{201d}?|[^\s",;&]+)"#,
                "$1$2<removed>",
            ),
            // A password named in another language (German, Spanish, French, Dutch,
            // Portuguese, Hebrew with its joined "the"/"and" prefixes, Russian). After ":" or
            // "=" any value goes; after the language's "is" only one with a digit or a symbol
            // in it: an ordinary word there ("Mein Passwort ist falsch?") is a sentence.
            (
                r#"(?i)\b((?:pass|kenn)wort|contrase(?:ñ|n)a|mot de passe|wachtwoord|senha|[הוש]?סיסמ[הא]|пароль)(\s*[:=]\s*(?:"[^"\n]*"?|'[^'\n]*'?|[^\s",;&]+)|\s+(?:ist|es|est|is|é|היא|הוא|זה|это)\s+[^\s",;&]*[\p{N}\p{S}@#%*_\-][^\s",;&]*)"#,
                "$1 <removed>",
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
});

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
    // Short waits (MODELS_WAIT in all): this is part of making a question ready, which
    // must end well inside the web service's wait for it (PREPARE).
    let step = MODELS_WAIT / 3; // to connect, to send the request, to read the answer
    let mut ollama = TcpStream::connect_timeout(&at, step).ok()?;
    let _ = ollama.set_read_timeout(Some(step));
    let _ = ollama.set_write_timeout(Some(step));
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

/// Said to the model in place of the readings when there are none (the collector has
/// not run, or its file cannot be read): never "nothing needs attention", which nobody
/// looked for.
pub const NO_READINGS: &str =
    "The dashboard's readings could not be read, so how the server is now is not known. \
Say that you cannot see the readings; do not say that nothing needs attention.";
/// Readings older than this are not "now" (the collector writes them every minute).
const FRESH_SECONDS: f64 = 300.0;
/// Readings stamped further ahead of the clock than this have no known age (the
/// collector's own rule for a saved time: collect.rs trusts none more than a minute ahead).
const AHEAD_SECONDS: f64 = 60.0;
/// The heading over readings whose time cannot be trusted.
pub const TIME_NOT_KNOWN: &str =
    "The dashboard's last readings (the time of the readings is not known, so the server may be different now; \
say that you do not know how old they are):";
/// Said in place of the attention list when the readings have none (a file from another
/// writer, or half a one): nobody looked, so it is not "nothing needs attention".
pub const NO_ATTENTION_LIST: &str =
    "What needs attention is not in these readings: do not say that nothing needs attention.";

/// How long ago, in the largest unit that fits: "12 minutes", "3 hours", "2 days".
fn ago(seconds: f64) -> String {
    let (n, unit) = match seconds {
        s if s < 7200.0 => ((s / 60.0).round(), "minute"),
        s if s < 172_800.0 => ((s / 3600.0).round(), "hour"),
        s => ((s / 86_400.0).round(), "day"),
    };
    format!("{n:.0} {unit}{}", if n == 1.0 { "" } else { "s" })
}

/// The readings for the model, under a line that says when they were taken: "now" only
/// while they are fresh; older ones say how old (the collector stopped, or the app SSD
/// was away), and missing ones say so (NO_READINGS). `now`: the time, in seconds.
pub fn summary(status: &Value, now: f64) -> String {
    let Some(taken) = status["generated_at"].as_f64() else {
        return NO_READINGS.into();
    };
    let age = now - taken;
    let heading = if age < -AHEAD_SECONDS {
        // Stamped later than now: a clock was wrong when they were taken, or is wrong
        // now. Either way their age cannot be worked out, so they are not "now".
        TIME_NOT_KNOWN.to_string()
    } else if age < FRESH_SECONDS {
        "The dashboard now:".to_string()
    } else {
        format!(
            "The dashboard as it was {} ago (its readings have not been updated since, so the server may be \
             different now; say how old they are):",
            ago(age)
        )
    };
    format!("{heading}\n{}", readings(status))
}

/// A short summary of the readings: what needs attention, the backups, the drives, the
/// apps. Plain lines, at most SUMMARY_CHARS characters.
fn readings(status: &Value) -> String {
    let rows = |key: &str| status[key].as_array().cloned().unwrap_or_default();
    let text = |v: &Value| v.as_str().unwrap_or("").to_string();
    let mut lines = vec![format!(
        "NekoServer {}",
        status["version"].as_str().unwrap_or("?")
    )];
    let attention = rows("alerts");
    if !status["alerts"].is_array() {
        lines.push(NO_ATTENTION_LIST.into());
    } else if attention.is_empty() {
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
        // The collector gives a drive's size and what is used (collect::drive_row);
        // what is free is the difference. "?" only when either is not known.
        let free = match (s["total"].as_f64(), s["used"].as_f64()) {
            (Some(total), Some(used)) => format!("{:.1} GB", (total - used).max(0.0) / 1e9),
            _ => "?".into(),
        };
        lines.push(format!(
            "Drive {}: {}, {free} free",
            text(&s["name"]),
            text(&s["status"]),
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

/// Making a question ready (is the model there, the log lines of what is asked about)
/// takes at most this long in all. The web service waits 15 seconds for it
/// (serve::CONTROL_WAIT): three commands of 15 seconds each could pass that, and the
/// page then said "unavailable" for a question that was ready a second later (the review
/// of 0.49.0). What is not in by then is left out, and the model is told so (LOG_LATE).
pub const PREPARE: Duration = Duration::from_secs(8);
/// The longest ollama_models may take (its connection, its request, its answer).
pub const MODELS_WAIT: Duration = Duration::from_secs(5);
/// Said to the model in place of a log that was not read in time.
pub const LOG_LATE: &str = "(Its log could not be read in time, so it is not shown here.)";

/// The last LOG_LINES lines of that log, as the system has them (journalctl for a
/// service, docker logs for an app's main container): "" when there is none, None when
/// it could not be read by `until`. Each command gets only the time that is left.
pub fn log_lines(source: &Source, until: Instant) -> Option<String> {
    log_lines_with(source, until, &common::probe)
}

/// A command run with a time limit in seconds: (its exit code, what it printed).
type Probe<'a> = &'a dyn Fn(&[&str], u64) -> (i32, String);

fn log_lines_with(source: &Source, until: Instant, probe: Probe) -> Option<String> {
    // One command, given the whole seconds left (a command's time limit is in seconds).
    // None when it is not in time: none left, so it is not run; or it ran until its time
    // limit ended it. That is told apart from a command that failed or printed nothing,
    // because a limit of whole seconds ends a moment before `until`, where a look at the
    // clock alone would call it "no log" (the review of the 0.50.0 fixes).
    let timed = |args: &[&str]| -> Option<(i32, String)> {
        let limit = until.saturating_duration_since(Instant::now()).as_secs();
        if limit == 0 {
            return None;
        }
        let started = Instant::now();
        let (code, text) = probe(args, limit);
        let ran_out = code != 0 && started.elapsed() >= Duration::from_secs(limit);
        (!ran_out).then_some((code, text))
    };
    let (code, text) = match source {
        Source::Unit(unit) => timed(&[
            "journalctl",
            "-u",
            unit,
            "-n",
            LOG_LINES,
            "-o",
            "cat",
            "--no-pager",
        ])?,
        Source::App(app) => {
            let service = apps::restart_plan(app).first().copied().unwrap_or("");
            let (_, id) = timed(&[
                "docker",
                "ps",
                "-aq",
                "--filter",
                &format!("label=com.docker.compose.project=neko-{app}"),
                "--filter",
                &format!("label=com.docker.compose.service={service}"),
            ])?;
            match id.split_whitespace().next() {
                Some(id) => timed(&["docker", "logs", "--tail", LOG_LINES, id])?,
                None => (1, String::new()),
            }
        }
        Source::None => (1, String::new()),
    };
    if code != 0 {
        // It failed in time, or there is no such container: no log.
        return Some(String::new());
    }
    Some(plain_log(&text))
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
    let mut user = format!("{}\n\n{}", redact(question, known), redact(summary, known));
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
    /// For "update_app": the signature of the version the card names ("" otherwise).
    /// Approve sends it back, and only that version is installed (approved_update).
    pub sig: String,
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
        sig: String::new(),
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
            let (version, sig) = waiting_update(data, app)
                .ok_or("No new version of that app is waiting; Check for updates looks again")?;
            Ok(Repair {
                sig,
                ..made(
                    format!("Updates {} to {version} now: a snapshot first, then the update, then a check; if the check fails, it is undone.", app_name(app)),
                    "The app is unavailable for a few minutes while it updates.",
                )
            })
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

/// Said when the version waiting is no longer the one the card described.
pub const NEWER_ARRIVED: &str =
    "A newer version arrived meanwhile, so nothing was installed; ask again";

/// The signature "update_app" passes to "Update now": the one the card showed (`shown`,
/// sent back with Approve), and only while that is still the version waiting (`now`,
/// checked again at Approve). The daily check can find a newer version between the card
/// and the press; that one was never described to the person, so it is not installed.
pub fn approved_update(now: &Repair, shown: &str) -> std::result::Result<String, &'static str> {
    if crate::updates::valid_signature(shown) && now.sig == shown {
        Ok(now.sig.clone())
    } else {
        Err(NEWER_ARRIVED)
    }
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
    ASKS.lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// A random id for an ask: 32 hex characters from the kernel's random numbers.
fn new_id() -> std::result::Result<String, String> {
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|e| format!("no random numbers ({e})"))?;
    use std::fmt::Write as _;
    Ok(bytes.iter().fold(String::new(), |mut out, b| {
        let _ = write!(out, "{b:02x}");
        out
    }))
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
        a.failed = Some(TOO_LONG.into());
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
/// Read piece by piece, so the limit holds while bytes come without a line's end too:
/// with read_until, an answer sent a byte at a time never let a read time out, and it
/// kept "one question at a time" taken long after the page said it was stopped (seen
/// with a stand-in assistant: every later question was "busy").
fn next_line(
    reader: &mut BufReader<std::io::Take<TcpStream>>,
    line: &mut Vec<u8>,
    limit: Duration,
    keep: &dyn Fn() -> bool,
) -> std::result::Result<usize, Waited> {
    let start = Instant::now();
    let mut asked = Instant::now();
    let mut added = 0;
    // After a read timed out, or about once a second while bytes keep coming.
    let mut ask_now = false;
    loop {
        if ask_now || asked.elapsed() >= KEEP_ASKED {
            asked = Instant::now();
            if !keep() {
                return Err(Waited::NotWanted);
            }
        }
        if start.elapsed() >= limit {
            return Err(Waited::TooLong);
        }
        let piece = match reader.fill_buf() {
            Ok(piece) => piece,
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                ask_now = true;
                continue;
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(Waited::Broken),
        };
        ask_now = false;
        if piece.is_empty() {
            return Ok(added); // the end: 0 when nothing more came
        }
        let (taken, ended) = match piece.iter().position(|b| *b == b'\n') {
            Some(at) => (at + 1, true),
            None => (piece.len(), false),
        };
        line.extend_from_slice(&piece[..taken]);
        reader.consume(taken);
        added += taken;
        if ended {
            return Ok(added);
        }
    }
}
/// How often `keep` is asked while a line is being read.
const KEEP_ASKED: Duration = Duration::from_secs(1);

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
    let tick = Duration::from_secs(1).min(limits.next).min(limits.first);
    let mut reader = asked_ollama(address, &body, limits, tick, keep)?;
    let mut line = vec![];
    let started = Instant::now();
    let mut asked = Instant::now();
    let mut answer = String::new();
    loop {
        if started.elapsed() > limits.total {
            return Err(TOO_LONG.into());
        }
        // A model that keeps printing never lets a read time out, which is where
        // next_line asks `keep`: so it is asked here too, about once a second, and an
        // answer nobody waits for is stopped while it streams (the review of 0.49.0).
        if asked.elapsed() >= tick {
            asked = Instant::now();
            if !keep() {
                return Err(why_none(Waited::NotWanted, TOO_LONG));
            }
        }
        line.clear();
        // The first words may take a while (the model is loaded first), the next ones less.
        let limit = if answer.is_empty() {
            limits.first
        } else {
            limits.next
        };
        match next_line(&mut reader, &mut line, limit, keep) {
            // Closed before Ollama said "done": the answer is not whole (its runner was
            // ended, or the connection dropped). What came so far is not passed off as it.
            Ok(0) => return Err(CUT_OFF.into()),
            Ok(_) => {}
            Err(why) => return Err(why_none(why, TOO_LONG)),
        }
        if answer_line(&line, &mut answer, piece)? {
            return Ok(answer);
        }
    }
}

/// Said when an answer passes its time limit (Limits.total, or a pause too long).
const TOO_LONG: &str = "the answer took too long, so it was stopped";
/// Said when the assistant closes the connection without a word (it was restarting, or
/// its runner ended).
const HUNG_UP: &str =
    "the AI assistant hung up without answering (it may be restarting); ask again in a minute";

/// Why no line came, in words; `slow` is what to say when the wait was too long.
fn why_none(why: Waited, slow: &str) -> String {
    match why {
        Waited::TooLong => slow.to_string(),
        Waited::NotWanted => "the page stopped asking for the answer, so it was stopped".into(),
        Waited::Broken => "the AI assistant stopped answering (out of memory, perhaps)".into(),
    }
}

/// The question sent to Ollama and its answer's head read: what is left to read is the
/// answer itself, line by line. An answer other than "200 OK" is why there is none.
fn asked_ollama(
    address: &str,
    body: &str,
    limits: &Limits,
    tick: Duration,
    keep: &dyn Fn() -> bool,
) -> std::result::Result<BufReader<std::io::Take<TcpStream>>, String> {
    let at = address
        .parse::<SocketAddr>()
        .map_err(|_| "its address is not one".to_string())?;
    let mut ollama = TcpStream::connect_timeout(&at, Duration::from_secs(3)).map_err(|_| {
        "the AI assistant does not answer (it may be stopped, or still starting)".to_string()
    })?;
    let _ = ollama.set_write_timeout(Some(Duration::from_secs(5)));
    let _ = ollama.set_read_timeout(Some(tick));
    // HTTP/1.0: Ollama then sends the answer as it comes and closes, without chunks.
    let request = format!("POST /api/chat HTTP/1.0\r\nHost: {address}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len());
    ollama
        .write_all(request.as_bytes())
        .map_err(|_| "the AI assistant did not take the question".to_string())?;
    let mut reader = BufReader::new(ollama.take(4_000_000));
    let mut line = vec![];
    let slow_start = "the AI assistant took too long to start answering";
    match next_line(&mut reader, &mut line, limits.first, keep) {
        // Hung up at once: not slow (seen with a stand-in assistant, it was said to be).
        Ok(0) => return Err(HUNG_UP.into()),
        Ok(_) => {}
        Err(why) => return Err(why_none(why, slow_start)),
    }
    let status = String::from_utf8_lossy(&line).to_string();
    let code = status.split_whitespace().nth(1).unwrap_or("");
    let stopped = "the AI assistant stopped answering";
    loop {
        line.clear();
        match next_line(&mut reader, &mut line, limits.next, keep) {
            Ok(0) => return Err(stopped.into()),
            Ok(_) if line.iter().all(u8::is_ascii_whitespace) => break,
            Ok(_) => {}
            Err(why) => return Err(why_none(why, stopped)),
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
    Ok(reader)
}

/// One line of the answer taken in: its words are added to `answer` and handed to
/// `piece`. True when it is the last line of a whole answer; an error line, or an
/// answer ended by its length limit, is why there is no whole answer.
fn answer_line(
    line: &[u8],
    answer: &mut String,
    piece: &mut dyn FnMut(&str),
) -> std::result::Result<bool, String> {
    let Ok(part) = serde_json::from_slice::<Value>(line.trim_ascii()) else {
        return Ok(false);
    };
    if let Some(why) = part["error"].as_str() {
        return Err(plain_error(why));
    }
    if let Some(text) = part["message"]["content"].as_str() {
        answer.push_str(text);
        piece(text);
    }
    // Ollama ends an answer that reached its token limit (num_predict) with "done": true
    // and "done_reason": "length": that answer stops in the middle of a sentence, so it
    // is not whole either.
    if part["done"] == true && part["done_reason"] == "length" {
        return Err(LENGTH_LIMIT.into());
    }
    Ok(part["done"] == true)
}

/// Said when the answer stopped before Ollama marked it as finished.
pub const CUT_OFF: &str = "the answer was cut off before it ended; ask again";

/// Said when the answer reached the length an answer may have (ANSWER_TOKENS) before the
/// model had finished it.
pub const LENGTH_LIMIT: &str =
    "the answer reached its length limit before it ended, so it is not whole; ask again about one thing at a time";

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

    /// The made-up secrets the tests plant. The ones shaped like a real key or token are
    /// put together here from pieces, so nothing in this file looks like one to a
    /// scanner; what is planted is the same as ever (checked in the test below).
    fn planted() -> [String; 9] {
        [
            "S3cret-Passw0rd!".to_string(),
            ["tskey", "-auth-", "kAbCdEf1CNTRL", "-0123456789abcdef"].concat(),
            "me@example.org".to_string(),
            [
                "eyJhbGciOi",
                "JIUzI1NiJ9",
                ".eyJzdWIiOiIx",
                "MjM0NTY3ODkwIn0",
                ".c2lnbmF0dXJl",
            ]
            .concat(),
            ["AKIA", "0123456789", "ABCDEFGHIJKLMNOPQRSTUV"].concat(),
            "hunter2hunter2".to_string(),
            "cookie-value-123".to_string(),
            "bearer-value-987654".to_string(),
            "envsecret-555".to_string(),
        ]
    }

    #[test]
    fn the_planted_values_are_shaped_like_the_real_things() {
        let p = planted();
        assert!(
            p[1].starts_with("tskey-auth-") && p[1].len() == 41,
            "{}",
            p[1]
        );
        assert!(p[3].starts_with("eyJ") && p[3].split('.').count() == 3 && p[3].len() == 61);
        assert!(p[4].starts_with("AKIA") && p[4].len() == 36, "{}", p[4]);
    }

    fn log_with_secrets() -> String {
        // Put together here, so no line of this file is shaped like a real token.
        let p = planted();
        let authorization = format!("Authorization: {} {}", "Bearer", p[7]);
        let tailscale = format!("tailscale up --auth-key={}", p[1]);
        let token = format!("token {}", p[3]);
        let aws = format!("AWS key {} refused", p[4]);
        [
            "Connecting to https://admin:S3cret-Passw0rd!@nas.local/share",
            tailscale.as_str(),
            "Mail sent to me@example.org",
            token.as_str(),
            aws.as_str(),
            "db login failed for neko with PASSWORD=hunter2hunter2",
            "Set-Cookie: session=cookie-value-123; Path=/",
            authorization.as_str(),
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
        for secret in planted() {
            assert!(
                !sent.contains(&secret),
                "{secret} reached the model: {sent}"
            );
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

    /// Review of the 0.50.0 fixes: the bug report said "passwords and keys removed", but
    /// a password written out in a sentence stayed in the question.
    /// Deep check plan 39: the redaction knew only English words for a password, so
    /// "Passwort: X" and "הסיסמה היא X" (Hebrew, "the password is X") reached the model.
    #[test]
    fn a_password_named_in_another_language_is_taken_out() {
        // Made up here from pieces: not a saved login, not shaped like a key.
        let word = ["Gelb", "-Katze-", "42"].concat();
        for typed in [
            format!("Passwort: {word}"),
            format!("das Kennwort ist {word}"),
            format!("la contraseña es {word}"),
            format!("mot de passe = {word}"),
            format!("het wachtwoord is {word}"),
            format!("a senha é {word}"),
            format!("הסיסמה היא {word}"),
            format!("סיסמה: {word}"),
            format!("пароль: {word}"),
        ] {
            let said = redact(&typed, &[]);
            assert!(!said.contains(&word), "{typed} -> {said}");
            assert!(said.contains("<removed>"), "{typed} -> {said}");
        }
        // The words alone, with nothing after them, stay.
        assert_eq!(
            redact("Mein Passwort ist falsch?", &[]),
            "Mein Passwort ist falsch?"
        );
    }
    #[test]
    fn a_password_written_in_a_sentence_is_taken_out() {
        // Made up here from pieces: not a saved login, not shaped like a key.
        let word = ["Blue", "-Horse-", "77"].concat();
        let digits = ["48", "15"].concat();
        let phrase = ["correct", "pony", "staple"].join(" ");
        let cases = [
            (
                format!("I changed the Jellyfin password to {word} and now it fails"),
                "I changed the Jellyfin password to <removed> and now it fails".to_string(),
            ),
            (
                format!("my password is {word}"),
                "my password is <removed>".into(),
            ),
            (
                format!("My password: {word}"),
                "My password: <removed>".into(),
            ),
            (
                format!("the password to Jellyfin is {word}, why is it refused?"),
                "the password to Jellyfin is <removed> why is it refused?".into(),
            ),
            (
                format!("The password for the admin account was {word}"),
                "The password for the admin account was <removed>".into(),
            ),
            (
                format!("the passphrase is now set to {word}"),
                "the passphrase is now set to <removed>".into(),
            ),
            (
                format!("the dashboard PIN is {digits} but it says wrong"),
                "the dashboard PIN is <removed> but it says wrong".into(),
            ),
            (
                format!("my passphrase is \"{phrase}\" and Immich refuses it"),
                "my passphrase is <removed> and Immich refuses it".into(),
            ),
            // The settings form with a passphrase in quotes: all of it goes, not only its
            // first word (seen asking the real service: "<removed> pony staple" stayed).
            (
                format!("the admin password: \"{phrase}\" fails"),
                "the admin password: <removed> fails".into(),
            ),
            (
                format!("{{\"db_password\": \"{phrase}\", \"user\": \"neko\"}}"),
                "{\"db_password\": <removed>, \"user\": \"neko\"}".into(),
            ),
            (format!("SMB_PASS='{phrase}'"), "SMB_PASS=<removed>".into()),
            (
                format!("the password to plex is {word}"),
                "the password to <removed>".into(),
            ),
            (
                format!("first line\nPasswords are {word}\nlast line"),
                "first line\nPasswords are <removed>\nlast line".into(),
            ),
        ];
        for (typed, wanted) in cases {
            let said = redact(&typed, &[]);
            assert_eq!(said, wanted, "{typed}");
            for secret in [&word, &digits, &phrase] {
                assert!(!said.contains(secret.as_str()), "{said}");
            }
        }
        // Ordinary sentences and log lines keep every word.
        for line in [
            "The password is wrong",
            "Why is my password not accepted?",
            "Jellyfin says the password is incorrect.",
            "the password to log in is refused",
            "Enter your password to continue",
            "the password for Jellyfin is not saved",
            "the password was changed yesterday",
            "Is the PIN needed on this PC too?",
            "taskset: pin to core 3",
            "password is required",
            "FATAL:  password authentication failed for user \"postgres\"",
        ] {
            assert_eq!(redact(line, &[]), line);
        }
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
    fn no_text_makes_the_redaction_panic() {
        // Whatever a person types or a log holds (Hebrew, emoji, quotes left open, the
        // words that start a value at the very end): the redaction never panics, and the
        // value after "password is" never stays in.
        let parts = [
            "password",
            "PIN",
            "pin",
            "is",
            "was",
            "to",
            "for",
            " ",
            "\n",
            "\"",
            "'",
            "\u{201c}",
            "\u{201d}",
            "שלום",
            "🐈",
            "é",
            "x",
            "Jellyfin",
            "=",
            ":",
            "passphrase",
            "now",
            "set",
            "the",
            "סיסמה",
        ];
        let mut seed: u64 = 0x5eed;
        for _ in 0..20_000 {
            let mut text = String::new();
            for _ in 0..(seed % 12) {
                seed = seed
                    .wrapping_mul(6364136223846793005)
                    .wrapping_add(1442695040888963407);
                text.push_str(parts[(seed >> 33) as usize % parts.len()]);
                if seed & 1 == 0 {
                    text.push(' ');
                }
            }
            let _ = redact(&text, &[]);
        }
        let said = redact("my password is קטן🐈סוד and more", &[]);
        assert!(!said.contains("קטן🐈סוד"), "{said}");
    }

    #[test]
    fn a_secret_that_holds_another_is_taken_out_whole() {
        // The review of 0.51.0: the shorter was replaced first and the longer's rest stayed.
        let known = ["Moss-Lark".to_string(), "Moss-Lark-Fern-Oak".to_string()];
        for order in [known.clone(), [known[1].clone(), known[0].clone()]] {
            let said = redact("signed in with Moss-Lark-Fern-Oak, then Moss-Lark", &order);
            assert_eq!(said, "signed in with <removed>, then <removed>");
        }
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
        // The stored login, put together here (nothing in this file is shaped like one).
        let stored = ["Pw-from", "-the-store-1"].concat();
        let mut login = json!({"username": "neko_ab12cd"});
        login[["pass", "word"].concat()] = json!(stored);
        std::fs::write(
            root.join("secrets/accounts/credentials.json"),
            json!({"format": 1, "apps": {"jellyfin": login}}).to_string(),
        )
        .unwrap();
        std::fs::write(
            root.join("compose/immich/.env"),
            "DB_PASSWORD=\"db-pass-from-env\"\nX=1\n",
        )
        .unwrap();
        let cfg = json!({"notifications": {"endpoint": "https://ntfy.sh/my-secret-topic"}});
        let known = known_secrets(&cfg, &root);
        assert_eq!(stored, "Pw-from-the-store-1");
        for want in [
            stored.as_str(),
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
            "storage": [{"name": "Media HDD", "status": "mounted", "total": 20_000_000_000u64, "used": 8_000_000_000u64}],
            "applications": [{"name": "Jellyfin", "state": "running", "health": "unhealthy"}]});
        let s = readings(&status);
        assert!(s.contains("NekoServer 0.49.0"));
        assert!(s.contains("Needs attention: Host and user snapshot failed (critical)"));
        assert!(s.contains("Backup Host and user snapshot: fail (No space left)"));
        assert!(s.contains("Drive Media HDD: mounted, 12.0 GB free"));
        assert!(s.contains("App Jellyfin: running, unhealthy"));
    }

    /// Review of 0.49.0: an unreadable status file became {} and the model was told
    /// "Nothing needs attention"; an old snapshot was handed over as "The dashboard now".
    #[test]
    fn missing_or_old_readings_are_never_passed_off_as_current() {
        let now = 1_800_000_000.0;
        // No readings (the file is missing, empty or not JSON: common::json gives {}).
        for none in [json!({}), json!(null), json!({"version": "0.50.0"})] {
            let s = summary(&none, now);
            assert_eq!(s, NO_READINGS, "{none}");
            assert!(!s.contains("Nothing needs attention.\n"));
        }
        let taken =
            |age: f64| json!({"version": "0.50.0", "generated_at": now - age, "alerts": []});
        // Fresh: "now", and with nothing in the list, nothing needs attention.
        let fresh = summary(&taken(40.0), now);
        assert!(
            fresh.starts_with("The dashboard now:\nNekoServer 0.50.0\nNothing needs attention."),
            "{fresh}"
        );
        // Old: how old, never "now".
        for (age, said) in [
            (20.0 * 60.0, "20 minutes"),
            (3.0 * 3600.0, "3 hours"),
            (2.0 * 86_400.0, "2 days"),
            (3600.0, "60 minutes"),
        ] {
            let old = summary(&taken(age), now);
            assert!(
                old.starts_with(&format!("The dashboard as it was {said} ago")),
                "{old}"
            );
            assert!(!old.contains("The dashboard now"), "{old}");
        }
        // A few seconds ahead of the clock (two clocks read a moment apart) is still now.
        assert!(summary(&taken(-30.0), now).starts_with("The dashboard now:"));
        // The model is handed the summary's own heading, not a fixed "now".
        let m = messages("why", &summary(&taken(7200.0), now), None, &[], &[]);
        let user = m[1]["content"].as_str().unwrap();
        assert!(
            user.contains("as it was 2 hours ago") && !user.contains("The dashboard now"),
            "{user}"
        );
    }

    /// Review of the 0.50.0 fixes: readings stamped in the future read as "now" for
    /// ever, and readings without an "alerts" list said "Nothing needs attention".
    #[test]
    fn readings_from_the_future_or_without_a_list_are_not_passed_off_as_sound() {
        let now = 1_800_000_000.0;
        let taken =
            |ahead: f64| json!({"version": "0.50.0", "generated_at": now + ahead, "alerts": []});
        // A day ahead (the clock was wrong when the collector last ran, and it has not
        // run since): the time is not known, never "now" and never an age.
        for ahead in [61.0, 3600.0, 86_400.0] {
            let s = summary(&taken(ahead), now);
            assert!(s.starts_with(TIME_NOT_KNOWN), "{ahead}: {s}");
            assert!(s.contains("the time of the readings is not known"), "{s}");
            assert!(
                !s.contains("The dashboard now") && !s.contains(" ago"),
                "{s}"
            );
            // The readings themselves are still handed over.
            assert!(
                s.contains("NekoServer 0.50.0\nNothing needs attention."),
                "{s}"
            );
        }
        assert!(summary(&taken(59.0), now).starts_with("The dashboard now:"));
        // No "alerts" list at all (not the collector's own file, or half of one): what
        // needs attention is not known, which is not "nothing".
        for odd in [
            json!({"generated_at": now}),
            json!({"generated_at": now, "alerts": null}),
            json!({"generated_at": now, "alerts": "none"}),
        ] {
            let s = summary(&odd, now);
            assert!(s.contains(NO_ATTENTION_LIST), "{odd}: {s}");
            assert!(!s.contains("Nothing needs attention."), "{odd}: {s}");
        }
        // An empty list is the collector saying that nothing does.
        let none = summary(&json!({"generated_at": now, "alerts": []}), now);
        assert!(none.contains("Nothing needs attention.") && !none.contains(NO_ATTENTION_LIST));
    }

    /// Review of 0.49.0: the summary read a drive's "free", which the collector never
    /// gives (it gives "total" and "used"), so every drive was "? free".
    #[test]
    fn a_drives_free_space_is_worked_out_from_the_collectors_own_row() {
        // The row as the collector makes it, filled in as it fills a mounted drive.
        let mut row = collect::drive_row(
            "media_hdd",
            "Media HDD",
            Path::new("/mnt/nekoserver-media"),
            &json!("mounted"),
        );
        assert!(row.get("total").is_some() && row.get("used").is_some());
        assert!(row.get("free").is_none(), "the collector gives no \"free\"");
        let not_mounted = readings(&json!({"storage": [row.clone()]}));
        assert!(
            not_mounted.contains("Drive Media HDD: mounted, ? free"),
            "{not_mounted}"
        );
        row["total"] = json!(2_000_000_000_000u64);
        row["used"] = json!(1_487_500_000_000u64);
        let s = readings(&json!({"storage": [row]}));
        assert!(s.contains("Drive Media HDD: mounted, 512.5 GB free"), "{s}");
        // A reading that makes no sense (more used than there is) is never a negative.
        let odd =
            json!({"storage": [{"name": "SSD", "status": "mounted", "total": 10, "used": 20}]});
        assert!(readings(&odd).contains("Drive SSD: mounted, 0.0 GB free"));
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

    /// Review of 0.49.0: Approve took the newest version waiting when it was pressed,
    /// not the one the card had named.
    #[test]
    fn approve_installs_only_the_version_the_card_showed() {
        let (cfg, status) = installed();
        let data = std::env::temp_dir().join(format!("neko-assist-update-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&data);
        let waiting = |versions: &[(&str, &str)]| {
            let list: Vec<Value> = versions
                .iter()
                .map(|(version, sig)| json!({"version": version, "sig": sig}))
                .collect();
            crate::updates::save_pending(
                &data,
                &json!({"format": "neko-pending-updates-v1", "apps": {"jellyfin": list}}),
            )
            .unwrap();
        };
        let (first, second) = ("a".repeat(16), "b".repeat(16));
        waiting(&[("10.9.1", &first)]);
        let card = repair(&cfg, &status, &data, "update_app:jellyfin").unwrap();
        assert!(card.does.contains("to 10.9.1 now"), "{card:?}");
        assert_eq!(card.sig, first);
        // Unchanged between the card and Approve: that version is installed.
        let now = repair(&cfg, &status, &data, "update_app:jellyfin").unwrap();
        assert_eq!(approved_update(&now, &card.sig), Ok(first.clone()));
        // The daily check found a newer one meanwhile: refused, nothing is installed.
        waiting(&[("10.9.1", &first), ("10.9.2", &second)]);
        let now = repair(&cfg, &status, &data, "update_app:jellyfin").unwrap();
        assert_eq!(now.sig, second);
        assert_eq!(approved_update(&now, &card.sig), Err(NEWER_ARRIVED));
        // A press that names no version (an older page) installs nothing either.
        assert_eq!(approved_update(&now, ""), Err(NEWER_ARRIVED));
        // The other repairs have no target that can change, and carry no signature.
        assert_eq!(
            repair(&cfg, &status, &data, "restart_app:jellyfin")
                .unwrap()
                .sig,
            ""
        );
        let _ = std::fs::remove_dir_all(&data);
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

    /// Review of 0.49.0: making a question ready ran up to three commands of 15 seconds
    /// each, past the web service's 15-second wait: "unavailable" at 15 s, ready at 16 s.
    #[test]
    fn making_a_question_ready_ends_inside_the_web_services_wait() {
        // Everything it may take, with room to spare under the web service's wait.
        assert!(PREPARE + MODELS_WAIT + Duration::from_secs(1) < crate::serve::CONTROL_WAIT);
        let app = Source::App("jellyfin".into());
        let given = std::cell::RefCell::new(vec![]);
        // docker answers after `slow`; each call notes the time limit it was given.
        let docker = |slow: Duration| {
            let given = &given;
            move |args: &[&str], limit: u64| {
                given.borrow_mut().push(limit);
                std::thread::sleep(slow);
                match args[1] {
                    "ps" => (0, "abc123\n".to_string()),
                    _ => (0, "line one\nline two\n".to_string()),
                }
            }
        };
        // In time: both commands run, each with no more than what is left.
        let until = Instant::now() + PREPARE;
        let read = log_lines_with(&app, until, &docker(Duration::ZERO));
        assert_eq!(
            read.as_deref().map(str::trim_end),
            Some("line one\nline two")
        );
        assert!(
            given
                .borrow()
                .iter()
                .all(|limit| *limit <= PREPARE.as_secs()),
            "{given:?}"
        );
        assert_eq!(given.borrow().len(), 2);
        // A slow first command uses the time up: the second is not started at all, and
        // the log is "not in time" (None), never an empty log passed off as one.
        given.borrow_mut().clear();
        let until = Instant::now() + Duration::from_millis(2500);
        let started = Instant::now();
        let read = log_lines_with(&app, until, &docker(Duration::from_millis(1700)));
        assert_eq!(read, None);
        assert_eq!(
            *given.borrow(),
            [2],
            "one command, given the 2 whole seconds left"
        );
        assert!(started.elapsed() < Duration::from_millis(2500));
        // No time left: nothing is run.
        given.borrow_mut().clear();
        assert_eq!(
            log_lines_with(&app, Instant::now(), &docker(Duration::ZERO)),
            None
        );
        assert_eq!(
            log_lines_with(
                &Source::Unit("x.service".into()),
                Instant::now(),
                &docker(Duration::ZERO)
            ),
            None
        );
        assert!(given.borrow().is_empty());
        // A command that failed in time is "no log", as before.
        let failed = |_: &[&str], _: u64| (1, String::new());
        let later = Instant::now() + PREPARE;
        assert_eq!(
            log_lines_with(&Source::Unit("x.service".into()), later, &failed).as_deref(),
            Some("")
        );
        // No such container (docker ps prints nothing) is "no log" too.
        let none = |_: &[&str], _: u64| (0, String::new());
        assert_eq!(log_lines_with(&app, later, &none).as_deref(), Some(""));
        // The control service starts the clock before it asks Ollama for its models (what
        // it tells the model about a late log is run in control.rs's own test).
        let control = include_str!("control.rs");
        let asks = &control[control.find("fn assistant(").unwrap()..];
        assert!(
            asks.find("Instant::now() + assist::PREPARE").unwrap()
                < asks.find("assist::ollama_models").unwrap()
        );
    }

    /// Review of the 0.50.0 fixes: a command ended by its time limit (whole seconds, so
    /// a moment before the deadline) was passed off as "no log", and the model was not
    /// told that the log is missing.
    #[test]
    fn a_log_command_that_runs_out_of_time_is_late_not_empty() {
        // As common::probe ends a command: after its limit, with a failure and no output.
        let hangs = |_: &[&str], limit: u64| {
            std::thread::sleep(Duration::from_secs(limit));
            (-1, String::new())
        };
        // 1.6 s left: the command gets 1 s and is ended 0.6 s before the deadline.
        let soon = || Instant::now() + Duration::from_millis(1600);
        let until = soon();
        assert_eq!(
            log_lines_with(&Source::Unit("x.service".into()), until, &hangs),
            None
        );
        assert!(Instant::now() < until, "ended before the deadline");
        // docker ps hanging is the same: not "no such container".
        assert_eq!(
            log_lines_with(&Source::App("jellyfin".into()), soon(), &hangs),
            None
        );
        // docker ps answers, docker logs hangs: late as well.
        let logs_hang = |args: &[&str], limit: u64| match args[1] {
            "ps" => (0, "abc123\n".to_string()),
            _ => hangs(args, limit),
        };
        assert_eq!(
            log_lines_with(&Source::App("jellyfin".into()), soon(), &logs_hang),
            None
        );
    }

    /// Review of 0.49.0: a connection closed before "done": true gave Ok(answer), so half
    /// an answer was shown as a whole one, and a repair could be read from it.
    #[test]
    fn an_answer_that_closes_before_done_is_cut_off_not_complete() {
        let body = [
            r#"{"message":{"content":"Restart Jellyfin.\nACTION: restart_app:jellyfin"},"done":false}"#,
            r#"{"message":{"content":" It has"},"done":false}"#,
        ]
        .join("\n");
        let (address, _) = fake_ollama(Some(format!(
            "HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\n\r\n{body}\n"
        )));
        let (answer, pieces) = collect(&address, &LIMITS);
        assert_eq!(answer.unwrap_err(), CUT_OFF);
        // The page still gets what came, to show it marked as cut off.
        assert_eq!(
            pieces.concat(),
            "Restart Jellyfin.\nACTION: restart_app:jellyfin It has"
        );
    }

    /// The same answer as its page gets it: failed, not done, and no repair, though its
    /// text names one that is on offer.
    #[test]
    fn a_cut_off_answer_offers_no_repair() {
        let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
        let said = "Restart Jellyfin.\nACTION: restart_app:jellyfin";
        let body = json!({"message": {"content": said}, "done": false});
        let (address, _) = fake_ollama(Some(format!("HTTP/1.0 200 OK\r\n\r\n{body}\n")));
        let offered = vec![("restart_app:jellyfin".to_string(), String::new())];
        // The text does name a repair on offer: a whole answer would have set it.
        assert_eq!(
            suggested(said, &offered).as_deref(),
            Some("restart_app:jellyfin")
        );
        let id = begin(
            Turn::take().unwrap(),
            "local".into(),
            address,
            "m".into(),
            2,
            json!([]),
            offered,
            LIMITS,
        )
        .unwrap();
        wait_until(|| asks().get(&id).is_some_and(|a| a.ended.is_some()));
        wait_until(|| Turn::take().is_some());
        let cut = progress(&id, "local").unwrap();
        assert_eq!(
            cut["failed"],
            format!("{NOT_AVAILABLE}: {CUT_OFF}"),
            "{cut}"
        );
        assert_eq!(cut["done"], false, "{cut}");
        assert_eq!(cut["repair"], Value::Null, "{cut}");
        assert_eq!(cut["text"], said);
    }

    /// Review of 0.49.0: `keep` was asked only when a read timed out, so an answer that
    /// streamed steadily could not be stopped by the page leaving.
    #[test]
    fn a_steadily_streaming_answer_stops_when_the_page_leaves() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let server = std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut got = vec![0u8; 65536];
            let _ = s.read(&mut got);
            let _ = s.write_all(b"HTTP/1.0 200 OK\r\n\r\n");
            // A word every 20 ms for 6 seconds, unless the other side hangs up first.
            for sent in 0..300 {
                let line = "{\"message\":{\"content\":\"word \"},\"done\":false}\n";
                if s.write_all(line.as_bytes()).is_err() {
                    return sent;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            300
        });
        let limits = Limits {
            first: Duration::from_secs(5),
            next: Duration::from_secs(5),
            total: Duration::from_secs(30),
            abandon: Duration::from_secs(1),
        };
        let start = Instant::now();
        let leaves = Duration::from_millis(400);
        let mut pieces = 0;
        let result = stream(
            &address,
            "qwen3.5:4b",
            2,
            &json!([{"role": "user", "content": "why"}]),
            &limits,
            &|| start.elapsed() < leaves,
            &mut |_| pieces += 1,
        );
        let took = start.elapsed();
        assert!(
            result.unwrap_err().contains("stopped asking"),
            "took {took:?}"
        );
        assert!(pieces > 5, "it was streaming: {pieces}");
        // Within about a second of the page leaving, not after the whole answer.
        assert!(took < leaves + Duration::from_millis(1800), "{took:?}");
        assert!(server.join().unwrap() < 300, "Ollama's side was hung up on");
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

    /// Seen asking the real service with a stand-in assistant that sent its answer a byte
    /// at a time with no line's end: no read ever timed out, so its time limit was never
    /// looked at and the answer held "one question at a time" until the stand-in stopped.
    #[test]
    fn an_answer_that_never_ends_its_line_is_stopped_in_time() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let _ = s.read(&mut [0u8; 65536]);
            let _ = s.write_all(b"HTTP/1.0 200 OK\r\n\r\n");
            // A byte every 50 ms for 10 s: faster than any read's time limit.
            for _ in 0..200 {
                if s.write_all(b"x").is_err() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        });
        let limits = Limits {
            first: Duration::from_secs(1),
            next: Duration::from_secs(1),
            total: Duration::from_secs(2),
            abandon: Duration::from_secs(60),
        };
        let start = Instant::now();
        let result = stream(&address, "m", 1, &json!([]), &limits, &|| true, &mut |_| {});
        let took = start.elapsed();
        assert!(result.unwrap_err().contains("too long"), "took {took:?}");
        assert!(took < Duration::from_secs(4), "{took:?}");
    }

    /// Seen with a stand-in assistant that closed the connection at once: Neko said it
    /// "took too long to start answering", though it had answered nothing in no time.
    #[test]
    fn an_assistant_that_hangs_up_at_once_is_not_called_slow() {
        let (address, _) = fake_ollama(Some(String::new()));
        let why = collect(&address, &LIMITS).0.unwrap_err();
        assert!(why.contains("hung up without answering"), "{why}");
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

    /// Review of the 0.50.0 fixes: an answer that Ollama ended at its token limit
    /// ("done": true, "done_reason": "length") counted as whole: a repair was read from
    /// it and a bug report made from half a report.
    #[test]
    fn an_answer_ended_by_the_length_limit_is_not_whole() {
        let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
        let ended = |reason: &str| {
            let body = [
                r#"{"message":{"content":"Look for updates.\nACTION: update_check\nBUG REPORT: the retention st"},"done":false}"#.to_string(),
                format!(r#"{{"message":{{"content":""}},"done":true,"done_reason":"{reason}"}}"#),
            ]
            .join("\n");
            let (address, _) = fake_ollama(Some(format!("HTTP/1.0 200 OK\r\n\r\n{body}\n")));
            let id = begin(
                Turn::take().unwrap(),
                "local".into(),
                address,
                "m".into(),
                2,
                json!([]),
                vec![("update_check".to_string(), String::new())],
                LIMITS,
            )
            .unwrap();
            wait_until(|| asks().get(&id).is_some_and(|a| a.ended.is_some()));
            wait_until(|| Turn::take().is_some());
            progress(&id, "local").unwrap()
        };
        // Ended by the limit: failed, said why, not done, and no repair read from it.
        let cut = ended("length");
        assert_eq!(cut["done"], false, "{cut}");
        assert_eq!(cut["repair"], Value::Null, "{cut}");
        assert_eq!(
            cut["failed"],
            format!("{NOT_AVAILABLE}: {LENGTH_LIMIT}"),
            "{cut}"
        );
        // The page still gets what came, to show it marked as cut off.
        assert!(
            cut["text"].as_str().unwrap().ends_with("the retention st"),
            "{cut}"
        );
        // The model's own end is a whole answer, and its repair is offered.
        let whole = ended("stop");
        assert_eq!(whole["done"], true, "{whole}");
        assert_eq!(whole["repair"], "update_check", "{whole}");
        assert_eq!(whole["failed"], Value::Null, "{whole}");
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
