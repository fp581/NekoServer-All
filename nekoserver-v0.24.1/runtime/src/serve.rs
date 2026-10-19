use crate::{
    common::{self, Error, Result},
    Args,
};
use std::{
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::Path,
    sync::{mpsc, Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
const CSP:&str="default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; object-src 'none'";
fn reply(stream: &mut TcpStream, code: u16, body: &[u8], mime: &str, head: bool) {
    let reason = match code {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        431 => "Request Header Fields Too Large",
        _ => "Service Unavailable",
    };
    let header=format!("HTTP/1.1 {code} {reason}\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nContent-Security-Policy: {CSP}\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n",body.len());
    let _ = stream.write_all(header.as_bytes());
    if !head {
        let _ = stream.write_all(body);
    }
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
fn handle(mut stream: TcpStream, assets: &Path, status: &Path, port: u16, public: Option<&str>) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
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
    if !matches!(req.parse(&buf), Ok(httparse::Status::Complete(_))) {
        reply(&mut stream, 400, b"", "application/json", false);
        return;
    }
    let method = req.method.unwrap_or("");
    let head = method == "HEAD";
    if method != "GET" && !head {
        reply(&mut stream, 405, b"", "application/json", false);
        return;
    }
    let (allowed_hosts, origins) = allowed(port, public);
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
    {
        reply(&mut stream, 403, b"", "application/json", head);
        return;
    }
    if !values("transfer-encoding").is_empty()
        || cl.len() > 1
        || cl.first().map(|v| *v != b"0").unwrap_or(false)
    {
        reply(&mut stream, 400, b"", "application/json", head);
        return;
    }
    let path = req.path.unwrap_or("").split('?').next().unwrap_or("");
    #[cfg(feature = "test-fixtures")]
    if path == "/__fixture_metrics" {
        let mut metrics = serde_json::json!({"fixture":true});
        if let Ok(status) = fs::read_to_string("/proc/self/status") {
            for (key, label) in [("VmRSS:", "rss_kib"), ("Threads:", "threads")] {
                metrics[label] = serde_json::json!(status
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
    let result = if path == "/api/status" {
        common::bytes(status, 2_000_000).and_then(|b| {
            let v: serde_json::Value = serde_json::from_slice(&b)?;
            if !v.is_object() || v["format"] != "neko-dashboard-v1" {
                return Err(Error::new("Invalid snapshot"));
            }
            Ok((b, "application/json"))
        })
    } else {
        let asset = match path {
            "/" | "/index.html" => Some(("index.html", "text/html; charset=utf-8")),
            "/style.css" => Some(("style.css", "text/css; charset=utf-8")),
            "/app.js" => Some(("app.js", "text/javascript; charset=utf-8")),
            "/logo.svg" => Some(("logo.svg", "image/svg+xml")),
            _ => None,
        };
        match asset {
            Some((file, mime)) => common::bytes(&assets.join(file), 2_000_000).map(|b| (b, mime)),
            None => {
                reply(&mut stream, 404, b"", "application/json", head);
                return;
            }
        }
    };
    match result {
        Ok((b, mime)) => reply(&mut stream, 200, &b, mime, head),
        Err(_) => reply(
            &mut stream,
            503,
            b"{\"status\":\"unavailable\"}",
            "application/json",
            head,
        ),
    }
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
    let (tx, rx) = mpsc::sync_channel::<TcpStream>(32);
    let rx = Arc::new(Mutex::new(rx));
    for _ in 0..4 {
        let rx = rx.clone();
        let assets = assets.clone();
        let status = status.clone();
        let public = public.clone();
        thread::spawn(move || loop {
            let stream = match rx.lock().unwrap().recv() {
                Ok(s) => s,
                Err(_) => break,
            };
            handle(stream, &assets, &status, port, public.as_deref());
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
}
