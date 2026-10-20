//! `nekoserver-runtime control`: the one part of NekoServer that changes hardware.
//!
//! Runs as root (writing fan speeds needs it). The dashboard's unprivileged web server
//! passes requests over a private socket (/run/nekoserver-control/control.sock, group
//! nekoserver-control). On the server itself changes need nothing more; from other
//! devices (Tailscale) they need the PIN chosen during setup.
//!
//! `control --restore` gives every fan back to the motherboard (systemd runs it whenever
//! the service ends, crash included); `control --set-pin` reads a new PIN from stdin.
use crate::{
    common::{self, Error, Result},
    fans::{self, Controller, Setting},
    openrgb, Args,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixListener},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

pub const SOCKET: &str = "/run/nekoserver-control/control.sock";
const RUN_DIR: &str = "/run/nekoserver-control";
const CONFIG_DIR: &str = "/etc/nekoserver-control";
const HWMON: &str = "/sys/class/hwmon";
const GROUP: &[u8] = b"nekoserver-control\0";
const PIN_ROUNDS: u32 = 200_000;
const TOKEN_LIFETIME: Duration = Duration::from_secs(12 * 3600);
const MAX_TRIES: usize = 5;
const LOCKOUT: Duration = Duration::from_secs(300);

static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn on_stop(_: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

/// Short coded answers, shown by the page like the server's other errors.
fn refuse(code: &str, message: &str) -> Value {
    json!({"ok": false, "error": code, "message": message})
}

/* ---------- PIN ---------- */
fn random_hex(bytes: usize) -> Result<String> {
    let mut buf = vec![0u8; bytes];
    fs::File::open("/dev/urandom")?.read_exact(&mut buf)?;
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}
/// A salted, slow hash: guessing a short PIN offline takes real time per try.
fn pin_hash(salt: &str, pin: &str, rounds: u32) -> String {
    let mut digest = Sha256::digest(format!("{salt}:{pin}").as_bytes());
    for _ in 1..rounds {
        digest = Sha256::digest(digest);
    }
    digest.iter().map(|b| format!("{b:02x}")).collect()
}
pub fn valid_pin(pin: &str) -> bool {
    (4..=8).contains(&pin.len()) && pin.bytes().all(|b| b.is_ascii_digit())
}
fn same(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}
pub fn pin_record(pin: &str, salt: &str) -> Value {
    json!({"format": "neko-pin-v1", "salt": salt, "rounds": PIN_ROUNDS, "hash": pin_hash(salt, pin, PIN_ROUNDS)})
}
pub fn pin_matches(record: &Value, pin: &str) -> bool {
    let (Some(salt), Some(hash), Some(rounds)) = (
        record["salt"].as_str(),
        record["hash"].as_str(),
        record["rounds"].as_u64(),
    ) else {
        return false;
    };
    record["format"] == "neko-pin-v1"
        && (1000..=10_000_000).contains(&rounds)
        && same(&pin_hash(salt, pin, rounds as u32), hash)
}

/// Who may change things: the server itself, or a device that unlocked with the PIN.
#[derive(Default)]
pub struct Gate {
    pin: Option<Value>,
    tokens: BTreeMap<String, Instant>,
    failures: Vec<Instant>,
}
impl Gate {
    pub fn new(pin: Option<Value>) -> Self {
        Gate {
            pin,
            ..Default::default()
        }
    }
    fn locked(&mut self) -> bool {
        self.failures.retain(|t| t.elapsed() < LOCKOUT);
        self.failures.len() >= MAX_TRIES
    }
    pub fn unlock(&mut self, pin: &str) -> Value {
        if self.locked() {
            return refuse("E45", "Too many tries; wait 5 minutes");
        }
        let Some(record) = &self.pin else {
            return refuse(
                "E43",
                "No PIN is set: run setup on the server to choose one",
            );
        };
        if !valid_pin(pin) || !pin_matches(record, pin) {
            self.failures.push(Instant::now());
            return refuse("E44", "Wrong PIN");
        }
        self.failures.clear();
        let token = random_hex(24).unwrap_or_default();
        if token.is_empty() {
            return refuse("E21", "Fan and light control unavailable");
        }
        self.tokens.retain(|_, t| t.elapsed() < TOKEN_LIFETIME);
        self.tokens.insert(token.clone(), Instant::now());
        json!({"ok": true, "token": token})
    }
    pub fn allowed(&mut self, request: &Value) -> bool {
        if request["local"] == true {
            return true;
        }
        self.tokens.retain(|_, t| t.elapsed() < TOKEN_LIFETIME);
        request["token"]
            .as_str()
            .is_some_and(|t| self.tokens.contains_key(t))
    }
    fn pin_set(&self) -> bool {
        self.pin.is_some()
    }
}

/* ---------- lighting ---------- */
/// The lighting chosen for a device, kept so it comes back after a restart.
fn device_key(d: &openrgb::Device) -> String {
    format!("{} · {}", d.name, d.description)
}
/// Send one device its chosen lighting: off, a steady colour, or an effect (with the
/// colour, when the effect takes one).
fn apply_light(
    client: &mut openrgb::Client,
    d: &openrgb::Device,
    want: &Value,
) -> std::io::Result<()> {
    let color = want["color"]
        .as_str()
        .and_then(openrgb::parse_color)
        .unwrap_or([255, 255, 255]);
    let brightness = want["brightness"].as_u64().unwrap_or(100).min(100) as u8;
    if want["off"] == true || brightness == 0 {
        return client.set_color(d, [0, 0, 0]);
    }
    let shown = openrgb::dimmed(color, brightness);
    match want["effect"]
        .as_str()
        .and_then(|e| d.modes.iter().position(|m| m.name == e))
    {
        Some(mode) if !["Direct", "Custom"].contains(&d.modes[mode].name.as_str()) => {
            client.set_mode(d, mode, Some(shown))
        }
        _ => client.set_color(d, shown),
    }
}
/// Send the chosen lighting to every device (or just `only`).
fn apply_lights(chosen: &Value, only: Option<&str>) -> std::io::Result<usize> {
    let mut client = openrgb::Client::connect(openrgb::PORT)?;
    let mut applied = 0;
    for d in client.devices()? {
        if only.is_some_and(|key| key != device_key(&d)) {
            continue;
        }
        if let Some(want) = chosen.get(device_key(&d)) {
            apply_light(&mut client, &d, want)?;
            applied += 1;
        }
    }
    Ok(applied)
}
fn light_from(request: &Value, previous: &Value) -> std::result::Result<Value, Value> {
    let mut want = if previous.is_object() {
        previous.clone()
    } else {
        json!({"brightness": 100, "color": "#ffffff"})
    };
    if let Some(c) = request.get("color") {
        let c = c
            .as_str()
            .filter(|c| openrgb::parse_color(c).is_some())
            .ok_or_else(|| refuse("E42", "Colours look like #ff8800"))?;
        want["color"] = json!(c);
    }
    if let Some(b) = request.get("brightness") {
        want["brightness"] = json!(b
            .as_u64()
            .filter(|b| *b <= 100)
            .ok_or_else(|| refuse("E42", "Brightness goes from 0 to 100"))?);
    }
    if let Some(e) = request.get("effect") {
        want["effect"] = json!(e
            .as_str()
            .filter(|e| e.len() < 80)
            .ok_or_else(|| refuse("E42", "Unknown effect"))?);
    }
    if let Some(off) = request.get("off") {
        want["off"] = json!(off == true);
    }
    Ok(want)
}

/* ---------- the service ---------- */
pub struct State {
    pub controller: Controller,
    pub gate: Gate,
    pub lights: Value,
    lights_pending: bool,
    /// OpenRGB's device list, asked for at most every few seconds.
    devices: Option<(Instant, Vec<openrgb::Device>)>,
    detecting: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
    detect: Value,
    config: PathBuf,
}
impl State {
    fn devices(&mut self) -> Option<&Vec<openrgb::Device>> {
        let fresh = self
            .devices
            .as_ref()
            .is_some_and(|(at, _)| at.elapsed() < Duration::from_secs(5));
        if !fresh {
            self.devices = openrgb::Client::connect(openrgb::PORT)
                .and_then(|mut c| c.devices())
                .ok()
                .map(|d| (Instant::now(), d));
        }
        self.devices.as_ref().map(|(_, d)| d)
    }
    /// A device OpenRGB reports now, or one chosen before (OpenRGB may be starting).
    fn known_light(&mut self, key: &str) -> bool {
        self.lights.get(key).is_some()
            || self
                .devices()
                .is_some_and(|d| d.iter().any(|d| device_key(d) == key))
    }
    fn save_fans(&self) {
        let settings = self
            .controller
            .settings
            .iter()
            .map(|(k, v)| (k.clone(), fans::setting_json(v)))
            .collect::<serde_json::Map<_, _>>();
        let _ = common::atomic_json(
            &self.config.join("fans.json"),
            &json!({"format": "neko-fans-v1", "outputs": settings}),
        );
    }
    fn status(&mut self, request: &Value) -> Value {
        let inv = fans::inventory_json(&self.controller.inventory);
        let settings = self
            .controller
            .settings
            .iter()
            .map(|(k, v)| (k.clone(), fans::setting_json(v)))
            .collect::<serde_json::Map<_, _>>();
        let chosen = self.lights.clone();
        let lights = match self.devices() {
            Some(devices) => {
                json!({"status": if devices.is_empty() { "none_reported" } else { "detected" },
                "devices": devices.iter().map(|d| { let mut v = d.to_json(); v["key"] = json!(device_key(d)); v["chosen"] = chosen.get(device_key(d)).cloned().unwrap_or(Value::Null); v }).collect::<Vec<_>>()})
            }
            None => json!({"status": "not_running", "devices": []}),
        };
        json!({"ok": true, "format": "neko-control-v1", "fans": {"inventory": inv, "settings": settings,
            "state": self.controller.status, "detect": self.detect, "floor": fans::MIN_FLOOR},
            "lights": lights,
            "access": {"local": request["local"] == true, "pin_set": self.gate.pin_set(), "unlocked": self.gate.allowed(request)}})
    }
}

/// One request from the web server (a JSON object; `local` is set by the web server).
pub fn answer(state: &Arc<Mutex<State>>, request: &Value) -> Value {
    let Ok(mut s) = state.lock() else {
        return refuse("E21", "Fan and light control unavailable");
    };
    let op = request["op"].as_str().unwrap_or("");
    match op {
        "status" => return s.status(request),
        "unlock" => return s.gate.unlock(request["pin"].as_str().unwrap_or("")),
        _ => {}
    }
    if !s.gate.allowed(request) {
        return if s.gate.pin_set() {
            refuse("E43", "Enter your PIN to change this")
        } else {
            refuse("E43", "No PIN is set: run setup on the server to change fans and lights from other devices")
        };
    }
    match op {
        "fan" => {
            let id = request["output"].as_str().unwrap_or("");
            if !s.controller.inventory.outputs.iter().any(|o| o.id == id) {
                return refuse("E42", "Unknown fan output");
            }
            let mut setting = match fans::setting_from(&request["setting"]) {
                Ok(v) => v,
                Err(e) => return refuse("E42", e),
            };
            if let fans::Mode::Curve { sensor, .. } = &setting.mode {
                if !s.controller.inventory.temps.iter().any(|t| &t.id == sensor) {
                    return refuse("E42", "Unknown temperature sensor");
                }
            }
            // Detect's findings stay with the output.
            if setting.fans.is_empty() {
                setting.fans = s
                    .controller
                    .settings
                    .get(id)
                    .map(|o| o.fans.clone())
                    .unwrap_or_default();
            }
            s.controller.settings.insert(id.to_string(), setting);
            s.save_fans();
            if !s.detecting.load(Ordering::SeqCst) {
                s.controller.step();
            }
            json!({"ok": true})
        }
        "detect" => start_detect(state.clone(), &mut s),
        "detect_cancel" => {
            s.cancel.store(true, Ordering::SeqCst);
            json!({"ok": true})
        }
        "light" => {
            let key = request["device"].as_str().unwrap_or("").to_string();
            let previous = s.lights.get(&key).cloned().unwrap_or(Value::Null);
            if !s.known_light(&key) {
                return refuse("E42", "Unknown light");
            }
            match light_from(request, &previous) {
                Ok(want) => {
                    s.lights[&key] = want;
                    let _ = common::atomic_json(&s.config.join("lights.json"), &s.lights);
                    // Shown at once; if OpenRGB is not answering, the service retries.
                    if apply_lights(&s.lights, Some(&key)).is_ok() {
                        s.devices = None;
                        json!({"ok": true})
                    } else {
                        s.lights_pending = true;
                        json!({"ok": true, "note": "Saved. It will show once OpenRGB is running."})
                    }
                }
                Err(e) => e,
            }
        }
        _ => refuse("E42", "Request not supported"),
    }
}

fn start_detect(state: Arc<Mutex<State>>, s: &mut State) -> Value {
    if s.detecting.swap(true, Ordering::SeqCst) {
        return refuse("E42", "Fan detection is already running");
    }
    s.cancel.store(false, Ordering::SeqCst);
    s.controller.release_all(); // detection drives every output itself
    let inventory = s.controller.inventory.clone();
    let total = inventory.outputs.len();
    s.detect = json!({"running": true, "done": 0, "total": total, "step": "Starting"});
    let (detecting, cancel) = (s.detecting.clone(), s.cancel.clone());
    thread::spawn(move || {
        let mut probe = Controller::new(inventory, BTreeMap::new());
        let mut done = 0;
        let report = |state: &Arc<Mutex<State>>, step: String, done: usize| {
            if let Ok(mut s) = state.lock() {
                s.detect = json!({"running": true, "done": done, "total": total, "step": step});
            }
        };
        let result = fans::detect(&mut probe, |c, seconds| {
            if seconds == 7 {
                report(
                    &state,
                    format!("Testing output {} of {total}", done + 1),
                    done,
                );
                done += 1;
            }
            for _ in 0..seconds * 10 {
                thread::sleep(Duration::from_millis(100));
                let hot = c
                    .inventory
                    .temps
                    .iter()
                    .any(|t| c.temperature(&t.id).is_some_and(|v| v >= 85.0));
                if cancel.load(Ordering::SeqCst) || STOP.load(Ordering::SeqCst) || hot {
                    return false;
                }
            }
            true
        });
        if let Ok(mut s) = state.lock() {
            match result {
                Ok(found) => {
                    for (output, fans_found) in &found {
                        let entry =
                            s.controller
                                .settings
                                .entry(output.clone())
                                .or_insert(Setting {
                                    mode: fans::Mode::Automatic,
                                    min: fans::MIN_FLOOR,
                                    name: String::new(),
                                    fans: vec![],
                                });
                        entry.fans = fans_found.clone();
                    }
                    s.save_fans();
                    s.detect = json!({"running": false, "result": found});
                }
                Err(e) => {
                    s.detect = json!({"running": false, "error": if e == "Stopped" { "Detection stopped; the motherboard is in charge again".to_string() } else { e }})
                }
            }
        }
        detecting.store(false, Ordering::SeqCst);
    });
    json!({"ok": true})
}

fn load_settings(config: &Path) -> BTreeMap<String, Setting> {
    common::json(&config.join("fans.json"), 1_000_000)["outputs"]
        .as_object()
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| Some((k.clone(), fans::setting_from(v).ok()?)))
                .collect()
        })
        .unwrap_or_default()
}
fn originals_json(c: &Controller) -> Value {
    json!(c
        .originals
        .iter()
        .map(|(k, o)| (k.clone(), json!({"enable": o.enable, "pwm": o.pwm})))
        .collect::<serde_json::Map<_, _>>())
}
/// Give every fan NekoServer took over back to the motherboard (after a crash too).
pub fn restore(hwmon: &Path, saved: &Value) -> usize {
    let inventory = fans::discover(hwmon);
    let mut restored = 0;
    for (id, o) in saved.as_object().into_iter().flatten() {
        if let Some(out) = inventory.outputs.iter().find(|x| &x.id == id) {
            let original = fans::Original {
                enable: o["enable"].as_str().unwrap_or("").to_string(),
                pwm: o["pwm"].as_str().unwrap_or("").to_string(),
            };
            fans::restore_output(out, &original);
            restored += 1;
        }
    }
    restored
}

fn serve_socket(state: Arc<Mutex<State>>) -> Result<()> {
    let path = Path::new(SOCKET);
    let _ = fs::remove_file(path);
    let listener = UnixListener::bind(path)?;
    // SAFETY: getgrnam reads the group database; the name is a NUL-terminated constant.
    let gid = unsafe {
        let g = libc::getgrnam(GROUP.as_ptr() as *const libc::c_char);
        if g.is_null() {
            None
        } else {
            Some((*g).gr_gid)
        }
    };
    if let Some(gid) = gid {
        std::os::unix::fs::chown(path, Some(0), Some(gid))?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o660))?;
    } else {
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
            let mut line = String::new();
            let Ok(reading) = stream.try_clone() else {
                continue;
            };
            if BufReader::new(reading.take(65536))
                .read_line(&mut line)
                .is_err()
            {
                continue;
            }
            let reply = match serde_json::from_str::<Value>(&line) {
                Ok(request) if request.is_object() => answer(&state, &request),
                _ => refuse("E42", "Request not supported"),
            };
            let mut stream = stream;
            let _ = writeln!(stream, "{reply}");
        }
    });
    Ok(())
}

pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let hwmon = PathBuf::from(HWMON);
    let run_dir = Path::new(RUN_DIR);
    if a.flag("restore") {
        let saved = common::json(&run_dir.join("originals.json"), 1_000_000);
        println!(
            "Fans: {} outputs handed back to the motherboard",
            restore(&hwmon, &saved)
        );
        let _ = fs::remove_file(run_dir.join("originals.json"));
        return Ok(());
    }
    let config = PathBuf::from(CONFIG_DIR);
    common::directory(&config, 0o700)?;
    if a.flag("set-pin") {
        let mut pin = String::new();
        std::io::stdin().read_line(&mut pin)?;
        let pin = pin.trim();
        if !valid_pin(pin) {
            return Err(Error::new("A PIN is 4 to 8 digits"));
        }
        common::atomic_json(&config.join("pin.json"), &pin_record(pin, &random_hex(16)?))?;
        fs::set_permissions(config.join("pin.json"), fs::Permissions::from_mode(0o600))?;
        println!("PIN saved");
        return Ok(());
    }
    // SAFETY: installing a handler that only stores to an atomic flag.
    unsafe {
        libc::signal(
            libc::SIGTERM,
            on_stop as extern "C" fn(libc::c_int) as libc::sighandler_t,
        );
        libc::signal(
            libc::SIGINT,
            on_stop as extern "C" fn(libc::c_int) as libc::sighandler_t,
        );
    }
    common::directory(run_dir, 0o755)?;
    let pin = Some(common::json(&config.join("pin.json"), 64000))
        .filter(|v| v["format"] == "neko-pin-v1");
    let lights = Some(common::json(&config.join("lights.json"), 1_000_000))
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}));
    let state = Arc::new(Mutex::new(State {
        controller: Controller::new(fans::discover(&hwmon), load_settings(&config)),
        gate: Gate::new(pin),
        lights,
        lights_pending: true,
        devices: None,
        detecting: Arc::new(AtomicBool::new(false)),
        cancel: Arc::new(AtomicBool::new(false)),
        detect: json!({"running": false}),
        config,
    }));
    serve_socket(state.clone())?;
    let mut rediscovered = Instant::now();
    let mut lights_tried = Instant::now() - Duration::from_secs(60);
    while !STOP.load(Ordering::SeqCst) {
        if let Ok(mut s) = state.lock() {
            if !s.detecting.load(Ordering::SeqCst) {
                if rediscovered.elapsed() > Duration::from_secs(60) {
                    // A driver loaded late (or a card added): pick up its outputs.
                    let found = fans::discover(&hwmon);
                    s.controller.inventory = found;
                    rediscovered = Instant::now();
                }
                s.controller.step();
                let _ = common::atomic_json(
                    &run_dir.join("originals.json"),
                    &originals_json(&s.controller),
                );
            }
            // Lighting is re-applied at start (OpenRGB may start later) and after changes.
            if s.lights_pending && lights_tried.elapsed() > Duration::from_secs(10) {
                lights_tried = Instant::now();
                if apply_lights(&s.lights, None).is_ok() {
                    s.lights_pending = false;
                }
            }
        }
        for _ in 0..20 {
            if STOP.load(Ordering::SeqCst) {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    if let Ok(mut s) = state.lock() {
        s.controller.release_all();
    }
    let _ = fs::remove_file(run_dir.join("originals.json"));
    let _ = fs::remove_file(SOCKET);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state(board: &fans::tests::Board, pin: Option<&str>) -> Arc<Mutex<State>> {
        let config = board.0.join("config");
        fs::create_dir_all(&config).unwrap();
        Arc::new(Mutex::new(State {
            controller: Controller::new(fans::discover(&board.hwmon()), BTreeMap::new()),
            gate: Gate::new(pin.map(|p| pin_record(p, "salt"))),
            lights: json!({}),
            lights_pending: false,
            devices: None,
            detecting: Arc::new(AtomicBool::new(false)),
            cancel: Arc::new(AtomicBool::new(false)),
            detect: json!({"running": false}),
            config,
        }))
    }

    #[test]
    fn pins_are_hashed_checked_and_rate_limited() {
        assert!(valid_pin("1234") && valid_pin("12345678"));
        assert!(!valid_pin("123") && !valid_pin("123456789") && !valid_pin("12a4"));
        let record = pin_record("4321", "salt");
        assert!(pin_matches(&record, "4321") && !pin_matches(&record, "1234"));
        assert!(!record.to_string().contains("4321"));
        let mut gate = Gate::new(Some(record));
        for _ in 0..MAX_TRIES {
            assert_eq!(gate.unlock("0000")["error"], "E44");
        }
        assert_eq!(
            gate.unlock("4321")["error"],
            "E45",
            "locked out even with the right PIN"
        );
        let mut gate = Gate::new(Some(pin_record("4321", "salt")));
        let token = gate.unlock("4321")["token"].as_str().unwrap().to_string();
        assert!(gate.allowed(&json!({"token": token})));
        assert!(!gate.allowed(&json!({"token": "guess"})));
        assert!(gate.allowed(&json!({"local": true})));
    }

    #[test]
    fn changes_need_the_server_itself_or_the_pin() {
        let board = fans::tests::Board::new("gate");
        let s = state(&board, Some("2468"));
        let output = s.lock().unwrap().controller.inventory.outputs[0].id.clone();
        let change = |extra: Value| {
            let mut r =
                json!({"op": "fan", "output": output, "setting": {"mode": "fixed", "fixed": 60}});
            for (k, v) in extra.as_object().unwrap() {
                r[k] = v.clone();
            }
            answer(&s, &r)
        };
        assert_eq!(change(json!({}))["error"], "E43");
        assert_eq!(board.get("pwm1_enable"), "5", "nothing changed");
        assert_eq!(change(json!({"local": true}))["ok"], true);
        assert_eq!(
            (
                board.get("pwm1_enable").as_str(),
                board.get("pwm1").as_str()
            ),
            ("1", "153")
        );
        let token = answer(&s, &json!({"op": "unlock", "pin": "2468"}))["token"]
            .as_str()
            .unwrap()
            .to_string();
        assert_eq!(
            change(json!({"token": token, "setting": {"mode": "auto"}}))["ok"],
            true
        );
        assert_eq!(
            board.get("pwm1_enable"),
            "5",
            "Automatic hands it back to the firmware"
        );
        let saved = common::json(&board.0.join("config/fans.json"), 100000);
        assert_eq!(saved["outputs"][&output]["mode"], "auto");
        // Bad requests are refused with a reason.
        assert_eq!(
            change(json!({"local": true, "setting": {"mode": "fixed", "fixed": 60, "min": 5}}))
                ["error"],
            "E42"
        );
        assert_eq!(
            answer(
                &s,
                &json!({"op": "fan", "local": true, "output": "nope", "setting": {"mode": "auto"}})
            )["error"],
            "E42"
        );
        // Lighting only for devices OpenRGB reports (or chosen before).
        let light = answer(
            &s,
            &json!({"op": "light", "local": true, "device": "made up", "color": "#ffffff"}),
        );
        assert_eq!(light["error"], "E42");
        assert!(s.lock().unwrap().lights.get("made up").is_none());
        let status = answer(&s, &json!({"op": "status"}));
        assert_eq!(status["access"]["pin_set"], true);
        assert_eq!(status["access"]["unlocked"], false);
        assert_eq!(
            status["fans"]["inventory"]["outputs"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }

    #[test]
    fn a_crashed_service_still_hands_fans_back() {
        let board = fans::tests::Board::new("restore");
        let s = state(&board, None);
        let output = s.lock().unwrap().controller.inventory.outputs[1].id.clone();
        answer(
            &s,
            &json!({"op": "fan", "local": true, "output": output, "setting": {"mode": "fixed", "fixed": 80}}),
        );
        assert_eq!(board.get("pwm2_enable"), "1");
        let saved = originals_json(&s.lock().unwrap().controller);
        drop(s); // the service is gone without releasing anything
        assert_eq!(restore(&board.hwmon(), &saved), 1);
        assert_eq!(board.get("pwm2_enable"), "5");
    }

    #[test]
    fn lighting_requests_are_checked_and_remembered() {
        let want = light_from(
            &json!({"color": "#00ff88", "brightness": 40, "effect": "Breathing"}),
            &Value::Null,
        )
        .unwrap();
        assert_eq!(
            (
                want["color"].as_str(),
                want["brightness"].as_u64(),
                want["effect"].as_str()
            ),
            (Some("#00ff88"), Some(40), Some("Breathing"))
        );
        let off = light_from(&json!({"off": true}), &want).unwrap();
        assert_eq!(
            off["color"], "#00ff88",
            "turning off keeps the colour for later"
        );
        assert!(light_from(&json!({"color": "red"}), &Value::Null).is_err());
        assert!(light_from(&json!({"brightness": 150}), &Value::Null).is_err());
    }

    /// A fake OpenRGB server: answers device questions and records what it is sent.
    #[test]
    fn lighting_reaches_openrgb() {
        use std::net::TcpListener;
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let body = crate::openrgb::tests::device_bytes(
            "Strip",
            &[("Direct", 0), ("Breathing", 1)],
            2,
            false,
        );
        let seen = Arc::new(Mutex::new(Vec::<u32>::new()));
        let log = seen.clone();
        thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            loop {
                let mut head = [0u8; 16];
                if s.read_exact(&mut head).is_err() {
                    break;
                }
                let id = u32::from_le_bytes(head[8..12].try_into().unwrap());
                let size = u32::from_le_bytes(head[12..16].try_into().unwrap()) as usize;
                let mut data = vec![0; size];
                s.read_exact(&mut data).unwrap();
                log.lock().unwrap().push(id);
                let reply = match id {
                    0 => Some(1u32.to_le_bytes().to_vec()),
                    1 => Some(body.clone()),
                    _ => None,
                };
                if let Some(r) = reply {
                    let mut out = b"ORGB".to_vec();
                    for n in [0u32, id, r.len() as u32] {
                        out.extend_from_slice(&n.to_le_bytes());
                    }
                    out.extend(r);
                    s.write_all(&out).unwrap();
                }
            }
        });
        let mut client = openrgb::Client::connect(port).unwrap();
        let devices = client.devices().unwrap();
        apply_light(
            &mut client,
            &devices[0],
            &json!({"color": "#ff0000", "brightness": 50}),
        )
        .unwrap();
        apply_light(
            &mut client,
            &devices[0],
            &json!({"color": "#ff0000", "effect": "Breathing"}),
        )
        .unwrap();
        apply_light(&mut client, &devices[0], &json!({"off": true})).unwrap();
        drop(client);
        thread::sleep(Duration::from_millis(200));
        // name, count, data, then: custom mode + LEDs, effect, custom mode + LEDs (off).
        assert_eq!(
            *seen.lock().unwrap(),
            vec![50, 0, 1, 1100, 1050, 1101, 1100, 1050]
        );
    }
}
