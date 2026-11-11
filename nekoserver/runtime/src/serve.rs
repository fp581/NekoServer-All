//! The dashboard's web server: unprivileged and loopback only.
//!
//! It serves the page, the collector's latest snapshot (`/api/status`), live
//! hardware readings (`/api/live`) and the process list (`/api/processes`). Fan and
//! lighting changes and app controls (`/api/control`) are passed to the root control
//! service over its private socket; this server never writes to hardware, stops a
//! program or talks to Docker itself. Ask Neko (`/api/control/ask`, then `/api/ask/<id>`)
//! asks the AI assistant on 127.0.0.1 in the background (assist.rs), once the control
//! service has checked the PIN and made the question ready. Anything
//! that goes wrong is answered with a short code and a plain sentence (see ERRORS),
//! which the page shows as is.
use crate::{
    assist,
    common::{self, Error, Result},
    live::Live,
    Args,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener, TcpStream},
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
    ("/theme.css", "theme.css", "text/css; charset=utf-8"),
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
/// The cat's pictures (DASHBOARD.md, "The cat"), named from shared.json's lists: a big
/// animation for each of Home's moods, a small one and a still for each page's. Only names
/// built from those lists are ever served: (file, type).
fn cat_file(path: &str) -> Option<(String, &'static str)> {
    let moods = |key: &str| {
        common::SHARED["cats"][key]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|mood| mood.as_str().map(str::to_string))
            .collect::<Vec<_>>()
    };
    let home = moods("home")
        .into_iter()
        .map(|m| (format!("cat-{m}.webp"), "image/webp"));
    let pages = moods("pages").into_iter().flat_map(|m| {
        [
            (format!("cat-{m}-small.webp"), "image/webp"),
            (format!("cat-{m}.png"), "image/png"),
        ]
    });
    let name = path.strip_prefix('/')?;
    home.chain(pages).find(|(file, _)| file == name)
}
#[cfg(test)]
fn asset_files() -> Vec<String> {
    let mut files = ASSETS.iter().map(|a| a.1.to_string()).collect::<Vec<_>>();
    files.dedup();
    for key in ["home", "pages"] {
        for mood in common::SHARED["cats"][key].as_array().unwrap() {
            let mood = mood.as_str().unwrap();
            for name in [
                format!("/cat-{mood}.webp"),
                format!("/cat-{mood}-small.webp"),
                format!("/cat-{mood}.png"),
            ] {
                if let Some((file, _)) = cat_file(&name) {
                    if !files.contains(&file) {
                        files.push(file);
                    }
                }
            }
        }
    }
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
/// One of the page's files, with its fingerprint (ETag): the browser keeps a copy and asks
/// again only whether it changed, so the cats and the rest are not downloaded anew at every
/// visit (the review of 0.47.0). A copy it already has is answered "304 Not Modified".
fn reply_asset(stream: &mut TcpStream, body: &[u8], mime: &str, asked: &[&[u8]], head: bool) {
    let tag = format!("\"{}\"", &common::sha(body)[..32]);
    let fresh = asked.iter().any(|value| {
        value
            .split(|b| *b == b',')
            .any(|one| one.trim_ascii() == tag.as_bytes() || one.trim_ascii() == b"*")
    });
    let (code, reason) = if fresh {
        (304, "Not Modified")
    } else {
        (200, "OK")
    };
    let length = if fresh { 0 } else { body.len() };
    let header = format!(
        "HTTP/1.1 {code} {reason}\r\nContent-Type: {mime}\r\nContent-Length: {length}\r\nCache-Control: no-cache\r\nETag: {tag}\r\nContent-Security-Policy: {CSP}\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n"
    );
    let _ = stream.write_all(header.as_bytes());
    if !head && !fresh {
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

/// How long the web server waits for the control service's answer.
const CONTROL_WAIT: Duration = Duration::from_secs(15);
/// Largest request body accepted (fan curves and colours are small).
const MAX_BODY: usize = 4096;

/// Ask the control service (one JSON line each way).
fn ask_control(socket: &Path, request: &Value) -> std::result::Result<Value, &'static str> {
    let mut stream = UnixStream::connect(socket).map_err(|_| "E21")?;
    // Longer than the control service may take itself: a lighting change waits for the
    // lighting lock (up to about 6 s while OpenRGB does not answer) and then up to 3 s
    // more; a shorter wait said "unavailable" though the change was saved.
    let _ = stream.set_read_timeout(Some(CONTROL_WAIT));
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
        "sig",
        "tag",
        "question",
        "item",
        "action",
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
    /// Another device: its tailnet address, or else its Tailscale login. Wrong PINs are
    /// counted per source, so one device can't lock out the others. A program on the
    /// server itself that sends Tailscale's headers is `program:<its account number>`.
    pub source: Option<String>,
}
fn printable(v: &[u8]) -> Option<&str> {
    std::str::from_utf8(v)
        .ok()
        .map(str::trim)
        .filter(|v| !v.is_empty() && v.len() <= 200 && v.bytes().all(|b| b.is_ascii_graphic()))
}
/// The source of a forwarded request: the device's tailnet address (the last
/// X-Forwarded-For address, the one Tailscale Serve itself added), or else the login
/// Tailscale Serve names. The address comes first: a login names a person, who often
/// has a phone, a tablet and a laptop on one account, while the address names one device.
fn source_of(login: &[&[u8]], forwarded_for: &[&[u8]]) -> Option<String> {
    let address = forwarded_for
        .last()
        .and_then(|last| std::str::from_utf8(last).ok())
        .and_then(|v| v.rsplit(',').next())
        .and_then(|v| v.trim().parse::<IpAddr>().ok());
    if let Some(ip) = address {
        return Some(format!("ip:{ip}"));
    }
    match login {
        [one] => printable(one).map(|login| format!("login:{login}")),
        _ => None,
    }
}

/// The kernel's lists of TCP connections; each line names the account that owns it.
pub const CONNECTION_TABLES: [&str; 2] = ["/proc/net/tcp", "/proc/net/tcp6"];
/// An address as the connection tables write it: "0100007F:1F90" (IPv4) or 32 hex
/// digits and a port (IPv6). Each group of 8 digits is 4 bytes in this computer's order.
fn table_address(field: &str) -> Option<SocketAddr> {
    let (host, port) = field.split_once(':')?;
    let port = u16::from_str_radix(port, 16).ok()?;
    let word = |i: usize| -> Option<[u8; 4]> {
        let digits = host.get(i * 8..i * 8 + 8)?;
        Some(u32::from_str_radix(digits, 16).ok()?.to_ne_bytes())
    };
    let ip = match host.len() {
        8 => IpAddr::V4(Ipv4Addr::from(word(0)?)),
        32 => {
            let mut bytes = [0u8; 16];
            for i in 0..4 {
                bytes[i * 4..i * 4 + 4].copy_from_slice(&word(i)?);
            }
            IpAddr::V6(Ipv6Addr::from(bytes))
        }
        _ => return None,
    };
    Some(SocketAddr::new(plain(ip), port))
}
/// An IPv4 address written the IPv6 way (::ffff:127.0.0.1) as plain IPv4.
fn plain(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => v6.to_ipv4_mapped().map_or(ip, IpAddr::V4),
        v4 => v4,
    }
}
/// The account that owns the other end of a connection made on this computer: the
/// `uid` column of the client's own socket (its address is `client`, connected to
/// `server`) in the connection tables. None when it is not listed.
pub fn peer_uid(tables: &[String], client: SocketAddr, server: SocketAddr) -> Option<u32> {
    let (client, server) = (
        SocketAddr::new(plain(client.ip()), client.port()),
        SocketAddr::new(plain(server.ip()), server.port()),
    );
    tables
        .iter()
        .flat_map(|t| t.lines().skip(1))
        .find_map(|line| {
            let fields = line.split_whitespace().collect::<Vec<_>>();
            let (local, remote) = (
                table_address(fields.get(1)?)?,
                table_address(fields.get(2)?)?,
            );
            (local == client && remote == server).then(|| fields.get(7)?.parse().ok())?
        })
}
/// Who sent a request with Tailscale's headers: Tailscale Serve (its service runs as
/// root), or a program of another account on this computer pretending to be it. Such a
/// program is counted on its own (`program:<account>`), so it can't use up the wrong
/// PINs that all devices share. When the tables can't be read, it counts as Tailscale.
fn forwarded_source(server: &Server, stream: &TcpStream, named: Option<String>) -> Option<String> {
    match connection_owner(server, stream) {
        Some(uid) if uid != 0 => Some(format!("program:{uid}")),
        _ => named,
    }
}

/// The account of the program at the other end of a connection made on this computer
/// (its connection table's owner column), or None when it can't be told.
fn connection_owner(server: &Server, stream: &TcpStream) -> Option<u32> {
    let tables = server
        .tables
        .iter()
        .filter_map(|t| fs::read_to_string(t).ok())
        .collect::<Vec<_>>();
    match (stream.peer_addr(), stream.local_addr()) {
        (Ok(client), Ok(here)) => peer_uid(&tables, client, here),
        _ => None,
    }
}
/// Requests answered at once, and connections that may wait for a worker.
const WORKERS: usize = 16;
const QUEUE: usize = 128;
struct Server {
    assets: PathBuf,
    status: PathBuf,
    control: PathBuf,
    port: u16,
    public: Option<String>,
    live: Mutex<Live>,
    /// The connection tables read to tell Tailscale from a local program (see
    /// forwarded_source).
    tables: Vec<PathBuf>,
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
/// How long a request may take, counted from when it was accepted: one that waited in the
/// queue is given only what is left (the audit of 0.46.2: queued slow connections each got
/// a full 3 s once a worker took them, so a few hundred kept every worker busy).
const REQUEST_TIME: Duration = Duration::from_secs(3);
/// The request's head (up to its blank line), read until `start` + REQUEST_TIME; None when
/// it didn't come in time, the connection closed, or it was too long (answered 431).
fn read_head(stream: &mut TcpStream, start: Instant) -> Option<Vec<u8>> {
    let mut buf = Vec::with_capacity(4096);
    while start.elapsed() <= REQUEST_TIME {
        if buf.len() >= 16384 {
            reply(stream, 431, b"", "application/json", false);
            return None;
        }
        let mut chunk = [0u8; 2048];
        let capacity = chunk.len().min(16384 - buf.len());
        match stream.read(&mut chunk[..capacity]) {
            Ok(0) | Err(_) => return None,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
        }
        if buf.windows(4).any(|b| b == b"\r\n\r\n") {
            return Some(buf);
        }
    }
    None
}
/// The body's length from Content-Length: none or 0, or for a POST up to MAX_BODY.
fn body_length(given: &[&[u8]], post: bool) -> Option<usize> {
    let length = match given {
        [] => Some(0),
        [v] => std::str::from_utf8(v)
            .ok()
            .and_then(|v| v.parse::<usize>().ok()),
        _ => None,
    };
    length.filter(|n| *n == 0 || (post && *n <= MAX_BODY))
}
/// The rest of a body of `length` bytes after what came with the head, read until
/// `start` + REQUEST_TIME.
fn read_body(stream: &mut TcpStream, mut body: Vec<u8>, length: usize, start: Instant) -> Vec<u8> {
    while body.len() < length && start.elapsed() < REQUEST_TIME {
        let mut chunk = [0u8; 2048];
        match stream.read(&mut chunk[..(length - body.len()).min(2048)]) {
            Ok(0) | Err(_) => break,
            Ok(n) => body.extend_from_slice(&chunk[..n]),
        }
    }
    body
}
/// The request names the dashboard's own address and comes from its page: one Host the
/// dashboard answers to, an Origin (at most one) of its own, never cross-site, and for a
/// change (POST) the page itself (browsers always send Origin with POST), so another site
/// cannot press the buttons. `known`: the hosts and origins it answers to. `navigation`:
/// opening the page itself (a GET of a page, not of /api/), which may come from a link
/// anywhere: the QR code on the phone setup page, a link in a chat. Seen with the phone
/// (0.47.6): Chrome opened from another app says "cross-site", and the dashboard refused
/// its own address. Its readings and buttons stay refused from another site.
fn from_the_page(
    hosts: &[&[u8]],
    origin: &[&[u8]],
    fetch: &[&[u8]],
    post: bool,
    navigation: bool,
    known: (&[String], &[String]),
) -> bool {
    let (allowed_hosts, origins) = known;
    hosts.len() == 1
        && allowed_hosts.iter().any(|h| h.as_bytes() == hosts[0])
        && origin.len() <= 1
        && origin
            .first()
            .is_none_or(|v| origins.iter().any(|o| o.as_bytes() == *v))
        && (navigation || !fetch.iter().any(|v| *v == b"cross-site"))
        && !(post && (origin.len() != 1 || fetch.iter().any(|v| *v != b"same-origin")))
}
/// A page load (Sec-Fetch-Mode: navigate) of a page, never of the readings or buttons
/// under /api/, and never a change (POST). Its Sec-Fetch-Dest is not asked: once the
/// dashboard's service worker (sw.js) is installed, it passes the page load on itself and
/// the browser says "empty" instead of "document" (seen on the phone: a second visit from
/// a link got E41 again). A page's script can't send "navigate" (fetch() refuses it), and
/// frame-ancestors 'none' keeps another site from showing the page in a frame.
fn page_navigation(method: &str, path: &str, mode: &[&[u8]]) -> bool {
    method == "GET" && !path.starts_with("/api/") && mode == [b"navigate"]
}
fn handle(mut stream: TcpStream, server: &Server, accepted: Instant) {
    if accepted.elapsed() >= REQUEST_TIME {
        return; // waited out its time in the queue: closed unanswered
    }
    let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(4)));
    if !stream
        .peer_addr()
        .map(|p| p.ip().is_loopback())
        .unwrap_or(false)
    {
        return;
    }
    let start = accepted;
    let Some(buf) = read_head(&mut stream, start) else {
        return;
    };
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
    let wanted = req.path.unwrap_or("").split('?').next().unwrap_or("");
    let navigation = page_navigation(method, wanted, &values("sec-fetch-mode"));
    if !from_the_page(
        &hosts,
        &origin,
        &fetch,
        post,
        navigation,
        (&allowed_hosts, &origins),
    ) {
        reply_error(&mut stream, "E41", head);
        return;
    }
    let Some(length) = body_length(&cl, post) else {
        reply_error(&mut stream, "E42", head);
        return;
    };
    if !values("transfer-encoding").is_empty() {
        reply_error(&mut stream, "E42", head);
        return;
    }
    let path = req.path.unwrap_or("").split('?').next().unwrap_or("");
    // The server itself: a loopback address that Tailscale did not forward.
    let forwarded = [
        "x-forwarded-for",
        "x-forwarded-host",
        "forwarded",
        "tailscale-user-login",
    ]
    .iter()
    .any(|h| !values(h).is_empty());
    let local = is_local(forwarded, hosts[0]);
    let token = values("x-neko-token")
        .first()
        .and_then(|v| std::str::from_utf8(v).ok())
        .map(str::to_string);
    if let (Some(id), false) = (path.strip_prefix("/api/ask/"), post) {
        let owner = ask_owner(local, token.as_deref());
        return answer_so_far(&mut stream, id, &owner, head);
    }
    if path == "/api/control" || path.starts_with("/api/control/") {
        let named = source_of(&values("tailscale-user-login"), &values("x-forwarded-for"));
        let who = caller(server, &stream, local, named);
        let body = read_body(&mut stream, buf[body_start..].to_vec(), length, start);
        if path == "/api/control/ask" && post {
            let body = &body[..body.len().min(length)];
            ask(server, &mut stream, body, length, &who, token.as_deref());
            return;
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
        return answered(&mut stream, result, head);
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
        _ => match ASSETS
            .iter()
            .find(|a| a.0 == path)
            .map(|a| (a.1.to_string(), a.2))
            .or_else(|| cat_file(path))
        {
            Some((file, mime)) => match common::bytes(&server.assets.join(&file), 2_000_000) {
                Ok(b) => {
                    reply_asset(&mut stream, &b, mime, &values("if-none-match"), head);
                    return;
                }
                Err(_) => Err("E40"),
            },
            None => Err("E40"),
        },
    };
    answered(&mut stream, result.map(|b| (200, b)), head)
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
            "update-now" => "app_update_now",
            "update-run" => "app_update_run",
            "update-check" => "app_update_check",
            "upgrade-nekoserver" => "app_upgrade_nekoserver",
            "desktop-close" => "desktop_close",
            "desktop-force" => "desktop_force",
            "ask" => "app_ask",
            "assist-check" => "app_assist_check",
            "assist-run" => "app_assist_run",
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

/// The server itself: a loopback address that Tailscale did not forward.
fn is_local(forwarded: bool, host: &[u8]) -> bool {
    !forwarded && (host.starts_with(b"127.0.0.1:") || host.starts_with(b"localhost:"))
}

/// Who sent a control request. On the server itself, the account of the program that
/// connected: each one's wrong PINs count on their own. Otherwise the device Tailscale
/// named (`named`), or the program on this computer that sent Tailscale's headers.
fn caller(server: &Server, stream: &TcpStream, local: bool, named: Option<String>) -> Who {
    let source = if local {
        connection_owner(server, stream).map(|uid| format!("account:{uid}"))
    } else {
        forwarded_source(server, stream, named)
    };
    Who { local, source }
}

/// A JSON answer (its status and body), or an error by its code.
fn answered(
    stream: &mut TcpStream,
    result: std::result::Result<(u16, Vec<u8>), &'static str>,
    head: bool,
) {
    match result {
        Ok((status, b)) => reply(stream, status, &b, "application/json", head),
        Err(code) => reply_error(stream, code, head),
    }
}

/// Ask Neko's answer so far (GET /api/ask/<id>), for the one who asked: a short request,
/// whatever the model is doing.
fn answer_so_far(stream: &mut TcpStream, id: &str, owner: &str, head: bool) {
    match assist::progress(id, owner) {
        Some(v) => reply(
            stream,
            200,
            v.to_string().as_bytes(),
            "application/json",
            head,
        ),
        None => reply_error(stream, "E40", head),
    }
}

/// Who may read an ask's answer: the PIN session that asked, or the server itself; ""
/// (no one) for another device without a session.
fn ask_owner(local: bool, token: Option<&str>) -> String {
    match token {
        Some(t) => format!("token:{t}"),
        None if local => "local".into(),
        None => String::new(),
    }
}

/// Ask Neko: the control service checks the PIN and makes the question ready (the
/// readings and logs with their secrets taken out); the answer is then asked for in the
/// background (assist::begin) and the page gets only an id to poll (/api/ask/<id>).
/// One question at a time.
fn ask(
    server: &Server,
    stream: &mut TcpStream,
    body: &[u8],
    length: usize,
    who: &Who,
    token: Option<&str>,
) {
    let Some(turn) = assist::Turn::take() else {
        let busy = json!({"ok": false, "error": "E47", "message": assist::BUSY});
        return reply(
            stream,
            status_of("E47"),
            busy.to_string().as_bytes(),
            "application/json",
            false,
        );
    };
    let ready = match control(server, "/api/control/ask", true, body, who, token, length) {
        Ok((200, b)) => serde_json::from_slice::<Value>(&b).unwrap_or_default(),
        Ok((status, b)) => return reply(stream, status, &b, "application/json", false),
        Err(code) => return reply_error(stream, code, false),
    };
    let owner = ask_owner(who.local, token);
    let model = ready["model"].as_str().unwrap_or("").to_string();
    let repairs: Vec<(String, String)> = ready["repairs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|id| id.as_str().map(|id| (id.to_string(), String::new())))
        .collect();
    if owner.is_empty() {
        return reply_error(stream, "E43", false);
    }
    let answer = match assist::begin(
        turn,
        owner,
        assist::OLLAMA.into(),
        model.clone(),
        ready["threads"].as_u64().unwrap_or(1),
        ready["messages"].clone(),
        repairs,
        assist::LIMITS,
    ) {
        Ok(id) => {
            json!({"ok": true, "id": id, "model": model, "excerpt": ready["excerpt"], "question": ready["question"]})
        }
        Err(why) => {
            json!({"ok": false, "error": "E21", "message": format!("{}: {why}", assist::NOT_AVAILABLE)})
        }
    };
    reply(
        stream,
        200,
        answer.to_string().as_bytes(),
        "application/json",
        false,
    );
}

/// The dashboard's socket: the one systemd opened for it at the start of the computer
/// (nekoserver-dashboard.socket, handed over as fd 3), so no other program can take the
/// port first and collect the PIN the phones type in (the audit of 0.46.2). Started by
/// hand (no socket handed over), it binds the port itself, as before.
fn listener(port: u16) -> Result<TcpListener> {
    use std::os::unix::io::FromRawFd;
    let ours = std::env::var("LISTEN_PID")
        .ok()
        .and_then(|p| p.parse::<u32>().ok())
        == Some(std::process::id());
    let count = std::env::var("LISTEN_FDS")
        .ok()
        .and_then(|n| n.parse::<u32>().ok())
        .unwrap_or(0);
    if !ours || count == 0 {
        return Ok(TcpListener::bind(("127.0.0.1", port))?);
    }
    // SAFETY: systemd hands the listening socket over as fd 3 (SD_LISTEN_FDS_START).
    let handed = unsafe { TcpListener::from_raw_fd(3) };
    let address = handed.local_addr()?;
    if address.port() != port || !address.ip().is_loopback() {
        return Err(Error::new(&format!(
            "The dashboard's socket is {address}, not 127.0.0.1:{port}: run setup again"
        )));
    }
    Ok(handed)
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
    let listener = listener(port)?;
    let server = Arc::new(Server {
        assets,
        status,
        control: PathBuf::from(a.value("control", crate::control::SOCKET)),
        port,
        public,
        live: Mutex::new(Live::default()),
        tables: CONNECTION_TABLES.iter().map(PathBuf::from).collect(),
    });
    // Each connection holds a worker for at most about 4 s (a 3 s limit for the request,
    // checked after reads that wait at most 1 s). With few workers, a few dozen slow
    // connections kept every one busy and the page got no answer (the audit of 0.45.6).
    let (tx, rx) = mpsc::sync_channel::<(TcpStream, Instant)>(QUEUE);
    let rx = Arc::new(Mutex::new(rx));
    for _ in 0..WORKERS {
        let rx = rx.clone();
        let server = server.clone();
        thread::spawn(move || loop {
            let (stream, accepted) = match rx.lock().unwrap().recv() {
                Ok(s) => s,
                Err(_) => break,
            };
            // A request that panics must not leave one worker fewer each time, until
            // the server takes connections it never answers: exit, and systemd starts
            // a fresh one (Restart=always) within seconds.
            let served = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                handle(stream, &server, accepted)
            }));
            if served.is_err() {
                std::process::exit(70);
            }
        });
    }
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let _ = tx.try_send((s, Instant::now()));
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
        // One person's login is on all their devices: the device's address comes first.
        assert_eq!(
            source_of(&[b"amy@x"], &[b"100.64.0.7"]).as_deref(),
            Some("ip:100.64.0.7")
        );
        assert_eq!(
            source_of(&[b"amy@x"], &[b"100.64.0.8"]).as_deref(),
            Some("ip:100.64.0.8")
        );
        // Without an address, the login is used.
        assert_eq!(
            source_of(&[b"bo@example.com"], &[]).as_deref(),
            Some("login:bo@example.com")
        );
        assert_eq!(
            source_of(&[b"bo@example.com"], &[b"not an address"]).as_deref(),
            Some("login:bo@example.com")
        );
        // Two logins, or one that isn't plain text, don't count as one.
        assert_eq!(source_of(&[b"a@x", b"b@x"], &[]), None);
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
        test_server_with(assets, root, control, public, vec![])
    }
    /// The same, reading the connection tables from `tables` (none: none can be read).
    fn test_server_with(
        assets: PathBuf,
        root: &Path,
        control: PathBuf,
        public: Option<&str>,
        tables: Vec<PathBuf>,
    ) -> u16 {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = Server {
            assets,
            status: root.join("status.json"),
            control,
            port,
            public: public.map(str::to_string),
            live: Mutex::new(Live::default()),
            tables,
        };
        thread::spawn(move || {
            for s in listener.incoming().flatten() {
                handle(s, &server, Instant::now());
            }
        });
        port
    }
    #[test]
    fn a_connection_that_waited_out_its_time_in_the_queue_is_closed() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let mut client = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let (accepted, _) = listener.accept().unwrap();
        let root = std::env::temp_dir().join(format!("neko-queue-{}", std::process::id()));
        let server = Server {
            assets: root.join("assets"),
            status: root.join("status.json"),
            control: root.join("control.sock"),
            port,
            public: None,
            live: Mutex::new(Live::default()),
            tables: vec![],
        };
        let started = Instant::now();
        handle(accepted, &server, Instant::now() - REQUEST_TIME);
        assert!(
            started.elapsed() < Duration::from_millis(500),
            "no worker time spent on it"
        );
        let mut answer = vec![];
        let _ = client.set_read_timeout(Some(Duration::from_secs(1)));
        assert_eq!(client.read_to_end(&mut answer).unwrap_or(0), 0);
    }
    /// Requests against a real server on a free port, with a temporary snapshot.
    #[test]
    fn the_server_answers_with_pages_readings_and_coded_errors() {
        let root = std::env::temp_dir().join(format!("neko-serve-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("assets")).unwrap();
        for file in asset_files() {
            fs::write(root.join("assets").join(&file), format!("asset {file}")).unwrap();
        }
        let port = test_server(root.join("assets"), &root, root.join("control.sock"), None);
        let ask = |path: &str, host: &str, more: &str| {
            let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
            write!(s, "GET {path} HTTP/1.1\r\nHost: {host}\r\n{more}\r\n").unwrap();
            let mut out = String::new();
            s.read_to_string(&mut out).unwrap();
            out
        };
        let get = |path: &str, host: &str| ask(path, host, "");
        let host = format!("127.0.0.1:{port}");
        assert!(get("/", &host).ends_with("asset index.html"));
        assert!(get("/manifest.webmanifest", &host).contains("application/manifest+json"));
        // The review of 0.47.0: a file the browser already has is not sent again.
        let first = get("/style.css", &host);
        let tag = first
            .lines()
            .find_map(|line| line.strip_prefix("ETag: "))
            .expect("a fingerprint")
            .to_string();
        let again = ask("/style.css", &host, &format!("If-None-Match: {tag}\r\n"));
        assert!(again.starts_with("HTTP/1.1 304 Not Modified"), "{again}");
        // The cats are served from shared.json's lists, and nothing else that looks like one.
        assert!(get("/cat-breezy-small.webp", &host).contains("image/webp"));
        // Every cat has its big animation since 0.47.2 (the stand-in for a small one).
        assert!(get("/cat-breezy.webp", &host).contains("image/webp"));
        assert!(get("/cat-tiger.webp", &host).contains("E40")); // not one of the cats
        assert!(get("/cat-../index.html", &host).contains("E40"));
        assert!(again.ends_with("\r\n\r\n"), "{again}");
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
        // Opened from a link in another app (the phone setup page's QR code, a chat): the
        // page loads; the readings stay refused from another site, and a wrong Host still is.
        let link = "Sec-Fetch-Site: cross-site\r\nSec-Fetch-Mode: navigate\r\nSec-Fetch-Dest: document\r\n";
        assert!(ask("/", &host, link).ends_with("asset index.html"));
        assert!(ask("/api/status", &host, link).contains("\"E41\""));
        assert!(ask("/", "evil.example:80", link).contains("\"E41\""));
        // The same page load once the service worker passes it on (Dest: empty).
        let again =
            "Sec-Fetch-Site: cross-site\r\nSec-Fetch-Mode: navigate\r\nSec-Fetch-Dest: empty\r\n";
        assert!(ask("/", &host, again).ends_with("asset index.html"));
        assert!(ask("/api/status", &host, again).contains("\"E41\""));
        let fetched =
            "Sec-Fetch-Site: cross-site\r\nSec-Fetch-Mode: cors\r\nSec-Fetch-Dest: empty\r\n";
        assert!(ask("/", &host, fetched).contains("\"E41\""));
        assert!(ask("/api/status", &host, fetched).contains("\"E41\""));
        let live = get("/api/live", &host);
        assert!(live.contains("neko-live-v1"), "{live}");
        assert!(get("/api/processes", &host).contains("neko-processes-v1"));
        let _ = fs::remove_dir_all(&root);
    }

    /// A stand-in control service that answers with the request it was given.
    fn stand_in_control(socket: &Path) {
        let service = std::os::unix::net::UnixListener::bind(socket).unwrap();
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
    }
    /// The request the stand-in control service was given, from the server's reply.
    fn seen(reply: &str) -> Value {
        serde_json::from_str::<Value>(reply.split("\r\n\r\n").nth(1).unwrap()).unwrap()["seen"]
            .clone()
    }
    /// A connection table line as the kernel writes it (IPv4, this computer's order).
    fn table_line(client: SocketAddr, server: SocketAddr, uid: u32) -> String {
        let hex = |a: SocketAddr| match a.ip() {
            IpAddr::V4(v4) => format!("{:08X}:{:04X}", u32::from_ne_bytes(v4.octets()), a.port()),
            IpAddr::V6(_) => unreachable!(),
        };
        format!(
            "   1: {} {} 01 00000000:00000000 00:00000000 00000000  {uid:>4}        0 4242 1 0 20 4 30 10 -1",
            hex(client),
            hex(server)
        )
    }
    const TABLE_HEAD: &str = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode";
    #[test]
    fn the_owner_of_a_connection_is_read_from_the_kernels_tables() {
        let client: SocketAddr = "127.0.0.1:53412".parse().unwrap();
        let server: SocketAddr = "127.0.0.1:8787".parse().unwrap();
        let v4 = format!(
            "{TABLE_HEAD}\n{}\n{}\n",
            table_line(server, client, 61234), // the server's own end: not the client's
            table_line(client, server, 1000)
        );
        if cfg!(target_endian = "little") {
            // As the kernel writes 127.0.0.1:53412 on a PC (x86 and ARM are little-endian).
            let line = table_line(client, server, 7);
            assert_eq!(line.split_whitespace().nth(1), Some("0100007F:D0A4"));
        }
        assert_eq!(
            peer_uid(std::slice::from_ref(&v4), client, server),
            Some(1000)
        );
        assert_eq!(
            peer_uid(std::slice::from_ref(&v4), server, client),
            Some(61234)
        );
        assert_eq!(
            peer_uid(&[v4], "127.0.0.1:1".parse().unwrap(), server),
            None
        );
        // IPv6 tables, with 127.0.0.1 written the IPv6 way (::ffff:127.0.0.1).
        let mapped = |port: u16| {
            let mut bytes = [0u8; 16];
            bytes[10..12].copy_from_slice(&[0xff, 0xff]);
            bytes[12..].copy_from_slice(&[127, 0, 0, 1]);
            let words = (0..4)
                .map(|i| {
                    format!(
                        "{:08X}",
                        u32::from_ne_bytes(bytes[i * 4..i * 4 + 4].try_into().unwrap())
                    )
                })
                .collect::<String>();
            format!("{words}:{port:04X}")
        };
        let v6 = format!(
            "{TABLE_HEAD}\n   0: {} {} 01 00000000:00000000 00:00000000 00000000     0        0 77 1\n",
            mapped(53412),
            mapped(8787)
        );
        assert_eq!(peer_uid(&[String::new(), v6], client, server), Some(0));
        assert_eq!(
            peer_uid(&[format!("{TABLE_HEAD}\n garbage\n")], client, server),
            None
        );
    }
    /// A program on the server itself that sends Tailscale's headers is counted as
    /// itself, never as a device on the tailnet; Tailscale (root) is a device.
    #[test]
    fn a_local_program_posing_as_tailscale_is_counted_as_itself() {
        let root = std::env::temp_dir().join(format!("neko-program-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let socket = root.join("control.sock");
        let table = root.join("tcp");
        let port = test_server_with(
            root.clone(),
            &root,
            socket.clone(),
            None,
            vec![table.clone()],
        );
        stand_in_control(&socket);
        let local = format!("127.0.0.1:{port}");
        for (uid, source) in [(1000, "program:1000"), (0, "ip:100.64.0.7")] {
            let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
            let line = table_line(s.local_addr().unwrap(), s.peer_addr().unwrap(), uid);
            fs::write(&table, format!("{TABLE_HEAD}\n{line}\n")).unwrap();
            let body = r#"{"pin":"1234"}"#;
            write!(s, "POST /api/control/unlock HTTP/1.1\r\nHost: {local}\r\nOrigin: http://{local}\r\nSec-Fetch-Site: same-origin\r\nX-Forwarded-For: 100.64.0.7\r\nTailscale-User-Login: amy@x\r\nContent-Length: {}\r\n\r\n{body}", body.len()).unwrap();
            let mut out = String::new();
            let _ = s.read_to_string(&mut out);
            assert_eq!(seen(&out)["source"], source, "{out}");
            assert_eq!(seen(&out)["local"], false);
        }
        let _ = fs::remove_dir_all(&root);
    }
    /// Fan and light requests reach the control service only from the page itself,
    /// with `local` decided by the server and never by the page.
    #[test]
    fn control_requests_are_checked_and_forwarded() {
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
        stand_in_control(&socket);
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
        assert_eq!(seen(&reply)["source"], "ip:100.64.0.9", "{reply}");
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
        // A form on another site sent as a page load (navigate) is a change all the same.
        let navigated = "Sec-Fetch-Site: cross-site
Sec-Fetch-Mode: navigate
Sec-Fetch-Dest: document
";
        assert!(post("/api/control/fan", &local, &here, navigated, "{}").contains("\"E41\""));
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
            tables: vec![],
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
