//! Scheduled, GET-only local API audit. Never persists credentials or raw responses.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::Path,
    time::{Duration, Instant},
};

const APPS: [(&str, u16); 8] = [
    ("jellyfin", 8096),
    ("navidrome", 4533),
    ("sonarr", 8989),
    ("radarr", 7878),
    ("lidarr", 8686),
    ("prowlarr", 9696),
    ("seerr", 5055),
    ("qbittorrent", 8080),
];
const LIMIT: usize = 1_900_000;
fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
fn port(app: &str) -> Result<u16> {
    let value = APPS
        .iter()
        .find(|(name, _)| *name == app)
        .map(|(_, p)| *p)
        .ok_or_else(|| Error::new("Unknown media service"))?;
    #[cfg(feature = "test-fixtures")]
    if let Ok(raw) = std::env::var(format!("NEKO_TEST_PORT_{}", app.to_ascii_uppercase())) {
        return raw.parse().map_err(|_| Error::new("Invalid fixture port"));
    }
    Ok(value)
}
fn decoded_chunks(mut bytes: &[u8]) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let n = bytes
            .windows(2)
            .position(|w| w == b"\r\n")
            .ok_or_else(|| Error::new("Invalid HTTP chunks"))?;
        let line =
            std::str::from_utf8(&bytes[..n]).map_err(|_| Error::new("Invalid chunk length"))?;
        let count = usize::from_str_radix(line.split(';').next().unwrap_or(""), 16)
            .map_err(|_| Error::new("Invalid chunk length"))?;
        bytes = &bytes[n + 2..];
        if count == 0 {
            return Ok(out);
        }
        if count > LIMIT
            || bytes.len() < count + 2
            || &bytes[count..count + 2] != b"\r\n"
            || out.len() + count > LIMIT
        {
            return Err(Error::new("Invalid HTTP chunks"));
        }
        out.extend_from_slice(&bytes[..count]);
        bytes = &bytes[count + 2..];
    }
}
fn get(app: &str, endpoint: &str, auth: Option<(&str, &str)>) -> Result<Value> {
    if !endpoint.starts_with('/') || endpoint.starts_with("//") || endpoint.contains(['\r', '\n']) {
        return Err(Error::new("Invalid local endpoint"));
    }
    let port = port(app)?;
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let start = Instant::now();
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(3))?;
    stream.set_write_timeout(Some(Duration::from_secs(3)))?;
    let mut request=format!("GET {endpoint} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: application/json\r\nUser-Agent: NekoServer-MediaHealth\r\n");
    if let Some((header, key)) = auth {
        if !["X-Api-Key", "X-Emby-Token"].contains(&header)
            || key.is_empty()
            || !key.bytes().all(|b| b.is_ascii_graphic())
        {
            return Err(Error::new("Invalid local credential"));
        }
        request.push_str(&format!("{header}: {key}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes())?;
    let mut bytes = Vec::new();
    let mut buf = [0u8; 8192];
    let mut header_end = None;
    loop {
        let left = Duration::from_secs(5)
            .checked_sub(start.elapsed())
            .ok_or_else(|| Error::new("API deadline"))?;
        stream.set_read_timeout(Some(left))?;
        let count = stream.read(&mut buf)?;
        if count == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..count]);
        if bytes.len() > LIMIT {
            return Err(Error::new("API response too large"));
        }
        if header_end.is_none() {
            header_end = bytes
                .windows(4)
                .position(|b| b == b"\r\n\r\n")
                .map(|n| n + 4);
        }
        if header_end.is_none() && bytes.len() > 32768 {
            return Err(Error::new("API headers too large"));
        }
    }
    let end = header_end.ok_or_else(|| Error::new("Incomplete HTTP response"))?;
    let mut headers = [httparse::EMPTY_HEADER; 64];
    let mut response = httparse::Response::new(&mut headers);
    if !response
        .parse(&bytes[..end])
        .map_err(|_| Error::new("Invalid HTTP response"))?
        .is_complete()
        || response.code != Some(200)
    {
        return Err(Error::new("API returned non-success status"));
    }
    let lengths = response
        .headers
        .iter()
        .filter(|h| h.name.eq_ignore_ascii_case("content-length"))
        .collect::<Vec<_>>();
    let encodings = response
        .headers
        .iter()
        .filter(|h| h.name.eq_ignore_ascii_case("transfer-encoding"))
        .collect::<Vec<_>>();
    if lengths.len() > 1 || encodings.len() > 1 || (!lengths.is_empty() && !encodings.is_empty()) {
        return Err(Error::new("Ambiguous HTTP framing"));
    }
    let raw = &bytes[end..];
    let body = if let Some(h) = encodings.first() {
        if !h.value.eq_ignore_ascii_case(b"chunked") {
            return Err(Error::new("Unsupported HTTP encoding"));
        }
        decoded_chunks(raw)?
    } else {
        if let Some(h) = lengths.first() {
            let expected = std::str::from_utf8(h.value)
                .ok()
                .and_then(|n| n.trim().parse::<usize>().ok())
                .ok_or_else(|| Error::new("Invalid response length"))?;
            if expected != raw.len() {
                return Err(Error::new("Incomplete API body"));
            }
        }
        raw.to_vec()
    };
    serde_json::from_slice(&body).map_err(Into::into)
}
fn arr_key(data: &Path, app: &str) -> Option<String> {
    let raw = common::bytes(&data.join(format!("appdata/{app}/config.xml")), 64000).ok()?;
    let raw = std::str::from_utf8(&raw).ok()?;
    let re = regex::Regex::new(r"<ApiKey>\s*([A-Za-z0-9_-]{16,128})\s*</ApiKey>").ok()?;
    let found = re.captures_iter(raw).collect::<Vec<_>>();
    if found.len() != 1 {
        return None;
    }
    Some(found[0][1].to_string())
}
fn arr(app: &str, key: &str, endpoint: &str) -> Result<Value> {
    let version = if ["lidarr", "prowlarr"].contains(&app) {
        "v1"
    } else {
        "v3"
    };
    get(
        app,
        &format!("/api/{version}/{endpoint}"),
        Some(("X-Api-Key", key)),
    )
}
fn seerr(key: &str, endpoint: &str) -> Result<Value> {
    get(
        "seerr",
        &format!("/api/v1{endpoint}"),
        Some(("X-Api-Key", key)),
    )
}
fn array(v: &Value) -> Result<&Vec<Value>> {
    let rows = v
        .as_array()
        .ok_or_else(|| Error::new("API list required"))?;
    if rows.iter().any(|r| !r.is_object()) {
        return Err(Error::new("API records required"));
    }
    Ok(rows)
}
fn field<'a>(record: &'a Value, key: &str) -> &'a Value {
    record["fields"]
        .as_array()
        .and_then(|rows| rows.iter().find(|r| r["name"] == key))
        .map(|r| &r["value"])
        .unwrap_or(&Value::Null)
}
fn directory(app: &str) -> &str {
    match app {
        "sonarr" => "/data/tv",
        "radarr" => "/data/movies",
        _ => "/data/music",
    }
}
fn configured(app: &str, endpoint: &str, rows: &[Value], selected: &[&str], data: &Path) -> bool {
    match endpoint {
        "rootfolder" => rows
            .iter()
            .any(|r| r["path"] == directory(app) && r["accessible"] == true),
        "downloadclient" => rows.iter().any(|r| {
            r["enable"] == true
                && text(&r["implementation"]).eq_ignore_ascii_case("qbittorrent")
                && field(r, "host") == "qbittorrent"
                && (field(r, "port") == 8080 || field(r, "port") == "8080")
                && field(r, "category")
                    == match app {
                        "sonarr" => "tv",
                        "radarr" => "movies",
                        _ => "music",
                    }
        }),
        "applications" => ["sonarr", "radarr", "lidarr"]
            .iter()
            .filter(|a| selected.contains(a))
            .all(|a| {
                let Some(key) = arr_key(data, a) else {
                    return false;
                };
                rows.iter().any(|r| {
                    ["addOnly", "fullSync"].contains(&text(&r["syncLevel"]))
                        && text(field(r, "baseUrl")).trim_end_matches('/')
                            == format!(
                                "http://{a}:{}",
                                APPS.iter().find(|(n, _)| n == a).unwrap().1
                            )
                        && text(field(r, "prowlarrUrl")).trim_end_matches('/')
                            == "http://prowlarr:9696"
                        && text(field(r, "apiKey")) == key
                })
            }),
        "indexer" => rows.iter().any(|r| {
            if app == "prowlarr" {
                r["enable"] == true
            } else {
                r["enableRss"] == true
                    || r["enableAutomaticSearch"] == true
                    || r["enableInteractiveSearch"] == true
            }
        }),
        _ => false,
    }
}
fn readiness_row(rows: &mut Value, id: &str, state: &str, reason: &str) {
    rows[id] = json!({"result":state,"reason":reason});
}
fn provider(data: &Path, key: &str, app: &str) -> Result<bool> {
    let values = seerr(key, &format!("/settings/{app}"))?;
    let values = array(&values)?;
    if values.iter().any(|v| !v["is4k"].is_boolean()) {
        return Err(Error::new("Provider type missing"));
    }
    let standard = values
        .iter()
        .filter(|v| v["is4k"] == false)
        .collect::<Vec<_>>();
    if standard.len() != 1 {
        return Ok(false);
    }
    let p = standard[0];
    let Some(manager_key) = arr_key(data, app) else {
        return Ok(false);
    };
    let status = arr(app, &manager_key, "system/status")?;
    let major = text(&status["version"])
        .split('.')
        .next()
        .unwrap_or("")
        .parse::<u32>()
        .unwrap_or(0);
    let base = text(&status["urlBase"]);
    if major < 3
        || (app == "sonarr" && major > 4)
        || base.contains(['?', '#', ':', '\\'])
        || base.starts_with("//")
        || base.split('/').any(|s| s == "..")
        || (!base.is_empty() && !base.starts_with('/'))
    {
        return Ok(false);
    }
    let normal_port = APPS.iter().find(|(n, _)| *n == app).unwrap().1;
    if p["isDefault"] != true
        || p["hostname"] != app
        || p["port"] != normal_port
        || p["apiKey"] != manager_key
        || p["useSsl"] != false
        || text(&p["baseUrl"]) != base
        || text(&p["activeDirectory"]).trim_end_matches('/') != directory(app)
        || p["preventSearch"] != false
    {
        return Ok(false);
    }
    let roots = arr(app, &manager_key, "rootfolder")?;
    let roots = array(&roots)?
        .iter()
        .filter(|r| text(&r["path"]).trim_end_matches('/') == directory(app))
        .collect::<Vec<_>>();
    if roots.len() != 1 || roots[0]["accessible"] != true {
        return Ok(false);
    }
    let local = arr(app, &manager_key, "qualityprofile")?;
    let local = array(&local)?;
    if local.is_empty() || !local.iter().any(|v| v["id"] == p["activeProfileId"]) {
        return Ok(false);
    }
    let id = p["id"]
        .as_u64()
        .ok_or_else(|| Error::new("Provider ID missing"))?;
    let live = seerr(key, &format!("/service/{app}/{id}"))?;
    let remote = array(&live["profiles"])?;
    if remote.len() != local.len()
        || !local.iter().all(|v| {
            remote
                .iter()
                .any(|r| r["id"] == v["id"] && r["name"] == v["name"])
        })
        || text(&live["urlBase"]) != base
        || !array(&live["rootFolders"])?
            .iter()
            .any(|r| text(&r["path"]).trim_end_matches('/') == directory(app))
    {
        return Ok(false);
    }
    if app == "sonarr"
        && major == 3
        && !array(&live["languageProfiles"])?
            .iter()
            .any(|r| r["id"] == p["activeLanguageProfileId"])
    {
        return Ok(false);
    }
    Ok(true)
}
fn encoded(value: &str) -> String {
    value
        .bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
fn music_catalogue(data: &Path) -> Result<&'static str> {
    use std::os::unix::fs::MetadataExt;
    let path = common::safe(&data.join("appdata/navidrome/nekoserver-client.json"))?;
    if !path.exists() {
        return Ok("needs_setup");
    }
    let m = std::fs::metadata(&path)?;
    if m.uid() != 0 || m.mode() & 0o077 != 0 {
        return Err(Error::new("Private music credential required"));
    }
    let value: Value = serde_json::from_slice(&common::bytes(&path, 64000)?)?;
    let user = text(&value["user"]);
    let salt = text(&value["salt"]);
    let token = text(&value["token"]);
    if value["format"] != "neko-music-client-v1"
        || user.trim().is_empty()
        || user.chars().count() > 128
        || user.chars().any(char::is_control)
        || [salt, token].iter().any(|s| {
            s.len() != 32
                || !s
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        })
    {
        return Err(Error::new("Invalid listener token"));
    }
    let call = |endpoint: &str, extra: &str| -> Result<Value> {
        let query = format!(
            "/rest/{endpoint}.view?u={}&t={token}&s={salt}&v=1.16.1&c=NekoServer&f=json{extra}",
            encoded(user)
        );
        let response = get("navidrome", &query, None)?;
        if response["subsonic-response"]["status"] != "ok" {
            return Err(Error::new("Music authentication failed"));
        }
        Ok(response["subsonic-response"].clone())
    };
    call("ping", "")?;
    let info = call("getUser", &format!("&username={}", encoded(user)))?;
    if info["user"]["username"] != user || info["user"]["adminRole"] != false {
        return Err(Error::new("Non-admin listener required"));
    }
    let folders = call("getMusicFolders", "")?;
    if folders["musicFolders"]["musicFolder"]
        .as_array()
        .is_none_or(|a| a.is_empty())
    {
        return Err(Error::new("Music folder missing"));
    }
    let albums = call("getAlbumList2", "&type=newest&size=1")?;
    if !albums["albumList2"].is_object()
        || albums["albumList2"]
            .get("album")
            .is_some_and(|a| !a.is_array())
    {
        return Err(Error::new("Invalid music catalogue"));
    }
    Ok("pass")
}

fn readiness(data: &Path, selected: &[&str]) -> Value {
    let mut rows = json!({});
    let settings = if selected.contains(&"seerr") {
        common::json(&data.join("appdata/seerr/settings.json"), 2_000_000)
    } else {
        json!({})
    };
    let saved = &settings["jellyfin"];
    let mut server_id = String::new();
    let mut ids = Vec::new();
    if selected.contains(&"jellyfin") {
        let outcome = (|| -> Result<bool> {
            let info = get("jellyfin", "/System/Info/Public", None)?;
            server_id = text(&info["Id"]).to_string();
            if server_id.is_empty() {
                return Err(Error::new("Jellyfin identity missing"));
            }
            if info["StartupWizardCompleted"] != true
                || text(
                    saved
                        .get("serverId")
                        .or_else(|| saved.get("serverID"))
                        .unwrap_or(&Value::Null),
                ) != server_id
                || text(&saved["apiKey"]).is_empty()
            {
                return Ok(false);
            }
            let values = get(
                "jellyfin",
                "/Library/VirtualFolders",
                Some(("X-Emby-Token", text(&saved["apiKey"]))),
            )?;
            let libraries = array(&values)?;
            for (dir, kind) in [("/data/movies", "movies"), ("/data/tv", "tvshows")] {
                let found = libraries
                    .iter()
                    .filter(|v| {
                        v["Locations"].as_array().is_some_and(|locations| {
                            locations
                                .iter()
                                .any(|p| text(p).trim_end_matches('/') == dir)
                        })
                    })
                    .collect::<Vec<_>>();
                if found.len() != 1
                    || found[0]["CollectionType"] != kind
                    || text(&found[0]["ItemId"]).is_empty()
                {
                    return Ok(false);
                }
                ids.push(text(&found[0]["ItemId"]).to_string());
            }
            Ok(true)
        })();
        let state = match outcome {
            Ok(true) => "pass",
            Ok(false) => "needs_setup",
            Err(_) => "fail",
        };
        readiness_row(
            &mut rows,
            "jellyfin:current_libraries",
            state,
            "Check first-run account, saved server identity and authenticated movie/TV libraries.",
        );
    }
    if selected.contains(&"seerr") {
        let key = text(&settings["main"]["apiKey"]);
        let outcome = (|| -> Result<bool> {
            if key.is_empty() {
                return Ok(false);
            }
            if seerr(key, "/settings/public")?["initialized"] != true {
                return Ok(false);
            }
            let link = seerr(key, "/settings/jellyfin")?;
            if server_id.is_empty()
                || text(
                    link.get("serverId")
                        .or_else(|| link.get("serverID"))
                        .unwrap_or(&Value::Null),
                ) != server_id
            {
                return Ok(false);
            }
            let libraries = seerr(key, "/settings/jellyfin/library")?;
            Ok(ids.len() == 2
                && ids.iter().all(|id| {
                    array(&libraries).is_ok_and(|rows| {
                        rows.iter()
                            .any(|r| text(&r["id"]) == id && r["enabled"] == true)
                    })
                }))
        })();
        readiness_row(
            &mut rows,
            "seerr:current_libraries",
            match outcome {
                Ok(true) => "pass",
                Ok(false) => "needs_setup",
                Err(_) => "fail",
            },
            "Check Seerr login, Jellyfin identity and enabled movie/TV libraries.",
        );
        for app in ["sonarr", "radarr"].iter().filter(|a| selected.contains(a)) {
            let outcome = if key.is_empty() {
                Ok(false)
            } else {
                provider(data, key, app)
            };
            readiness_row(
                &mut rows,
                &format!("seerr:provider:{app}"),
                match outcome {
                    Ok(true) => "pass",
                    Ok(false) => "needs_setup",
                    Err(_) => "fail",
                },
                "Verify saved default routing, live profiles and root folders through Seerr.",
            );
        }
    }
    if selected.contains(&"navidrome") {
        let state = music_catalogue(data).unwrap_or("fail");
        readiness_row(
            &mut rows,
            "navidrome:catalogue",
            state,
            "Authenticated non-admin listener/library check; audio playback remains witnessed.",
        );
    }
    for app in ["qbittorrent", "navidrome"]
        .iter()
        .filter(|a| selected.contains(a))
    {
        readiness_row(
            &mut rows,
            &format!("{app}:authenticated_playback_or_transfer"),
            "needs_setup",
            "Current authenticated transfer/playback needs a server acceptance test.",
        );
    }
    readiness_row(
        &mut rows,
        "end_to_end_media",
        "needs_setup",
        "Complete a real request, acquisition, import and playback test on the deployed server.",
    );
    rows
}
fn inspect(app: &str, data: &Path, media: &Path) -> Result<Value> {
    let filter = format!("label=com.docker.compose.project=neko-{app}");
    let service = format!("label=com.docker.compose.service={app}");
    let (code, ids) = common::command(
        &[
            "docker", "ps", "-aq", "--filter", &filter, "--filter", &service,
        ],
        10,
    )?;
    let ids = ids.lines().collect::<Vec<_>>();
    if code != 0 || ids.len() != 1 {
        return Err(Error::new("Container missing or ambiguous"));
    }
    let (code, raw) = common::command(&["docker", "inspect", ids[0]], 10)?;
    if code != 0 {
        return Err(Error::new("Container inspection failed"));
    }
    let values: Value = serde_json::from_str(&raw)?;
    let item = array(&values)?
        .first()
        .ok_or_else(|| Error::new("Container missing"))?;
    let running = item["State"]["Status"] == "running";
    let health = item["State"]["Health"]["Status"]
        .as_str()
        .unwrap_or("not_defined");
    let network = item["NetworkSettings"]["Networks"]
        .get("nekoserver-media")
        .is_some();
    let mounts = array(&item["Mounts"])?;
    let expected = data.join(format!("appdata/{app}"));
    let appdata = mounts.iter().any(|m| {
        Path::new(text(&m["Source"])) == expected
            && m["Destination"]
                == match app {
                    "seerr" => "/app/config",
                    "navidrome" => "/data",
                    _ => "/config",
                }
    });
    let expected_media = match app {
        "jellyfin" => vec![("movies", "/data/movies", false), ("tv", "/data/tv", false)],
        "navidrome" => vec![("music", "/music", false)],
        // Lidarr only handles music: one SSD mount keeps downloads and library on the same mount.
        "lidarr" => vec![("", "/data", true)],
        "sonarr" | "radarr" | "qbittorrent" => vec![("", "/data", true)],
        _ => vec![],
    };
    let media_ok = expected_media.iter().all(|(folder, dest, rw)| {
        mounts.iter().any(|m| {
            Path::new(text(&m["Source"]))
                == (if app == "navidrome" || app == "lidarr" {
                    data
                } else {
                    media
                })
                .join("library")
                .join(folder)
                && m["Destination"] == *dest
                && m["RW"] == *rw
        })
    });
    let ssd_music_ok = app != "qbittorrent"
        || mounts.iter().any(|m| {
            m["Source"]
                == data
                    .join("library/downloads/music")
                    .to_string_lossy()
                    .as_ref()
                && m["Destination"] == "/data/downloads/music"
                && m["RW"] == true
        });
    let media_ok = media_ok && ssd_music_ok;
    // Reachability needs a valid HTTP response; authenticated JSON checks below prove APIs.
    let http_status = if running { reachability(app) } else { None };
    let reachable = http_status.is_some();
    Ok(
        json!({"result":if running&&health!="unhealthy"&&network&&appdata&&media_ok&&reachable {"pass"}else{"fail"},"container":item["State"]["Status"],"docker_health":health,"network":network,"appdata_mount":appdata,"media_mount":media_ok,"http_status":http_status}),
    )
}
fn reachability(app: &str) -> Option<u16> {
    // API JSON is not required at the HTML root. A separate bounded HTTP GET
    // probe accepts login redirects/auth challenges but does not follow them.
    let result = (|| -> Result<Option<u16>> {
        let port = port(app)?;
        let mut stream = TcpStream::connect_timeout(
            &SocketAddr::from(([127, 0, 0, 1], port)),
            Duration::from_secs(2),
        )?;
        stream.set_read_timeout(Some(Duration::from_secs(3)))?;
        stream.set_write_timeout(Some(Duration::from_secs(3)))?;
        stream.write_all(
            format!("GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
                .as_bytes(),
        )?;
        let mut buf = [0u8; 4096];
        let n = stream.read(&mut buf)?;
        let first = std::str::from_utf8(&buf[..n])
            .map_err(|_| Error::new("Invalid HTTP response"))?
            .lines()
            .next()
            .unwrap_or("")
            .to_string();
        let status = first
            .split_whitespace()
            .nth(1)
            .and_then(|s| s.parse::<u16>().ok())
            .unwrap_or(0);
        Ok(
            if (200..400).contains(&status) || [401, 403].contains(&status) {
                Some(status)
            } else {
                None
            },
        )
    })();
    result.unwrap_or(None)
}
pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let config = a.path("config")?;
    let cfg = common::json(&config, 2_000_000);
    let data = common::mount_path(text(&cfg["data_mount"]))?;
    let media = common::mount_path(text(&cfg["media_mount"]))?;
    backup::guard(&config, &data, &media)?;
    let output = common::safe(&a.path("output")?)?;
    if output != data.join("reports/media/latest.json") {
        return Err(Error::new(
            "Media report must use its fixed private destination",
        ));
    }
    let _lock = common::lock(Path::new("/run/nekoserver-media-health.lock"))?;
    let selected = cfg["media"]["services"]
        .as_array()
        .ok_or_else(|| Error::new("Media services required"))?
        .iter()
        .map(text)
        .collect::<Vec<_>>();
    if selected.is_empty()
        || selected
            .iter()
            .any(|a| !APPS.iter().any(|(name, _)| name == a))
    {
        return Err(Error::new("Invalid media services"));
    }
    let mut services = json!({});
    let mut integrations = json!({});
    for app in &selected {
        services[app]=inspect(app,&data,&media).unwrap_or_else(|_|json!({"result":"fail","reason":"Container, mounts or local HTTP inspection failed"}));
    }
    for app in ["sonarr", "radarr", "lidarr", "prowlarr"]
        .iter()
        .filter(|a| selected.contains(a))
    {
        let Some(key) = arr_key(&data, app) else {
            integrations[app] =
                json!({"result":"needs_setup","reason":"Application API key unavailable"});
            continue;
        };
        let mut row = json!({"result":"pass","note":"Configuration checks only; imports and playback still require acceptance"});
        if arr(app, &key, "system/status").is_err() {
            row["result"] = json!("fail");
        } else {
            let endpoints = if *app == "prowlarr" {
                vec!["applications", "indexer"]
            } else {
                vec!["downloadclient", "indexer", "rootfolder"]
            };
            for endpoint in endpoints {
                match arr(app, &key, endpoint).and_then(|v| {
                    array(&v).map(|rows| configured(app, endpoint, rows, &selected, &data))
                }) {
                    Ok(valid) => {
                        row[format!("{endpoint}_configured")] = json!(valid);
                        if !valid && row["result"] == "pass" {
                            row["result"] = json!("needs_setup");
                        }
                    }
                    Err(_) => {
                        row[format!("{endpoint}_configured")] = Value::Null;
                        row["result"] = json!("fail");
                    }
                }
            }
        }
        integrations[app] = row;
    }
    let readiness = readiness(&data, &selected);
    let mut counts = json!({"pass":0,"warning":0,"fail":0,"needs_setup":0});
    for group in [&services, &integrations, &readiness] {
        for row in group.as_object().unwrap().values() {
            let key = text(&row["result"]);
            counts[key] = json!(counts[key].as_u64().unwrap_or(0) + 1);
        }
    }
    backup::guard(&config, &data, &media)?;
    common::atomic_json(
        &output,
        &json!({"generated_at":chrono::Utc::now().to_rfc3339(),"services":services,"integrations":integrations,"readiness":readiness,"summary":counts}),
    )?;
    if counts["fail"].as_u64().unwrap_or(0) > 0 {
        std::process::exit(2);
    }
    if counts["needs_setup"].as_u64().unwrap_or(0) > 0 {
        std::process::exit(3);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn chunked_json_is_decoded() {
        assert_eq!(
            decoded_chunks(b"7\r\n{\"a\":1}\r\n0\r\n\r\n").unwrap(),
            b"{\"a\":1}"
        );
    }
    #[test]
    fn chunk_extensions_and_multiple_chunks_are_supported() {
        assert_eq!(
            decoded_chunks(b"2;fixture=yes\r\n{}\r\n1\r\n\n\r\n0\r\n\r\n").unwrap(),
            b"{}\n"
        );
    }
    #[test]
    fn truncated_chunks_are_rejected() {
        assert!(decoded_chunks(b"7\r\n{}").is_err());
    }
    #[test]
    fn invalid_chunk_length_is_rejected() {
        assert!(decoded_chunks(b"no\r\n{}\r\n").is_err());
    }
    #[test]
    fn oversized_chunk_is_rejected_without_allocation() {
        assert!(decoded_chunks(b"ffffff\r\n").is_err());
    }
    #[test]
    fn unterminated_chunked_response_is_rejected() {
        assert!(decoded_chunks(b"2\r\n{}\r\n").is_err());
    }
    #[test]
    fn api_records_must_be_objects() {
        assert!(array(&json!([1, 2])).is_err());
        assert!(array(&json!({"unexpected":true})).is_err());
    }
}
