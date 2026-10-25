//! The dashboard's web server: unprivileged and loopback only.
//!
//! It serves the page, the collector's latest snapshot (`/api/status`), live
//! hardware readings (`/api/live`) and the process list (`/api/processes`). Fan and
//! lighting changes and app controls (`/api/control`) are passed to the root control
//! service over its private socket; this server never writes to hardware, stops a
//! program or talks to Docker itself. Anything
//! that goes wrong is answered with a short code and a plain sentence (see ERRORS),
//! which the page shows as is.
use crate::{
    common::{self, Error, Result},
    live::Live,
    Args,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    sync::{mpsc, Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
const CSP:&str="default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; object-src 'none'";

/// Every error the dashboard server answers with. Short, stable codes: the page and
/// DASHBOARD.md explain each one (E01/E02/E12 are detected by the page itself).
pub const ERRORS: &[(&str, u16, &str)] = &[
    ("E10", 503, "Readings not ready yet"),
    ("E11", 503, "Readings could not be read"),
    ("E20", 503, "Live readings unavailable"),
    ("E21", 503, "Fan and light control unavailable"),
    ("E40", 404, "Page not found"),
    ("E41", 403, "Open the dashboard from its own address"),
    ("E42", 400, "Request not supported"),
    ("E43", 403, "Enter your PIN to change this"),
    ("E44", 403, "Wrong PIN"),
    ("E45", 429, "Too many tries; wait a while"),
    ("E46", 503, "The change could not be saved"),
    ("E47", 409, "Not now: something else is using it"),
    ("E48", 403, "Protected: can't be stopped from here"),
];
/// The HTTP status for a coded answer from the control service.
fn status_of(code: &str) -> u16 {
    ERRORS
        .iter()
        .find(|e| e.0 == code)
        .map(|e| e.1)
        .unwrap_or(503)
}
fn error_entry(code: &str) -> (&'static str, u16, &'static str) {
    ERRORS
        .iter()
        .find(|e| e.0 == code)
        .copied()
        .unwrap_or(ERRORS[0])
}
/// The short sentence for a code (the same one the page and DASHBOARD.md show).
pub fn error_message(code: &str) -> &'static str {
    error_entry(code).2
}
fn error_body(code: &str) -> (u16, Vec<u8>) {
    let (code, status, message) = error_entry(code);
    (
        status,
        serde_json::to_vec(&json!({"error": code, "message": message})).unwrap(),
    )
}

/// Files the page is made of: (path, file, type).
const ASSETS: &[(&str, &str, &str)] = &[
    ("/", "index.html", "text/html; charset=utf-8"),
    ("/index.html", "index.html", "text/html; charset=utf-8"),
    ("/style.css", "style.css", "text/css; charset=utf-8"),
    ("/app.js", "app.js", "text/javascript; charset=utf-8"),
    ("/sw.js", "sw.js", "text/javascript; charset=utf-8"),
    ("/logo.svg", "logo.svg", "image/svg+xml"),
    (
        "/manifest.webmanifest",
        "manifest.webmanifest",
        "application/manifest+json",
    ),
    ("/icon-192.png", "icon-192.png", "image/png"),
    ("/icon-512.png", "icon-512.png", "image/png"),
    ("/apple-touch-icon.png", "apple-touch-icon.png", "image/png"),
];
#[cfg(test)]
fn asset_files() -> Vec<&'static str> {
    let mut files = ASSETS.iter().map(|a| a.1).collect::<Vec<_>>();
    files.dedup();
    files
}

/// The standard reason phrase for each status the server answers with.
fn reason(code: u16) -> &'static str {
    match code {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        429 => "Too Many Requests",
        409 => "Conflict",
        431 => "Request Header Fields Too Large",
        _ => "Service Unavailable",
    }
}
fn reply(stream: &mut TcpStream, code: u16, body: &[u8], mime: &str, head: bool) {
    let reason = reason(code);
    // Pages are checked with the server each time (no-cache); readings never stored.
    let cache = if mime.starts_with("application/json") {
        "no-store"
    } else {
        "no-cache"
    };
    // The server's own clock, so the page judges how old readings are by it rather than
    // by a phone whose clock may be off.
    let time = common::now();
    let header=format!("HTTP/1.1 {code} {reason}\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nCache-Control: {cache}\r\nContent-Security-Policy: {CSP}\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nX-Server-Time: {time:.3}\r\nConnection: close\r\n\r\n",body.len());
    let _ = stream.write_all(header.as_bytes());
    if !head {
        let _ = stream.write_all(body);
    }
}
fn reply_error(stream: &mut TcpStream, code: &str, head: bool) {
    let (status, body) = error_body(code);
    reply(stream, status, &body, "application/json", head);
}
/// The dashboard's private Tailscale address (Tailscale Serve, never Funnel), as
/// `https://<public>:PUBLIC_PORT`. Tailscale proxies it to this loopback server.
pub const PUBLIC_PORT: u16 = 8448;
/// A machine name on Tailscale's own domain; anything else is refused.
fn valid_public(name: &str) -> bool {
    name.len() < 254
        && name.ends_with(".ts.net")
        && name.split('.').count() >= 3
        && name.split('.').all(|l| {
            !l.is_empty()
                && l.len() <= 63
                && l.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
}
/// Host and Origin values a request may carry: the loopback names, plus the private
/// Tailscale address when one is configured.
fn allowed(port: u16, public: Option<&str>) -> (Vec<String>, Vec<String>) {
    let mut hosts = vec![format!("127.0.0.1:{port}"), format!("localhost:{port}")];
    let mut origins: Vec<String> = hosts.iter().map(|h| format!("http://{h}")).collect();
    if let Some(name) = public {
        // Tailscale passes the browser's Host, with or without the port.
        hosts.push(format!("{name}:{PUBLIC_PORT}"));
        hosts.push(name.to_string());
        origins.push(format!("https://{name}:{PUBLIC_PORT}"));
    }
    (hosts, origins)
}

/// Largest request body accepted (fan curves and colours are small).
const MAX_BODY: usize = 4096;

/// Ask the control service (one JSON line each way).
fn ask_control(socket: &Path, request: &Value) -> std::result::Result<Value, &'static str> {
    let mut stream = UnixStream::connect(socket).map_err(|_| "E21")?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    writeln!(stream, "{request}").map_err(|_| "E21")?;
    let mut line = String::new();
    BufReader::new(stream.take(4_000_000))
        .read_line(&mut line)
        .map_err(|_| "E21")?;
    serde_json::from_str::<Value>(&line)
        .ok()
        .filter(Value::is_object)
        .ok_or("E21")
}

/// The request the control service gets: only the fields it knows, plus who is asking.
/// `local` is decided here, never taken from the page.
pub fn control_request(op: &str, body: &Value, who: &Who, token: Option<&str>) -> Value {
    let mut request = json!({"op": op, "local": who.local});
    for key in [
        "pin",
        "output",
        "setting",
        "device",
        "color",
        "brightness",
        "effect",
        "off",
        "app",
        "pid",
        "start",
    ] {
        if let Some(v) = body.get(key) {
            request[key] = v.clone();
        }
    }
    if let Some(t) = token {
        request["token"] = json!(t);
    }
    if let Some(source) = &who.source {
        request["source"] = json!(source);
    }
    request
}

/// Who is asking, as this server determined it from the connection and the headers
/// Tailscale Serve adds (it removes any the browser sent), never from the page.
#[derive(Default)]
pub struct Who {
    /// The server itself.
    pub local: bool,
    /// Another device: its Tailscale login, or else its tailnet address. Wrong PINs are
    /// counted per source, so one device can't lock out the others.
    pub source: Option<String>,
}
fn printable(v: &[u8]) -> Option<&str> {
    std::str::from_utf8(v)
        .ok()
        .map(str::trim)
        .filter(|v| !v.is_empty() && v.len() <= 200 && v.bytes().all(|b| b.is_ascii_graphic()))
}
/// The source of a forwarded request: the login Tailscale Serve names, or else the last
/// X-Forwarded-For address (the one the proxy itself added).
fn source_of(login: &[&[u8]], forwarded_for: &[&[u8]]) -> Option<String> {
    if let [one] = login {
        if let Some(login) = printable(one) {
            return Some(format!("login:{login}"));
        }
    }
    let last = forwarded_for.last()?;
    let address = std::str::from_utf8(last).ok()?.rsplit(',').next()?.trim();
    address
        .parse::<std::net::IpAddr>()
        .ok()
        .map(|ip| format!("ip:{ip}"))
}

struct Server {
    assets: PathBuf,
    status: PathBuf,
    control: PathBuf,
    port: u16,
    public: Option<String>,
    live: Mutex<Live>,
}

impl Server {
    /// The collector's snapshot, checked to be one.
    fn snapshot(&self) -> std::result::Result<(Vec<u8>, Value), &'static str> {
        let bytes = match common::bytes(&self.status, 2_000_000) {
            Ok(b) => b,
            Err(_) if !self.status.exists() => return Err("E10"),
            Err(_) => return Err("E11"),
        };
        match serde_json::from_slice::<Value>(&bytes) {
            Ok(v) if v.is_object() && v["format"] == "neko-dashboard-v1" => Ok((bytes, v)),
            _ => Err("E11"),
        }
    }

    /// Live readings, with NVIDIA figures from the collector (only root can ask the
    /// NVIDIA driver) matched to their card.
    fn live(&self) -> std::result::Result<Vec<u8>, &'static str> {
        let mut value = self.live.lock().map_err(|_| "E20")?.sample();
        if let Ok((_, status)) = self.snapshot() {
            merge_nvidia(&mut value, &status, common::now());
        }
        serde_json::to_vec(&value).map_err(|_| "E20")
    }

    fn processes(&self) -> std::result::Result<Vec<u8>, &'static str> {
        let apps = self
            .snapshot()
            .map(|(_, v)| containers(&v))
            .unwrap_or_default();
        let value = self.live.lock().map_err(|_| "E20")?.processes(&apps);
        serde_json::to_vec(&value).map_err(|_| "E20")
    }
}

/// Each container (first 12 characters of its ID) with its app and Compose service,
/// from the collector's snapshot.
fn containers(status: &Value) -> HashMap<String, (String, String)> {
    let services = &status["container_services"];
    status["containers"]
        .as_object()
        .map(|m| {
            m.iter()
                .filter_map(|(id, app)| {
                    let service = services[id].as_str().unwrap_or("");
                    Some((id.clone(), (app.as_str()?.to_string(), service.to_string())))
                })
                .collect()
        })
        .unwrap_or_default()
}

/// NVIDIA figures come from the collector's snapshot, so they are as old as it is.
/// Each card gets the time they were measured; once the snapshot is late (two
/// collection intervals plus 30 s, the page's and acceptance's rule) the load and
/// memory figures are left out instead of being shown under a "Live" badge.
fn merge_nvidia(value: &mut Value, status: &Value, now: f64) {
    let at = status["generated_at"].as_f64().filter(|t| t.is_finite());
    let interval = status["collect_interval"]
        .as_u64()
        .filter(|s| (30..=300).contains(s))
        .unwrap_or(60) as f64;
    let current = at.is_some_and(|t| now - t <= 2.0 * interval + 30.0 && t <= now + 60.0);
    for extra in status["gpu_telemetry"].as_array().into_iter().flatten() {
        let bus = extra["bus"].as_str().unwrap_or("").to_lowercase();
        for gpu in value["gpus"].as_array_mut().into_iter().flatten() {
            let id = gpu["id"].as_str().unwrap_or("").to_lowercase();
            if bus.len() < 12 || id.len() < 12 || bus[bus.len() - 12..] != id[id.len() - 12..] {
                continue;
            }
            if !extra["name"].is_null() {
                gpu["name"] = extra["name"].clone();
            }
            if current {
                for key in ["percent", "memory_used", "memory_total"] {
                    if !extra[key].is_null() {
                        gpu[key] = extra[key].clone();
                    }
                }
                gpu["sampled_at"] = json!(at);
                gpu["source"] = json!("NVIDIA driver (every minute)");
            } else {
                gpu["source"] = json!("NVIDIA readings are out of date (the collector stopped)");
            }
        }
    }
}
fn handle(mut stream: TcpStream, server: &Server) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(4)));
    if !stream
        .peer_addr()
        .map(|p| p.ip().is_loopback())
        .unwrap_or(false)
    {
        return;
    }
    let mut buf = Vec::with_capacity(4096);
    let start = Instant::now();
    loop {
        if start.elapsed() > Duration::from_secs(3) {
            return;
        }
        if buf.len() >= 16384 {
            reply(&mut stream, 431, b"", "application/json", false);
            return;
        }
        let mut chunk = [0u8; 2048];
        let capacity = chunk.len().min(16384 - buf.len());
        match stream.read(&mut chunk[..capacity]) {
            Ok(0) | Err(_) => return,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
        }
        if buf.windows(4).any(|b| b == b"\r\n\r\n") {
            break;
        }
    }
    let mut headers = [httparse::EMPTY_HEADER; 64];
    let mut req = httparse::Request::new(&mut headers);
    let body_start = match req.parse(&buf) {
        Ok(httparse::Status::Complete(n)) => n,
        _ => {
            reply_error(&mut stream, "E42", false);
            return;
        }
    };
    let method = req.method.unwrap_or("");
    let head = method == "HEAD";
    let post = method == "POST";
    if method != "GET" && !head && !post {
        reply_error(&mut stream, "E42", false);
        return;
    }
    let (allowed_hosts, origins) = allowed(server.port, server.public.as_deref());
    let values = |key: &str| {
        req.headers
            .iter()
            .filter(|h| h.name.eq_ignore_ascii_case(key))
            .map(|h| h.value)
            .collect::<Vec<_>>()
    };
    let hosts = values("host");
    let origin = values("origin");
    let fetch = values("sec-fetch-site");
    let cl = values("content-length");
    if hosts.len() != 1
        || !allowed_hosts.iter().any(|h| h.as_bytes() == hosts[0])
        || origin.len() > 1
        || origin
            .first()
            .map(|v| !origins.iter().any(|o| o.as_bytes() == *v))
            .unwrap_or(false)
        || fetch.iter().any(|v| *v == b"cross-site")
        // Changes must come from the dashboard page itself (browsers always send
        // Origin with POST), so another site cannot press the buttons.
        || (post && (origin.len() != 1 || fetch.iter().any(|v| *v != b"same-origin")))
    {
        reply_error(&mut stream, "E41", head);
        return;
    }
    let length = match cl.as_slice() {
        [] => Some(0),
        [v] => std::str::from_utf8(v)
            .ok()
            .and_then(|v| v.parse::<usize>().ok()),
        _ => None,
    };
    let Some(length) = length.filter(|n| *n == 0 || (post && *n <= MAX_BODY)) else {
        reply_error(&mut stream, "E42", head);
        return;
    };
    if !values("transfer-encoding").is_empty() {
        reply_error(&mut stream, "E42", head);
        return;
    }
    let path = req.path.unwrap_or("").split('?').next().unwrap_or("");
    if path == "/api/control" || path.starts_with("/api/control/") {
        // The server itself: a loopback address that Tailscale did not forward.
        let forwarded = [
            "x-forwarded-for",
            "x-forwarded-host",
            "forwarded",
            "tailscale-user-login",
        ]
        .iter()
        .any(|h| !values(h).is_empty());
        let local = !forwarded
            && (hosts[0].starts_with(b"127.0.0.1:") || hosts[0].starts_with(b"localhost:"));
        let who = Who {
            local,
            source: (!local)
                .then(|| source_of(&values("tailscale-user-login"), &values("x-forwarded-for")))
                .flatten(),
        };
        let token = values("x-neko-token")
            .first()
            .and_then(|v| std::str::from_utf8(v).ok())
            .map(str::to_string);
        let mut body = buf[body_start..].to_vec();
        while body.len() < length && start.elapsed() < Duration::from_secs(3) {
            let mut chunk = [0u8; 2048];
            match stream.read(&mut chunk[..(length - body.len()).min(2048)]) {
                Ok(0) | Err(_) => break,
                Ok(n) => body.extend_from_slice(&chunk[..n]),
            }
        }
        let result = control(
            server,
            path,
            post,
            &body[..body.len().min(length)],
            &who,
            token.as_deref(),
            length,
        );
        match result {
            Ok((status, b)) => reply(&mut stream, status, &b, "application/json", head),
            Err(code) => reply_error(&mut stream, code, head),
        }
        return;
    }
    if post {
        reply_error(&mut stream, "E42", false);
        return;
    }
    #[cfg(feature = "test-fixtures")]
    if path == "/__fixture_metrics" {
        let mut metrics = json!({"fixture":true});
        if let Ok(status) = fs::read_to_string("/proc/self/status") {
            for (key, label) in [("VmRSS:", "rss_kib"), ("Threads:", "threads")] {
                metrics[label] = json!(status
                    .lines()
                    .find(|line| line.starts_with(key))
                    .and_then(|line| line.split_whitespace().nth(1))
                    .and_then(|v| v.parse::<u64>().ok()));
            }
        }
        reply(
            &mut stream,
            200,
            &serde_json::to_vec(&metrics).unwrap(),
            "application/json",
            head,
        );
        return;
    }
    let result = match path {
        "/api/status" => server.snapshot().map(|(b, _)| b),
        "/api/live" => server.live(),
        "/api/processes" => server.processes(),
        _ => match ASSETS.iter().find(|a| a.0 == path) {
            Some((_, file, mime)) => match common::bytes(&server.assets.join(file), 2_000_000) {
                Ok(b) => {
                    reply(&mut stream, 200, &b, mime, head);
                    return;
                }
                Err(_) => Err("E40"),
            },
            None => Err("E40"),
        },
    };
    match result {
        Ok(b) => reply(&mut stream, 200, &b, "application/json", head),
        Err(code) => reply_error(&mut stream, code, head),
    }
}

/// `/api/control` (GET: what can be controlled) and `/api/control/<op>` (POST).
fn control(
    server: &Server,
    path: &str,
    post: bool,
    body: &[u8],
    who: &Who,
    token: Option<&str>,
    length: usize,
) -> std::result::Result<(u16, Vec<u8>), &'static str> {
    let op = match (path, post) {
        ("/api/control", false) => "status",
        (p, true) => match p.trim_start_matches("/api/control/") {
            op @ ("unlock" | "fan" | "light" | "detect") => op,
            "detect-cancel" => "detect_cancel",
            "app-restart" => "app_restart",
            "desktop-close" => "desktop_close",
            "desktop-force" => "desktop_force",
            _ => return Err("E40"),
        },
        _ => return Err("E42"),
    };
    let body = match body.len() {
        0 if length == 0 => json!({}),
        n if n == length => serde_json::from_slice::<Value>(body)
            .ok()
            .filter(Value::is_object)
            .ok_or("E42")?,
        _ => return Err("E42"),
    };
    let answer = ask_control(&server.control, &control_request(op, &body, who, token))?;
    let status = match answer["error"].as_str() {
        Some(code) => status_of(code),
        None => 200,
    };
    serde_json::to_vec(&answer)
        .map(|b| (status, b))
        .map_err(|_| "E21")
}

pub fn run(a: &Args) -> Result<()> {
    if common::root() && !cfg!(feature = "test-fixtures") {
        return Err(Error::new("Unprivileged user required"));
    }
    let assets = common::safe(&a.path("assets")?)?;
    if !fs::metadata(&assets)?.is_dir() {
        return Err(Error::new("Assets directory required"));
    }
    let status = common::safe(Path::new(
        &a.value("status", "/run/nekoserver-dashboard/status.json"),
    ))?;
    let n = a.number("port", 8787)?;
    if !(1024..=65535).contains(&n) {
        return Err(Error::new("Invalid unprivileged port"));
    }
    let port = n as u16;
    let public = match a.value("public-host", "").as_str() {
        "" => None,
        name if valid_public(name) => Some(name.to_string()),
        _ => return Err(Error::new("Invalid Tailscale dashboard name")),
    };
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    let server = Arc::new(Server {
        assets,
        status,
        control: PathBuf::from(a.value("control", crate::control::SOCKET)),
        port,
        public,
        live: Mutex::new(Live::default()),
    });
    let (tx, rx) = mpsc::sync_channel::<TcpStream>(32);
    let rx = Arc::new(Mutex::new(rx));
    for _ in 0..4 {
        let rx = rx.clone();
        let server = server.clone();
        thread::spawn(move || loop {
            let stream = match rx.lock().unwrap().recv() {
                Ok(s) => s,
                Err(_) => break,
            };
            // A request that panics must not leave one worker fewer each time, until
            // the server takes connections it never answers: exit, and systemd starts
            // a fresh one (Restart=always) within seconds.
            let served =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| handle(stream, &server)));
            if served.is_err() {
                std::process::exit(70);
            }
        });
    }
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let _ = tx.try_send(s);
            }
            Err(_) => thread::sleep(Duration::from_millis(50)),
        }
    }
    Ok(())
}

/// `nekoserver-runtime hardware`: what the dashboard can read on this computer.
pub fn hardware() -> Result<()> {
    let mut live = Live::default();
    let mut value = live.sample();
    value["processes"] = json!(live.processes(&HashMap::new())["count"]);
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tailscale_name_is_the_only_extra_host() {
        assert!(valid_public("neko.tail1234.ts.net"));
        for bad in [
            "evil.example.com",
            "ts.net",
            "a..ts.net",
            "neko.tail1234.ts.net.evil.com",
            "x_y.tail.ts.net",
        ] {
            assert!(!valid_public(bad), "{bad}");
        }
        let (hosts, origins) = allowed(8787, None);
        assert_eq!(hosts, ["127.0.0.1:8787", "localhost:8787"]);
        assert_eq!(origins.len(), 2);
        let (hosts, origins) = allowed(8787, Some("neko.tail1234.ts.net"));
        assert!(hosts.contains(&"neko.tail1234.ts.net:8448".to_string()));
        assert!(origins.contains(&"https://neko.tail1234.ts.net:8448".to_string()));
        assert!(!hosts.iter().any(|h| h == "other.tail1234.ts.net:8448"));
    }

    #[test]
    fn nvidia_figures_carry_their_age_and_expire() {
        let gpus =
            || json!({"gpus":[{"id":"0000:01:00.0","name":"NVIDIA graphics","percent":null}]});
        let telemetry = json!([{"bus":"00000000:01:00.0","name":"NVIDIA GeForce RTX 4060","percent":23.0,"memory_used":1.0e9}]);
        let mut fresh = gpus();
        merge_nvidia(
            &mut fresh,
            &json!({"generated_at":1000.0,"gpu_telemetry":telemetry}),
            1030.0,
        );
        assert_eq!(fresh["gpus"][0]["percent"], 23.0);
        assert_eq!(fresh["gpus"][0]["sampled_at"], 1000.0);
        let mut old = gpus();
        merge_nvidia(
            &mut old,
            &json!({"generated_at":1000.0,"gpu_telemetry":telemetry}),
            1200.0,
        );
        assert!(
            old["gpus"][0]["percent"].is_null(),
            "a frozen load is not shown as live"
        );
        assert_eq!(old["gpus"][0]["name"], "NVIDIA GeForce RTX 4060");
        assert!(old["gpus"][0]["source"]
            .as_str()
            .unwrap()
            .contains("out of date"));
        // A five-minute collection interval keeps a 200-second-old reading current.
        let mut slow = gpus();
        merge_nvidia(
            &mut slow,
            &json!({"generated_at":1000.0,"collect_interval":300,"gpu_telemetry":telemetry}),
            1200.0,
        );
        assert_eq!(slow["gpus"][0]["percent"], 23.0);
    }
    #[test]
    fn app_controls_pass_on_only_what_they_need() {
        let body = json!({"app": "immich", "pid": 5, "start": 6, "local": true, "signal": 9, "path": "/system.slice"});
        let request = control_request("app_restart", &body, &Who::default(), Some("t"));
        assert_eq!(
            request,
            json!({"op": "app_restart", "local": false, "app": "immich", "pid": 5, "start": 6, "token": "t"})
        );
    }
    #[test]
    fn the_source_of_a_forwarded_request_is_what_tailscale_added() {
        let ff: &[&[u8]] = &[b"9.9.9.9, 100.64.0.7"];
        assert_eq!(source_of(&[], ff).as_deref(), Some("ip:100.64.0.7"));
        assert_eq!(
            source_of(&[b"bo@example.com"], ff).as_deref(),
            Some("login:bo@example.com")
        );
        // Two logins, or one that isn't plain text, don't count as one.
        assert_eq!(
            source_of(&[b"a@x", b"b@x"], ff).as_deref(),
            Some("ip:100.64.0.7")
        );
        assert_eq!(source_of(&[b"a b"], &[]), None);
        assert_eq!(source_of(&[], &[b"not an address"]), None);
        assert_eq!(source_of(&[], &[]), None);
    }
    #[test]
    fn every_status_has_its_own_reason_phrase() {
        for (_, status, _) in ERRORS {
            let phrase = reason(*status);
            assert!(
                *status == 503 || phrase != "Service Unavailable",
                "{status}"
            );
        }
        assert_eq!(reason(409), "Conflict");
    }
    #[test]
    fn errors_are_short_coded_sentences() {
        for (code, status, message) in ERRORS {
            assert!(code.len() == 3 && code.starts_with('E'), "{code}");
            assert!(message.split_whitespace().count() <= 7, "{message}");
            let (s, body) = error_body(code);
            assert_eq!(s, *status);
            let v: Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(
                (v["error"].as_str(), v["message"].as_str()),
                (Some(*code), Some(*message))
            );
        }
        let mut codes = ERRORS.iter().map(|e| e.0).collect::<Vec<_>>();
        codes.dedup();
        assert_eq!(codes.len(), ERRORS.len());
    }

    /// A server on a free port, answering on its own thread. Returns the port.
    fn test_server(assets: PathBuf, root: &Path, control: PathBuf, public: Option<&str>) -> u16 {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = Server {
            assets,
            status: root.join("status.json"),
            control,
            port,
            public: public.map(str::to_string),
            live: Mutex::new(Live::default()),
        };
        thread::spawn(move || {
            for s in listener.incoming().flatten() {
                handle(s, &server);
            }
        });
        port
    }
    /// Requests against a real server on a free port, with a temporary snapshot.
    #[test]
    fn the_server_answers_with_pages_readings_and_coded_errors() {
        let root = std::env::temp_dir().join(format!("neko-serve-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("assets")).unwrap();
        for file in asset_files() {
            fs::write(root.join("assets").join(file), format!("asset {file}")).unwrap();
        }
        let port = test_server(root.join("assets"), &root, root.join("control.sock"), None);
        let get = |path: &str, host: &str| {
            let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
            write!(s, "GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n").unwrap();
            let mut out = String::new();
            s.read_to_string(&mut out).unwrap();
            out
        };
        let host = format!("127.0.0.1:{port}");
        assert!(get("/", &host).ends_with("asset index.html"));
        assert!(get("/manifest.webmanifest", &host).contains("application/manifest+json"));
        let missing = get("/api/status", &host);
        assert!(
            missing.starts_with("HTTP/1.1 503") && missing.contains("\"E10\""),
            "{missing}"
        );
        fs::write(root.join("status.json"), "{not json").unwrap();
        assert!(get("/api/status", &host).contains("\"E11\""));
        fs::write(
            root.join("status.json"),
            r#"{"format":"neko-dashboard-v1","containers":{}}"#,
        )
        .unwrap();
        let ok = get("/api/status", &host);
        assert!(ok.starts_with("HTTP/1.1 200"));
        let clock: f64 = ok
            .lines()
            .find_map(|l| l.strip_prefix("X-Server-Time: "))
            .unwrap()
            .parse()
            .unwrap();
        assert!((clock - common::now()).abs() < 60.0);
        assert!(get("/nope", &host).contains("\"E40\""));
        assert!(get("/", "evil.example:80").contains("\"E41\""));
        let live = get("/api/live", &host);
        assert!(live.contains("neko-live-v1"), "{live}");
        assert!(get("/api/processes", &host).contains("neko-processes-v1"));
        let _ = fs::remove_dir_all(&root);
    }

    /// Fan and light requests reach the control service only from the page itself,
    /// with `local` decided by the server and never by the page.
    #[test]
    fn control_requests_are_checked_and_forwarded() {
        use std::os::unix::net::UnixListener;
        let root = std::env::temp_dir().join(format!("neko-control-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let socket = root.join("control.sock");
        let port = test_server(
            root.clone(),
            &root,
            socket.clone(),
            Some("neko.tail1234.ts.net"),
        );
        let send = |request: String| {
            let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
            // The server may answer and close before an oversized body is sent.
            let _ = s.write_all(request.as_bytes());
            let mut out = Vec::new();
            let _ = s.read_to_end(&mut out);
            String::from_utf8_lossy(&out).into_owned()
        };
        let local = format!("127.0.0.1:{port}");
        let post = |path: &str, host: &str, origin: &str, extra: &str, body: &str| {
            send(format!(
                "POST {path} HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\nSec-Fetch-Site: same-origin\r\nContent-Type: application/json\r\n{extra}Content-Length: {}\r\n\r\n{body}",
                body.len()
            ))
        };
        let here = format!("http://{local}");
        // The service is not running yet.
        assert!(post("/api/control/fan", &local, &here, "", "{}").contains("\"E21\""));
        // A stand-in service that answers with the request it was given.
        let service = UnixListener::bind(&socket).unwrap();
        thread::spawn(move || {
            for mut s in service.incoming().flatten() {
                let mut line = String::new();
                BufReader::new(s.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                let answer = if request["pin"] == "0000" {
                    json!({"ok": false, "error": "E44", "message": "Wrong PIN"})
                } else {
                    json!({"ok": true, "seen": request})
                };
                writeln!(s, "{answer}").unwrap();
            }
        });
        let seen = |reply: &str| -> Value {
            serde_json::from_str::<Value>(reply.split("\r\n\r\n").nth(1).unwrap()).unwrap()["seen"]
                .clone()
        };
        let reply = post(
            "/api/control/fan",
            &local,
            &here,
            "",
            r#"{"output":"a","setting":{"mode":"auto"},"local":false,"op":"x"}"#,
        );
        let request = seen(&reply);
        assert_eq!(
            (request["op"].as_str(), request["local"].as_bool()),
            (Some("fan"), Some(true)),
            "{reply}"
        );
        assert_eq!(request["setting"]["mode"], "auto");
        // Through Tailscale: not local, even if the page claims otherwise.
        let remote = "https://neko.tail1234.ts.net:8448";
        let reply = post(
            "/api/control/light",
            "neko.tail1234.ts.net:8448",
            remote,
            "X-Forwarded-For: 100.64.0.2\r\nX-Neko-Token: abc\r\n",
            r#"{"device":"d","local":true,"source":"local"}"#,
        );
        let request = seen(&reply);
        assert_eq!(
            (request["local"].as_bool(), request["token"].as_str()),
            (Some(false), Some("abc")),
            "{reply}"
        );
        // Wrong PINs are counted per device: the server names it, never the page.
        assert_eq!(request["source"], "ip:100.64.0.2", "{reply}");
        let reply = post(
            "/api/control/unlock",
            "neko.tail1234.ts.net:8448",
            remote,
            "X-Forwarded-For: 1.2.3.4, 100.64.0.9\r\nTailscale-User-Login: amy@example.com\r\n",
            r#"{"pin":"1234","source":"local"}"#,
        );
        assert_eq!(seen(&reply)["source"], "login:amy@example.com", "{reply}");
        let reply = post(
            "/api/control/unlock",
            &local,
            &here,
            "",
            r#"{"pin":"1234","source":"phone"}"#,
        );
        assert_eq!(
            (seen(&reply)["local"].as_bool(), seen(&reply).get("source")),
            (Some(true), None),
            "{reply}"
        );
        // A loopback Host forwarded by Tailscale is still not the server itself.
        let reply = post(
            "/api/control/light",
            &local,
            &here,
            "X-Forwarded-For: 100.64.0.2\r\n",
            r#"{"device":"d"}"#,
        );
        assert_eq!(seen(&reply)["local"], false);
        // Coded refusals keep their HTTP status.
        assert!(post(
            "/api/control/unlock",
            &local,
            &here,
            "",
            r#"{"pin":"0000"}"#
        )
        .starts_with("HTTP/1.1 403"));
        // Other sites, missing Origin, bad bodies and unknown actions are refused.
        assert!(
            post("/api/control/fan", &local, "https://evil.example", "", "{}").contains("\"E41\"")
        );
        let no_origin = send(format!(
            "POST /api/control/fan HTTP/1.1\r\nHost: {local}\r\nContent-Length: 2\r\n\r\n{{}}"
        ));
        assert!(no_origin.contains("\"E41\""), "{no_origin}");
        assert!(post("/api/control/fan", &local, &here, "", "[1]").contains("\"E42\""));
        assert!(post(
            "/api/control/fan",
            &local,
            &here,
            "",
            &"x".repeat(MAX_BODY + 1)
        )
        .contains("\"E42\""));
        assert!(post("/api/control/format-disk", &local, &here, "", "{}").contains("\"E40\""));
        assert!(post("/api/status", &local, &here, "", "{}").contains("\"E42\""));
        // Reading what can be controlled is a plain GET.
        let status = send(format!(
            "GET /api/control HTTP/1.1\r\nHost: {local}\r\n\r\n"
        ));
        assert_eq!(seen(&status)["op"], "status");
        let _ = fs::remove_dir_all(&root);
    }
    #[test]
    fn the_snapshot_is_checked_to_be_the_collectors() {
        let root = std::env::temp_dir().join(format!("neko-snapshot-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let server = Server {
            assets: root.clone(),
            status: root.join("status.json"),
            control: root.join("control.sock"),
            port: 1,
            public: None,
            live: Mutex::new(Live::default()),
        };
        assert_eq!(server.snapshot().unwrap_err(), "E10", "no snapshot yet");
        for wrong in [
            "not json",
            "[1, 2]",
            r#"{"format": "neko-dashboard-v0"}"#,
            r#"{"cpu": 1}"#,
        ] {
            fs::write(&server.status, wrong).unwrap();
            assert_eq!(server.snapshot().unwrap_err(), "E11", "{wrong}");
        }
        fs::write(
            &server.status,
            r#"{"format": "neko-dashboard-v1", "cpu": 1}"#,
        )
        .unwrap();
        let (bytes, value) = server.snapshot().unwrap();
        assert_eq!(value["cpu"], 1);
        assert_eq!(bytes, fs::read(&server.status).unwrap());
        fs::remove_file(&server.status).unwrap();
        fs::create_dir(&server.status).unwrap();
        assert_eq!(
            server.snapshot().unwrap_err(),
            "E11",
            "there, but unreadable"
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn containers_come_from_the_snapshot_with_their_services() {
        let status = json!({
            "containers": {"abc123def456": "jellyfin", "0123456789ab": "immich", "bad": 7},
            "container_services": {"abc123def456": "jellyfin", "0123456789ab": "immich-server"}
        });
        let found = containers(&status);
        assert_eq!(found.len(), 2, "{found:?}");
        assert_eq!(
            found["abc123def456"],
            ("jellyfin".to_string(), "jellyfin".to_string())
        );
        assert_eq!(
            found["0123456789ab"],
            ("immich".to_string(), "immich-server".to_string())
        );
        let unnamed = containers(&json!({"containers": {"abc123def456": "jellyfin"}}));
        assert_eq!(
            unnamed["abc123def456"],
            ("jellyfin".to_string(), String::new())
        );
        assert!(containers(&json!({})).is_empty());
    }
    #[test]
    fn nvidia_figures_expire_at_two_intervals_plus_thirty_seconds() {
        let telemetry = json!([{"bus":"00000000:01:00.0","percent":23.0}]);
        let percent = |interval: Option<u64>, now: f64| {
            let mut value = json!({"gpus":[{"id":"0000:01:00.0","percent":null}]});
            let mut status = json!({"generated_at":1000.0,"gpu_telemetry":telemetry});
            if let Some(seconds) = interval {
                status["collect_interval"] = json!(seconds);
            }
            merge_nvidia(&mut value, &status, now);
            value["gpus"][0]["percent"].clone()
        };
        assert_eq!(
            percent(None, 1149.0),
            23.0,
            "within two minutes and thirty seconds"
        );
        assert!(percent(None, 1151.0).is_null(), "a second too late");
        assert_eq!(percent(Some(30), 1089.0), 23.0);
        assert!(percent(Some(30), 1091.0).is_null());
        assert_eq!(
            percent(None, 941.0),
            23.0,
            "a clock a minute behind the collector"
        );
        assert!(
            percent(None, 939.0).is_null(),
            "a snapshot from the future is not live"
        );
        // Cards are matched by the last twelve characters of their bus address.
        let mut value =
            json!({"gpus":[{"id":"0000:02:00.0","percent":null},{"id":"short","percent":null}]});
        merge_nvidia(
            &mut value,
            &json!({"generated_at":1000.0,"gpu_telemetry":[{"bus":"00000000:01:00.0","percent":23.0},{"bus":"short","percent":9.0}]}),
            1000.0,
        );
        assert!(value["gpus"][0]["percent"].is_null());
        assert!(value["gpus"][1]["percent"].is_null());
    }
    #[test]
    fn tailscale_names_and_error_sentences_have_limits() {
        let name = |n: usize| {
            format!(
                "{}.{}.{}.{}.ts.net",
                "a".repeat(63),
                "b".repeat(63),
                "c".repeat(63),
                "d".repeat(n)
            )
        };
        assert_eq!(name(54).len(), 253);
        assert!(valid_public(&name(54)));
        assert!(!valid_public(&name(55)), "longer than a host name may be");
        assert_eq!(error_message("E10"), error_entry("E10").2);
        assert!(error_message("E10").len() > 10);
        assert_eq!(error_message("no such code"), ERRORS[0].2);
    }
}
