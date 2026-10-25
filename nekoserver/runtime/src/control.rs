//! `nekoserver-runtime control`: the one part of NekoServer that changes hardware.
//!
//! Runs as root (writing fan speeds needs it). The dashboard's unprivileged web server
//! passes requests over a private socket (/run/nekoserver-control/control.sock, group
//! nekoserver-control). On the server itself changes need nothing more; from other
//! devices (Tailscale) they need the PIN chosen during setup.
//!
//! `control --restore` gives every fan back to the motherboard (systemd runs it whenever
//! the service ends, crash included); `control --set-pin` reads a new PIN from stdin.
//!
//! Three separate locks: fans, lighting and the PIN gate. Talking to OpenRGB (which can
//! be slow, or hang until its timeout) only ever holds the lighting lock, so it can never
//! delay a fan step or the emergency full-speed response.
use crate::{
    apps,
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
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

pub const SOCKET: &str = "/run/nekoserver-control/control.sock";
const RUN_DIR: &str = "/run/nekoserver-control";
const CONFIG_DIR: &str = "/etc/nekoserver-control";
const HWMON: &str = "/sys/class/hwmon";
/// The firmware settings of outputs taken over, one journal per controller.
const JOURNALS: [&str; 2] = ["originals.json", "originals-detect.json"];
const GROUP: &[u8] = b"nekoserver-control\0";
const PIN_ROUNDS: u32 = 200_000;
const TOKEN_LIFETIME: Duration = Duration::from_secs(12 * 3600);
const MAX_TRIES: usize = 5;
/// The first lockout; each one after it lasts twice as long (up to a day), so guessing a
/// PIN takes years instead of days. A right PIN starts over at 5 minutes.
const LOCKOUT: Duration = Duration::from_secs(300);
const LONGEST_LOCKOUT: Duration = Duration::from_secs(86_400);

static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn on_stop(_: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

/// Short coded answers, shown by the page like the server's other errors.
fn refuse(code: &str, message: &str) -> Value {
    json!({"ok": false, "error": code, "message": message})
}
/// A refusal with the code's own standard sentence.
fn coded(code: &str) -> Value {
    refuse(code, crate::serve::error_message(code))
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
/// A new PIN has at least 6 digits (one chosen before 0.28.7 with 4 or 5 still works).
pub fn valid_new_pin(pin: &str) -> bool {
    valid_pin(pin) && pin.len() >= 6
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
    failures: usize,
    lockouts: u32,
    locked_until: Option<Instant>,
}
impl Gate {
    pub fn new(pin: Option<Value>) -> Self {
        Gate {
            pin,
            ..Default::default()
        }
    }
    fn locked(&self) -> bool {
        self.locked_until
            .is_some_and(|until| Instant::now() < until)
    }
    fn failed(&mut self) {
        self.failures += 1;
        if self.failures >= MAX_TRIES {
            self.failures = 0;
            let length = LOCKOUT
                .saturating_mul(1 << self.lockouts.min(10))
                .min(LONGEST_LOCKOUT);
            self.lockouts = self.lockouts.saturating_add(1);
            self.locked_until = Some(Instant::now() + length);
        }
    }
    pub fn unlock(&mut self, pin: &str) -> Value {
        if self.locked() {
            return coded("E45");
        }
        let Some(record) = &self.pin else {
            return refuse(
                "E43",
                "No PIN is set: run setup on the server to choose one",
            );
        };
        if !valid_pin(pin) || !pin_matches(record, pin) {
            self.failed();
            return coded("E44");
        }
        self.failures = 0;
        self.lockouts = 0;
        let token = random_hex(24).unwrap_or_default();
        if token.is_empty() {
            return coded("E21");
        }
        self.tokens.retain(|_, t| t.elapsed() < TOKEN_LIFETIME);
        self.tokens.insert(token.clone(), Instant::now());
        json!({"ok": true, "token": token})
    }
    pub fn allowed(&mut self, request: &Value) -> bool {
        request["local"] == true || self.signed_in(request)
    }
    /// Unlocked with the PIN on this device. App controls always need this, on the
    /// server itself too: closing or restarting an app can lose someone's work.
    pub fn signed_in(&mut self, request: &Value) -> bool {
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
/// What a device reports matches the choice: its effect, or its LEDs' colour.
fn shows(d: &openrgb::Device, want: &Value) -> bool {
    let color = want["color"]
        .as_str()
        .and_then(openrgb::parse_color)
        .unwrap_or([255, 255, 255]);
    let brightness = want["brightness"].as_u64().unwrap_or(100).min(100) as u8;
    let on = want["off"] != true && brightness > 0;
    let expected = if on {
        openrgb::dimmed(color, brightness)
    } else {
        [0, 0, 0]
    };
    let Some(active) = d.modes.get(d.active_mode.max(0) as usize) else {
        return false;
    };
    let effect = want["effect"]
        .as_str()
        .filter(|e| !e.is_empty() && !["Direct", "Custom"].contains(e));
    match effect.filter(|_| on) {
        // An effect: the right one, running with the chosen colour when it takes one.
        Some(effect) => {
            active.name == effect
                && (!active.takes_color()
                    || (!active.effect_colors().is_empty()
                        && active.effect_colors().iter().all(|c| *c == expected)))
        }
        // A steady colour (or off): a steady mode, every LED showing that colour.
        None => {
            active.is_steady() && !d.colors.is_empty() && d.colors.iter().all(|c| *c == expected)
        }
    }
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

/// Everything about lighting. Only this lock is held while OpenRGB is asked anything.
pub struct Lighting {
    pub chosen: Value,
    pending: bool,
    /// OpenRGB's devices with their lasting names, asked for at most every few seconds.
    devices: Option<(Instant, Vec<(String, openrgb::Device)>)>,
    port: u16,
    config: PathBuf,
}
impl Lighting {
    pub fn new(chosen: Value, port: u16, config: PathBuf) -> Self {
        Lighting {
            chosen,
            pending: true,
            devices: None,
            port,
            config,
        }
    }
    fn save(&self) -> Result<()> {
        common::atomic_json(&self.config.join("lights.json"), &self.chosen)
    }
    fn ask(&self) -> std::io::Result<(openrgb::Client, Vec<(String, openrgb::Device)>)> {
        let mut client = openrgb::Client::connect(self.port)?;
        let devices = client.devices()?;
        let named = openrgb::identities(&devices)
            .into_iter()
            .zip(devices)
            .collect();
        Ok((client, named))
    }
    /// Choices saved by earlier releases under other names (0.27.0: name and
    /// description; 0.27.1: plus the raw location; 0.27.2: serial only when no other
    /// device had it) move to each device's lasting name.
    /// A name several devices shared gives its choice to all of them, as before.
    fn adopt_legacy(&mut self, named: &[(String, openrgb::Device)]) {
        let devices = named.iter().map(|(_, d)| d.clone()).collect::<Vec<_>>();
        let earlier = openrgb::earlier_identities(&devices);
        let current = named.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>();
        for ((key, _), olds) in named.iter().zip(&earlier) {
            if self.chosen.get(key).is_some() {
                continue;
            }
            // The newest earlier name is the most specific one, so it is tried first.
            if let Some(want) = olds
                .iter()
                .rev()
                .find_map(|old| self.chosen.get(old).cloned())
            {
                self.chosen[key] = want;
            }
        }
        if let Some(map) = self.chosen.as_object_mut() {
            map.retain(|k, _| {
                current.contains(&k.as_str()) || !earlier.iter().flatten().any(|o| o == k)
            });
        }
    }
    fn devices(&mut self) -> Option<&Vec<(String, openrgb::Device)>> {
        let fresh = self
            .devices
            .as_ref()
            .is_some_and(|(at, _)| at.elapsed() < Duration::from_secs(5));
        if !fresh {
            self.devices = self.ask().ok().map(|(_, named)| (Instant::now(), named));
            if let Some((_, named)) = self.devices.take() {
                let _ = self.migrate(&named); // kept for the next look when it can't be saved
                                              // A device that was reset or reconnected shows something else again.
                if !self.all_showing(&named) {
                    self.pending = true;
                }
                self.devices = Some((Instant::now(), named));
            }
        }
        self.devices.as_ref().map(|(_, d)| d)
    }
    /// Send the chosen lighting to every device (or just `only`). Succeeds only when
    /// every device it was meant for was found and sent its lighting; a device that is
    /// missing for now (still starting, unplugged) keeps it pending.
    fn apply(&mut self, only: Option<&str>) -> std::io::Result<()> {
        let (mut client, named) = self.ask()?;
        // Names moved from an earlier release are saved before anything counts as done.
        self.migrate(&named)
            .map_err(|_| std::io::Error::other("Lighting names couldn't be saved"))?;
        let wanted = self
            .chosen
            .as_object()
            .map(|m| {
                m.keys()
                    .filter(|k| only.is_none_or(|o| o == k.as_str()))
                    .cloned()
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let mut missing = wanted.len();
        for (key, d) in &named {
            if let (true, Some(want)) = (wanted.contains(key), self.chosen.get(key)) {
                apply_light(&mut client, d, want)?;
                missing -= 1;
            }
        }
        self.devices = None; // read again, so the next look sees what they show now
        if missing > 0 {
            return Err(std::io::Error::other("Not every light was found"));
        }
        Ok(())
    }
    /// Move choices saved under earlier names, and save that at once: otherwise they
    /// could work until a restart renumbers a device, and then be lost.
    fn migrate(&mut self, named: &[(String, openrgb::Device)]) -> Result<()> {
        let before = self.chosen.clone();
        self.adopt_legacy(named);
        if before != self.chosen {
            if let Err(e) = self.save() {
                self.chosen = before;
                return Err(e);
            }
        }
        Ok(())
    }
    /// Every device with a saved choice shows it now.
    fn all_showing(&self, named: &[(String, openrgb::Device)]) -> bool {
        named
            .iter()
            .all(|(key, d)| self.chosen.get(key).is_none_or(|want| shows(d, want)))
    }
    fn status(&mut self) -> Value {
        self.devices(); // may move choices saved under older names
        let chosen = self.chosen.clone();
        match self.devices() {
            Some(named) => json!({
                "status": if named.is_empty() { "none_reported" } else { "detected" },
                "devices": named.iter().map(|(key, d)| {
                    let mut v = d.to_json();
                    v["key"] = json!(key);
                    v["chosen"] = chosen.get(key).cloned().unwrap_or(Value::Null);
                    v
                }).collect::<Vec<_>>()}),
            None => json!({"status": "not_running", "devices": []}),
        }
    }
    fn change(&mut self, request: &Value) -> Value {
        let key = request["device"].as_str().unwrap_or("").to_string();
        let known = self.chosen.get(&key).is_some()
            || self
                .devices()
                .is_some_and(|d| d.iter().any(|(k, _)| k == &key));
        if !known {
            return refuse("E42", "Unknown light");
        }
        let previous = self.chosen.get(&key).cloned().unwrap_or(Value::Null);
        match light_from(request, &previous) {
            Ok(want) => {
                self.chosen[&key] = want;
                if self.save().is_err() {
                    // Not kept after a restart, so not changed now either.
                    self.chosen[&key] = previous;
                    if self.chosen[&key].is_null() {
                        if let Some(map) = self.chosen.as_object_mut() {
                            map.remove(&key);
                        }
                    }
                    return coded("E46");
                }
                // Shown at once; if OpenRGB is not answering, the service retries.
                if self.apply(Some(&key)).is_ok() {
                    json!({"ok": true})
                } else {
                    self.pending = true;
                    json!({"ok": true, "note": "Saved. It will show once OpenRGB is running."})
                }
            }
            Err(e) => e,
        }
    }
}

/* ---------- fans ---------- */
pub struct Fans {
    pub controller: Controller,
    detecting: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
    detect: Value,
    /// The detection worker, waited for before the service exits.
    worker: Option<JoinHandle<()>>,
    /// A recovery record that couldn't be read at start (shown on the page).
    recovery: Option<String>,
    config: PathBuf,
    run_dir: PathBuf,
}
fn settings_json(c: &Controller) -> serde_json::Map<String, Value> {
    c.settings
        .iter()
        .map(|(k, v)| (k.clone(), fans::setting_json(v)))
        .collect()
}
impl Fans {
    pub fn new(controller: Controller, config: PathBuf, run_dir: PathBuf) -> Self {
        Fans {
            controller: controller.with_journal(run_dir.join(JOURNALS[0])),
            detecting: Arc::new(AtomicBool::new(false)),
            cancel: Arc::new(AtomicBool::new(false)),
            detect: json!({"running": false}),
            worker: None,
            recovery: None,
            config,
            run_dir,
        }
    }
    fn save(&self) -> Result<()> {
        common::atomic_json(
            &self.config.join("fans.json"),
            &json!({"format": "neko-fans-v1", "outputs": settings_json(&self.controller)}),
        )
    }
    fn status(&self) -> Value {
        json!({"inventory": fans::inventory_json(&self.controller.inventory),
            "settings": settings_json(&self.controller), "state": self.controller.status,
            "detect": self.detect, "floor": fans::MIN_FLOOR, "recovery": self.recovery})
    }
    fn change(&mut self, request: &Value) -> Value {
        let id = request["output"].as_str().unwrap_or("");
        if !self.controller.inventory.outputs.iter().any(|o| o.id == id) {
            return refuse("E42", "Unknown fan output");
        }
        let mut setting = match fans::setting_from(&request["setting"]) {
            Ok(v) => v,
            Err(e) => return refuse("E42", e),
        };
        if let fans::Mode::Curve { sensor, .. } = &setting.mode {
            if !self
                .controller
                .inventory
                .temps
                .iter()
                .any(|t| &t.id == sensor)
            {
                return refuse("E42", "Unknown temperature sensor");
            }
        }
        // Detect's findings stay with the output.
        if setting.fans.is_empty() {
            setting.fans = self
                .controller
                .settings
                .get(id)
                .map(|o| o.fans.clone())
                .unwrap_or_default();
        }
        let previous = self.controller.settings.insert(id.to_string(), setting);
        if self.save().is_err() {
            // Not kept after a restart, so the fan keeps its current setting.
            match previous {
                Some(p) => self.controller.settings.insert(id.to_string(), p),
                None => self.controller.settings.remove(id),
            };
            return coded("E46");
        }
        if !self.detecting.load(Ordering::SeqCst) {
            self.controller.step();
        }
        json!({"ok": true})
    }
}

/// The running service: three locks, never held together.
pub struct Service {
    pub fans: Mutex<Fans>,
    pub lights: Mutex<Lighting>,
    pub gate: Mutex<Gate>,
    /// The last lighting answer, shown while OpenRGB is busy answering someone else.
    shown_lights: Mutex<Value>,
    /// Lighting switched on in setup. When off, OpenRGB is never asked or changed.
    lighting: bool,
    /// Fan control switched on in setup. When off, no fan is ever taken over (any left
    /// over from before is handed back) and fan changes are refused.
    fans_on: bool,
    /// The dashboard's settings (which apps are installed, and the drives), for the app
    /// controls; None when they are off.
    config: Option<PathBuf>,
    pub actions: Arc<apps::Actions>,
    host: Arc<apps::Host>,
}
/// What setup switched on besides lighting.
pub struct Options {
    pub fans: bool,
    pub config: Option<PathBuf>,
}
impl Default for Options {
    fn default() -> Self {
        Options {
            fans: true,
            config: None,
        }
    }
}
impl Service {
    /// `lighting`: switched on in setup (when off, OpenRGB is never asked or changed).
    #[cfg(test)]
    pub fn new(fans: Fans, lights: Lighting, gate: Gate, lighting: bool) -> Arc<Self> {
        Self::with(fans, lights, gate, lighting, Options::default())
    }
    pub fn with(
        fans: Fans,
        lights: Lighting,
        gate: Gate,
        lighting: bool,
        options: Options,
    ) -> Arc<Self> {
        Arc::new(Service {
            fans: Mutex::new(fans),
            lights: Mutex::new(lights),
            gate: Mutex::new(gate),
            shown_lights: Mutex::new(json!({"status": "not_running", "devices": []})),
            lighting,
            fans_on: options.fans,
            config: options.config,
            actions: Arc::default(),
            host: Arc::default(),
        })
    }
    fn lights_status(&self) -> Value {
        if !self.lighting {
            return json!({"status": "disabled", "devices": []});
        }
        let fresh = self.lights.try_lock().ok().map(|mut l| l.status());
        let mut shown = self.shown_lights.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(v) = fresh {
            *shown = v;
        }
        shown.clone()
    }
    /// Stop a running detection and wait until it has handed every fan back.
    pub fn stop_detection(&self) {
        let worker = self.fans.lock().ok().and_then(|mut f| {
            f.cancel.store(true, Ordering::SeqCst);
            f.worker.take()
        });
        if let Some(w) = worker {
            let _ = w.join();
        }
    }
}

/// One request from the web server (a JSON object; `local` is set by the web server).
pub fn answer(service: &Arc<Service>, request: &Value) -> Value {
    let unavailable = || coded("E21");
    let op = request["op"].as_str().unwrap_or("");
    let Ok(mut gate) = service.gate.lock() else {
        return unavailable();
    };
    match op {
        "unlock" => return gate.unlock(request["pin"].as_str().unwrap_or("")),
        "status" => {
            let access = json!({"local": request["local"] == true, "pin_set": gate.pin_set(),
                "unlocked": gate.allowed(request), "signed_in": gate.signed_in(request)});
            drop(gate);
            let Ok(mut fans) = service.fans.lock().map(|f| f.status()) else {
                return unavailable();
            };
            fans["enabled"] = json!(service.fans_on);
            return json!({"ok": true, "format": "neko-control-v1", "fans": fans,
                "lights": service.lights_status(), "access": access,
                "apps": {"available": service.config.is_some(), "actions": service.actions.list()}});
        }
        _ => {}
    }
    if op.starts_with("app_") || op.starts_with("desktop_") {
        if !gate.pin_set() {
            return refuse(
                "E43",
                "App controls need a PIN. Run setup in a terminal on the server to choose one",
            );
        }
        if !gate.signed_in(request) {
            return coded("E43");
        }
        drop(gate);
        return app_control(service, op, request);
    }
    if !gate.allowed(request) {
        return if gate.pin_set() {
            coded("E43")
        } else {
            refuse("E43", "No PIN is set: run setup on the server to change fans and lights from other devices")
        };
    }
    drop(gate);
    if ["fan", "detect", "detect_cancel"].contains(&op) && !service.fans_on {
        return refuse(
            "E42",
            "Fan control is switched off in setup (\"fan_control\": false)",
        );
    }
    match op {
        "fan" => service
            .fans
            .lock()
            .map_or_else(|_| unavailable(), |mut f| f.change(request)),
        "detect" => start_detect(service),
        "detect_cancel" => {
            if let Ok(f) = service.fans.lock() {
                f.cancel.store(true, Ordering::SeqCst);
            }
            json!({"ok": true})
        }
        "light" if !service.lighting => {
            refuse("E42", "Lighting is switched off in setup (\"rgb\": false)")
        }
        "light" => service
            .lights
            .lock()
            .map_or_else(|_| unavailable(), |mut l| l.change(request)),
        _ => refuse("E42", "Request not supported"),
    }
}

/// Restart a server app, or close a desktop app (the PIN is already checked).
fn app_control(service: &Arc<Service>, op: &str, request: &Value) -> Value {
    let process = || {
        let pid = request["pid"]
            .as_u64()
            .filter(|p| (2..=u64::from(u32::MAX)).contains(p))?;
        Some((pid as u32, request["start"].as_u64()?))
    };
    match op {
        "app_restart" => match (&service.config, request["app"].as_str()) {
            (None, _) => refuse("E42", "App controls are switched off on this server"),
            (Some(config), Some(app)) => apps::Restart {
                config,
                app,
                actions: &service.actions,
            }
            .start(),
            _ => refuse("E42", "Which app?"),
        },
        "desktop_close" | "desktop_force" => match process() {
            Some((pid, start)) => apps::Close {
                host: service.host.clone(),
                pid,
                start,
                force: op == "desktop_force",
                actions: &service.actions,
            }
            .start(),
            None => refuse("E42", "Which program?"),
        },
        _ => refuse("E42", "Request not supported"),
    }
}

/// The temperatures detection watches: every sensor that can be read when it starts.
fn readable_temps(c: &Controller) -> Vec<String> {
    c.inventory
        .temps
        .iter()
        .filter(|t| c.temperature(&t.id).is_some())
        .map(|t| t.id.clone())
        .collect()
}
/// Why detection must stop now, if it must: a temperature of 85 °C, or a watched
/// temperature that can no longer be read (then nobody would know it was overheating).
fn detect_unsafe(c: &Controller, watched: &[String]) -> Option<&'static str> {
    let readings = watched
        .iter()
        .map(|id| c.temperature(id))
        .collect::<Vec<_>>();
    if readings.iter().any(Option::is_none) {
        return Some("Detection stopped: a temperature could no longer be read");
    }
    // The same limits as normal fan control (near a sensor's own critical limit, or
    // 95 °C), with detection's lower 85 °C ceiling on top.
    if c.emergency().is_some() {
        return Some("Detection stopped: a sensor reached its critical temperature");
    }
    readings
        .iter()
        .flatten()
        .any(|v| *v >= 85.0)
        .then_some("Detection stopped: the computer got too warm (85 °C)")
}
/// Wait `seconds`, watching for a stop, a cancel and the temperatures.
fn detect_wait(
    c: &Controller,
    seconds: u64,
    cancel: &AtomicBool,
    watched: &[String],
) -> Option<&'static str> {
    for _ in 0..seconds * 10 {
        thread::sleep(Duration::from_millis(100));
        if let Some(why) = detect_unsafe(c, watched) {
            return Some(why);
        }
        if cancel.load(Ordering::SeqCst) || STOP.load(Ordering::SeqCst) {
            return Some("Detection stopped; the motherboard is in charge again");
        }
    }
    None
}
fn detect_finished(
    f: &mut Fans,
    result: std::result::Result<BTreeMap<String, Vec<String>>, String>,
) {
    match result {
        Ok(found) => {
            for (output, fans_found) in &found {
                let entry = f
                    .controller
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
            f.detect = match f.save() {
                Ok(()) => json!({"running": false, "result": found}),
                Err(_) => {
                    json!({"running": false, "result": found, "error": "Found the fans, but couldn't save it (E46)"})
                }
            };
        }
        Err(e) => f.detect = json!({"running": false, "error": e}),
    }
}
fn start_detect(service: &Arc<Service>) -> Value {
    let Ok(mut f) = service.fans.lock() else {
        return coded("E21");
    };
    if f.detecting.swap(true, Ordering::SeqCst) {
        return refuse("E42", "Fan detection is already running");
    }
    // Never test with fans slowed down unless the temperatures are being watched.
    let watched = readable_temps(&f.controller);
    if watched.is_empty() {
        f.detecting.store(false, Ordering::SeqCst);
        return refuse(
            "E42",
            "No temperature can be read, so fans can't be tested safely",
        );
    }
    f.cancel.store(false, Ordering::SeqCst);
    // Detection drives every output itself, so each must first be back with the
    // motherboard: otherwise it would record NekoServer's own setting as the original.
    if !f.controller.release_all() {
        f.detecting.store(false, Ordering::SeqCst);
        return refuse(
            "E42",
            "A fan didn't go back to the motherboard, so detection can't start",
        );
    }
    let inventory = f.controller.inventory.clone();
    let total = inventory.outputs.len();
    f.detect = json!({"running": true, "done": 0, "total": total, "step": "Starting"});
    let (detecting, cancel) = (f.detecting.clone(), f.cancel.clone());
    // Its own journal: a crash mid-test still hands these fans back.
    let journal = f.run_dir.join(JOURNALS[1]);
    let service = service.clone();
    f.worker = Some(thread::spawn(move || {
        let mut probe = Controller::new(inventory, BTreeMap::new()).with_journal(journal);
        let mut done = 0;
        let mut why = None;
        let result = fans::detect(&mut probe, |c, seconds| {
            if seconds == 7 {
                if let Ok(mut f) = service.fans.lock() {
                    let step = format!("Testing output {} of {total}", done + 1);
                    f.detect = json!({"running": true, "done": done, "total": total, "step": step});
                }
                done += 1;
            }
            why = detect_wait(c, seconds, &cancel, &watched);
            why.is_none()
        });
        let result = result.map_err(|e| match (e.as_str(), why) {
            ("Stopped", Some(reason)) => reason.to_string(),
            _ => e,
        });
        if let Ok(mut f) = service.fans.lock() {
            detect_finished(&mut f, result);
            // Outputs that refused to go back: the fan loop keeps trying them.
            // Detection's journal is removed only once the main one holds them.
            let left = std::mem::take(&mut probe.originals);
            if !left.is_empty() && f.controller.adopt_originals(left).is_ok() {
                let _ = fs::remove_file(f.run_dir.join(JOURNALS[1]));
            }
        }
        detecting.store(false, Ordering::SeqCst);
    }));
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
/// Give every fan in a journal back to the motherboard (after a crash too). Returns how
/// many went back, and the ones that didn't (still to do).
pub fn restore(hwmon: &Path, saved: &Value) -> (usize, BTreeMap<String, fans::Original>) {
    let inventory = fans::discover(hwmon);
    let mut restored = 0;
    let mut left = BTreeMap::new();
    for (id, original) in fans::originals_from(saved) {
        match inventory.outputs.iter().find(|x| x.id == id) {
            Some(out) if fans::restore_output(out, &original) => restored += 1,
            _ => {
                left.insert(id, original);
            }
        }
    }
    (restored, left)
}
/// Every journal (fan control and detection). What could not be put back stays in the
/// main journal, so the next attempt (or the service, when it starts) tries again.
///
/// Detection's journal goes first, the main one last: for an output in both, the main
/// journal holds the setting from before NekoServer touched it, so that one wins. A
/// journal is only removed once nothing in it is left to do, or once what is left has
/// been safely written to the main journal.
///
/// Both journals are merged first (the main one wins for an output in both) and each
/// output is restored once. A journal that can't be read is never deleted or written
/// over: it is kept for diagnosis, and `unreadable_journals` reports it.
fn restore_journals(hwmon: &Path, run_dir: &Path) -> (usize, BTreeMap<String, fans::Original>) {
    let mut merged = BTreeMap::new();
    for name in JOURNALS.iter().rev() {
        if let Journal::Records(records) = read_journal(&run_dir.join(name)) {
            merged.extend(fans::originals_from(&records)); // main, read last, wins
        }
    }
    let (restored, left) = restore(hwmon, &fans::originals_json(&merged));
    let bad = unreadable_journals(run_dir);
    let main = run_dir.join(JOURNALS[0]);
    let keep_main = left.is_empty()
        || (!bad.contains(&main)
            && common::atomic_json(&main, &fans::originals_json(&left)).is_ok());
    if keep_main {
        for name in JOURNALS {
            let path = run_dir.join(name);
            let needed = path == main && !left.is_empty();
            if !needed && !bad.contains(&path) {
                let _ = fs::remove_file(path);
            }
        }
    } // else: every journal stays exactly as it was
    (restored, left)
}
enum Journal {
    Absent,
    Records(Value),
    Unreadable,
}
fn read_journal(path: &Path) -> Journal {
    match fs::read_to_string(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Journal::Absent,
        Err(_) => Journal::Unreadable,
        Ok(text) => match serde_json::from_str::<Value>(&text) {
            Ok(v) if fans::originals_valid(&v) => Journal::Records(v),
            _ => Journal::Unreadable,
        },
    }
}
/// Journals that exist but can't be read (damaged): kept, and reported.
fn unreadable_journals(run_dir: &Path) -> Vec<PathBuf> {
    JOURNALS
        .iter()
        .map(|n| run_dir.join(n))
        .filter(|p| matches!(read_journal(p), Journal::Unreadable))
        .collect()
}
fn report_unreadable(run_dir: &Path) -> Option<String> {
    let bad = unreadable_journals(run_dir);
    (!bad.is_empty()).then(|| {
        format!(
            "A fan recovery record can't be read and was kept for checking: {}. Check that every fan is back with the motherboard (Help, Fans don't change)",
            bad.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(", ")
        )
    })
}

fn serve_socket(service: Arc<Service>) -> Result<()> {
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
            // One thread per request: a slow lighting answer never holds up a fan change.
            let service = service.clone();
            thread::spawn(move || {
                let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
                let mut line = String::new();
                let Ok(reading) = stream.try_clone() else {
                    return;
                };
                if BufReader::new(reading.take(65536))
                    .read_line(&mut line)
                    .is_err()
                {
                    return;
                }
                let reply = match serde_json::from_str::<Value>(&line) {
                    Ok(request) if request.is_object() => answer(&service, &request),
                    _ => refuse("E42", "Request not supported"),
                };
                let mut stream = stream;
                let _ = writeln!(stream, "{reply}");
            });
        }
    });
    Ok(())
}

/// Lighting is re-applied at start (OpenRGB may start later) and after a failed change,
/// on its own thread so OpenRGB never holds up the fans.
fn retry_lights(service: Arc<Service>) {
    thread::spawn(move || {
        while !STOP.load(Ordering::SeqCst) {
            if let Ok(mut l) = service.lights.lock() {
                l.devices(); // notices a reset or reconnected device (sets pending)
                if l.pending && l.apply(None).is_ok() {
                    l.pending = false;
                }
            }
            for _ in 0..100 {
                if STOP.load(Ordering::SeqCst) {
                    return;
                }
                thread::sleep(Duration::from_millis(100));
            }
        }
    });
}

fn set_pin(config: &Path) -> Result<()> {
    let mut pin = String::new();
    std::io::stdin().read_line(&mut pin)?;
    let pin = pin.trim();
    if !valid_new_pin(pin) {
        return Err(Error::new("A PIN is 6 to 8 digits"));
    }
    common::atomic_json(&config.join("pin.json"), &pin_record(pin, &random_hex(16)?))?;
    fs::set_permissions(config.join("pin.json"), fs::Permissions::from_mode(0o600))?;
    println!("PIN saved");
    Ok(())
}

pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let hwmon = PathBuf::from(HWMON);
    let run_dir = PathBuf::from(RUN_DIR);
    if a.flag("restore") {
        let (n, left) = restore_journals(&hwmon, &run_dir);
        println!("Fans: {n} outputs handed back to the motherboard");
        if let Some(problem) = report_unreadable(&run_dir) {
            return Err(Error::new(&problem));
        }
        if !left.is_empty() {
            return Err(Error::new(
                "Some fan outputs did not go back to the motherboard; they are retried when the service starts",
            ));
        }
        return Ok(());
    }
    let config = PathBuf::from(CONFIG_DIR);
    common::directory(&config, 0o700)?;
    if a.flag("set-pin") {
        return set_pin(&config);
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
    common::directory(&run_dir, 0o755)?;
    // Anything left from a run that ended without its cleanup goes back first; what
    // refuses stays with the controller, which keeps trying (at full speed meanwhile).
    let (_, left) = restore_journals(&hwmon, &run_dir);
    // A damaged record is moved aside (never written over) and reported on the page.
    let recovery = report_unreadable(&run_dir);
    if let Some(problem) = &recovery {
        eprintln!("{problem}");
        for bad in unreadable_journals(&run_dir) {
            let _ = fs::rename(&bad, bad.with_extension("json.unreadable"));
        }
    }
    let pin = Some(common::json(&config.join("pin.json"), 64000))
        .filter(|v| v["format"] == "neko-pin-v1");
    // With lighting switched off in setup, saved lighting is not even read.
    let lighting = !a.flag("no-lights");
    let chosen = if lighting {
        Some(common::json(&config.join("lights.json"), 1_000_000))
            .filter(Value::is_object)
            .unwrap_or_else(|| json!({}))
    } else {
        json!({})
    };
    // With fan control switched off, no saved fan setting is used: every output stays
    // with (or goes back to) the motherboard.
    let fans_on = !a.flag("no-fans");
    let settings = if fans_on {
        load_settings(&config)
    } else {
        BTreeMap::new()
    };
    let controller = Controller::new(fans::discover(&hwmon), settings);
    let mut fans_state = Fans::new(controller, config.clone(), run_dir.clone());
    let _ = fans_state.controller.adopt_originals(left); // the journal already holds them
    fans_state.recovery = recovery;
    let options = Options {
        fans: fans_on,
        config: a
            .0
            .contains_key("config")
            .then(|| a.path("config"))
            .transpose()?,
    };
    // An app restart cut off part-way (the service stopped by force, or the computer
    // restarted): its parts run again once no backup or update needs the apps stopped.
    if let Some(settings) = &options.config {
        apps::finish_restarts_when_free(settings.clone(), PathBuf::from(apps::RESTART_RECORDS));
    }
    let service = Service::with(
        fans_state,
        Lighting::new(chosen, openrgb::PORT, config),
        Gate::new(pin),
        lighting,
        options,
    );
    serve_socket(service.clone())?;
    if lighting {
        retry_lights(service.clone());
    }
    let mut rediscovered = Instant::now();
    while !STOP.load(Ordering::SeqCst) {
        if let Ok(mut f) = service.fans.lock() {
            if !f.detecting.load(Ordering::SeqCst) {
                if rediscovered.elapsed() > Duration::from_secs(60) || f.controller.rediscover {
                    f.controller.rediscover = false;
                    // A driver loaded late (or a card added): pick up its outputs.
                    f.controller.inventory = fans::discover(&hwmon);
                    rediscovered = Instant::now();
                }
                f.controller.step();
            }
        }
        for _ in 0..20 {
            if STOP.load(Ordering::SeqCst) {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    // An app being restarted is never left half stopped: its parts are stopped and
    // started again before this service ends (setup restarts it; the unit allows longer).
    let waiting = Instant::now();
    while service.actions.changing_apps()
        && waiting.elapsed() < Duration::from_secs(apps::LONGEST_CHANGE_SECS)
    {
        thread::sleep(Duration::from_millis(250));
    }
    // Detection first (it hands its fans back itself), then the curves.
    service.stop_detection();
    if let Ok(mut f) = service.fans.lock() {
        f.controller.release_all();
    }
    let (_, left) = restore_journals(&hwmon, &run_dir);
    let _ = fs::remove_file(SOCKET);
    if !left.is_empty() {
        return Err(Error::new(
            "Some fan outputs did not go back to the motherboard; they are retried when the service starts",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn service(board: &fans::tests::Board, pin: Option<&str>, port: u16) -> Arc<Service> {
        let config = board.0.join("config");
        let run_dir = board.0.join("run");
        fs::create_dir_all(&config).unwrap();
        fs::create_dir_all(&run_dir).unwrap();
        let controller = Controller::new(fans::discover(&board.hwmon()), BTreeMap::new());
        let mut lights = Lighting::new(json!({}), port, config.clone());
        lights.pending = false;
        Service::new(
            Fans::new(controller, config, run_dir),
            lights,
            Gate::new(pin.map(|p| pin_record(p, "salt"))),
            true,
        )
    }
    fn first_output(s: &Arc<Service>, n: usize) -> String {
        s.fans.lock().unwrap().controller.inventory.outputs[n]
            .id
            .clone()
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
        // Each lockout is twice as long as the one before.
        let mut gate = Gate::new(Some(pin_record("4321", "salt")));
        for round in 0..3u32 {
            gate.locked_until = None;
            for _ in 0..MAX_TRIES {
                gate.unlock("0000");
            }
            let left = gate.locked_until.unwrap() - Instant::now();
            assert!(left > LOCKOUT * (1 << round) - Duration::from_secs(5));
            assert!(left <= LOCKOUT * (1 << round));
        }
        gate.locked_until = None;
        assert!(gate.unlock("4321")["ok"] == true);
        assert_eq!((gate.lockouts, gate.failures), (0, 0));
        assert!(valid_new_pin("123456") && !valid_new_pin("1234"));
        let mut gate = Gate::new(Some(pin_record("4321", "salt")));
        let token = gate.unlock("4321")["token"].as_str().unwrap().to_string();
        assert!(gate.allowed(&json!({"token": token})));
        assert!(!gate.allowed(&json!({"token": "guess"})));
        assert!(gate.allowed(&json!({"local": true})));
    }

    #[test]
    fn app_controls_always_need_the_pin_even_on_the_server() {
        let board = fans::tests::Board::new("app-pin");
        let restart = json!({"op": "app_restart", "app": "jellyfin", "local": true});
        let close = json!({"op": "desktop_force", "pid": 4242, "start": 1, "local": true});
        // No PIN chosen: app controls are off everywhere, and the page says why.
        let none = service(&board, None, 1);
        for request in [&restart, &close] {
            let refused = answer(&none, request);
            assert_eq!(refused["error"], "E43");
            assert!(refused["message"].as_str().unwrap().contains("need a PIN"));
        }
        // A PIN chosen: being on the server itself is not enough (fans and lights are).
        let s = service(&board, Some("2468"), 1);
        for request in [&restart, &close] {
            assert_eq!(answer(&s, request)["error"], "E43", "{request}");
        }
        let token = answer(&s, &json!({"op": "unlock", "pin": "2468"}))["token"].clone();
        let status = answer(&s, &json!({"op": "status", "local": true, "token": token}));
        assert_eq!(status["access"]["signed_in"], true);
        assert_eq!(status["apps"], json!({"available": false, "actions": []}));
        // Unlocked: the request reaches the app checks (no settings here, so they're off).
        let mut unlocked = restart.clone();
        unlocked["token"] = token.clone();
        assert!(answer(&s, &unlocked)["message"]
            .as_str()
            .unwrap()
            .contains("switched off"));
        // A program number that can't be one is refused before anything looks at it.
        for pid in [json!(0), json!(1), json!(-5), json!("12"), json!(u64::MAX)] {
            let r = answer(
                &s,
                &json!({"op": "desktop_close", "pid": pid, "start": 1, "token": token}),
            );
            assert_eq!(r["message"], "Which program?", "{pid}");
        }
        assert_eq!(
            answer(&s, &json!({"op": "app_format", "token": token}))["error"],
            "E42"
        );
    }

    #[test]
    fn with_fan_control_off_no_fan_is_ever_taken_over() {
        let board = fans::tests::Board::new("no-fans");
        let config = board.0.join("config");
        let run_dir = board.0.join("run");
        fs::create_dir_all(&config).unwrap();
        fs::create_dir_all(&run_dir).unwrap();
        let controller = Controller::new(fans::discover(&board.hwmon()), BTreeMap::new());
        let output = controller.inventory.outputs[0].id.clone();
        let s = Service::with(
            Fans::new(controller, config.clone(), run_dir),
            Lighting::new(json!({}), 1, config),
            Gate::new(None),
            false,
            Options {
                fans: false,
                config: None,
            },
        );
        for op in ["fan", "detect", "detect_cancel"] {
            let r = answer(
                &s,
                &json!({"op": op, "local": true, "output": output, "setting": {"mode": "fixed", "fixed": 100}}),
            );
            assert!(
                r["message"].as_str().unwrap().contains("switched off"),
                "{op}"
            );
        }
        assert_eq!(board.get("pwm1_enable"), "5", "left with the motherboard");
        assert_eq!(
            answer(&s, &json!({"op": "status", "local": true}))["fans"]["enabled"],
            false
        );
    }

    #[test]
    fn changes_need_the_server_itself_or_the_pin() {
        let board = fans::tests::Board::new("gate");
        let s = service(&board, Some("2468"), 1);
        let output = first_output(&s, 0);
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
        assert!(s.lights.lock().unwrap().chosen.get("made up").is_none());
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

    /// The firmware setting is on disk before an output is changed, so a crash at any
    /// moment (SIGKILL, power to the service lost) still lets `--restore` hand it back.
    #[test]
    fn a_crashed_service_still_hands_fans_back() {
        let board = fans::tests::Board::new("restore");
        let s = service(&board, None, 1);
        let output = first_output(&s, 1);
        answer(
            &s,
            &json!({"op": "fan", "local": true, "output": output, "setting": {"mode": "fixed", "fixed": 80}}),
        );
        assert_eq!(board.get("pwm2_enable"), "1");
        let journal = common::json(&board.0.join("run/originals.json"), 100000);
        assert_eq!(journal[&output]["enable"], "5");
        drop(s); // the service is gone without releasing anything
        assert_eq!(restore_journals(&board.hwmon(), &board.0.join("run")).0, 1);
        assert_eq!(board.get("pwm2_enable"), "5");
        assert!(!board.0.join("run/originals.json").exists());
    }

    /// Detection's own journal holds every output it drives while it tests them.
    #[test]
    fn a_crash_during_detection_still_hands_fans_back() {
        let board = fans::tests::Board::new("detect-crash");
        let hwmon = board.hwmon();
        let journal = board.0.join("originals-detect.json");
        let mut probe =
            Controller::new(fans::discover(&hwmon), BTreeMap::new()).with_journal(journal.clone());
        let mut checked = false;
        let _ = fans::detect(&mut probe, |_, seconds| {
            if seconds == 7 {
                // The 35 % test: both outputs in manual mode, both in the journal.
                assert_eq!(
                    (board.get("pwm1_enable"), board.get("pwm2_enable")),
                    ("1".into(), "1".into())
                );
                let saved = common::json(&journal, 100000);
                assert_eq!(fans::originals_from(&saved).len(), 2);
                // What systemd's ExecStopPost would do after a crash right now:
                assert_eq!(restore(&hwmon, &saved).0, 2);
                assert_eq!(
                    (board.get("pwm1_enable"), board.get("pwm2_enable")),
                    ("5".into(), "5".into())
                );
                checked = true;
                return false;
            }
            true
        });
        assert!(checked);
        assert!(fans::originals_from(&common::json(&journal, 100000)).is_empty());
    }

    /// Stopping the service waits for detection, which hands the fans back.
    #[test]
    fn stopping_waits_for_detection_to_hand_fans_back() {
        let board = fans::tests::Board::new("detect-stop");
        let s = service(&board, None, 1);
        assert_eq!(
            answer(&s, &json!({"op": "detect", "local": true}))["ok"],
            true
        );
        wait_until_driving(&board);
        let started = Instant::now();
        s.stop_detection();
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(
            (board.get("pwm1_enable"), board.get("pwm2_enable")),
            ("5".into(), "5".into())
        );
        assert!(fans::originals_from(&common::json(
            &board.0.join("run/originals-detect.json"),
            100000
        ))
        .is_empty());
        assert!(
            answer(&s, &json!({"op": "status"}))["fans"]["detect"]["error"]
                .as_str()
                .unwrap()
                .contains("motherboard")
        );
    }

    /// A slow (or hung) OpenRGB only holds up lighting: fan changes and the fan step
    /// carry on, and the page still gets an answer.
    #[test]
    fn slow_lighting_never_holds_up_the_fans() {
        let body = openrgb::tests::device_bytes("Strip", &[("Direct", 0)], 2, false);
        // OpenRGB takes 2 s for each answer (the light change needs two); the fans must
        // not wait for even one. Each answer still comes within the client's 3 s read
        // limit, so the light change itself succeeds. Before 0.28.5 the fake answered
        // after 4 s, past that limit, so this test failed whenever the operating system
        // didn't happen to deliver it early (it failed in CI, but rarely on a desktop).
        let (port, _) = openrgb::tests::fake_server(vec![body], Duration::from_millis(2000));
        let board = fans::tests::Board::new("slow-rgb");
        let s = service(&board, None, port);
        let output = first_output(&s, 0);
        let busy = s.clone();
        let slow = thread::spawn(move || {
            answer(
                &busy,
                &json!({"op": "light", "local": true, "device": "Strip · Desk strip · HID", "color": "#ff0000"}),
            )
        });
        thread::sleep(Duration::from_millis(200)); // the light change now waits on OpenRGB
        let started = Instant::now();
        let fan = answer(
            &s,
            &json!({"op": "fan", "local": true, "output": output, "setting": {"mode": "fixed", "fixed": 100}}),
        );
        assert_eq!(fan["ok"], true);
        s.fans.lock().unwrap().controller.step();
        let status = answer(&s, &json!({"op": "status"}));
        assert!(
            started.elapsed() < Duration::from_millis(1800),
            "{:?}",
            started.elapsed()
        );
        assert!(
            !slow.is_finished(),
            "the light change was still waiting on OpenRGB"
        );
        assert_eq!(board.get("pwm1"), "255");
        assert!(status["lights"].is_object());
        let light = slow.join().unwrap();
        assert_eq!(light["ok"], true, "{light}");
    }

    #[test]
    fn identical_lights_keep_their_own_settings() {
        let modes = [("Direct", 0)];
        let devices = vec![
            openrgb::tests::device_at("Vengeance", "I2C: address 0x58", &modes, 2, false),
            openrgb::tests::device_at("Vengeance", "I2C: address 0x59", &modes, 2, false),
        ];
        let (port, seen) = openrgb::tests::fake_server(devices, Duration::ZERO);
        let board = fans::tests::Board::new("twins");
        let s = service(&board, None, port);
        // A choice saved by 0.27.0 for both sticks at once goes to each of them.
        s.lights.lock().unwrap().chosen = json!({"Vengeance · Desk strip": {"color": "#00ff00"}});
        let status = answer(&s, &json!({"op": "status"}));
        let keys = status["lights"]["devices"]
            .as_array()
            .unwrap()
            .iter()
            .map(|d| d["key"].as_str().unwrap().to_string())
            .collect::<Vec<_>>();
        assert_eq!(
            keys,
            [
                "Vengeance · Desk strip · I2C: address 0x58",
                "Vengeance · Desk strip · I2C: address 0x59"
            ]
        );
        assert_eq!(status["lights"]["devices"][1]["chosen"]["color"], "#00ff00");
        // Now each stick has its own colour, and only that stick is sent it.
        seen.lock().unwrap().clear();
        let r = answer(
            &s,
            &json!({"op": "light", "local": true, "device": keys[1], "color": "#0000ff"}),
        );
        assert_eq!(r["ok"], true);
        let chosen = s.lights.lock().unwrap().chosen.clone();
        assert_eq!(chosen[&keys[0]]["color"], "#00ff00");
        assert_eq!(chosen[&keys[1]]["color"], "#0000ff");
        assert!(chosen.get("Vengeance · Desk strip").is_none());
        thread::sleep(Duration::from_millis(300)); // the last packets have no answer
        let sent = seen
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, id)| *id == 1050)
            .map(|(dev, _)| *dev)
            .collect::<Vec<_>>();
        assert_eq!(sent, [1], "only the second stick changes");
    }

    fn wait_for_detection(s: &Arc<Service>) -> Value {
        for _ in 0..50 {
            let d = answer(s, &json!({"op": "status"}))["fans"]["detect"].clone();
            if d["running"] == false {
                return d;
            }
            thread::sleep(Duration::from_millis(100));
        }
        panic!("detection did not stop");
    }

    /// Detection has taken the first fan (its worker thread is under way). A slow
    /// machine takes longer than a fixed pause would allow.
    fn wait_until_driving(board: &fans::tests::Board) {
        for _ in 0..100 {
            if board.get("pwm1_enable") == "1" {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        panic!("detection did not take the first fan");
    }

    #[test]
    fn detection_needs_a_temperature_it_can_read() {
        let board = fans::tests::Board::new("detect-blind");
        fs::remove_file(board.hwmon().join("hwmon1/temp1_input")).unwrap();
        let s = service(&board, None, 1);
        let r = answer(&s, &json!({"op": "detect", "local": true}));
        assert_eq!(r["error"], "E42");
        assert!(r["message"].as_str().unwrap().contains("temperature"));
        assert_eq!(board.get("pwm1_enable"), "5", "no fan was touched");
        assert!(!s.fans.lock().unwrap().detecting.load(Ordering::SeqCst));
    }

    #[test]
    fn detection_stops_when_a_temperature_stops_reading() {
        let board = fans::tests::Board::new("detect-lost");
        let s = service(&board, None, 1);
        assert_eq!(
            answer(&s, &json!({"op": "detect", "local": true}))["ok"],
            true
        );
        wait_until_driving(&board);
        board.set("temp1_input", "not a number"); // the sensor driver failed
        let d = wait_for_detection(&s);
        assert!(
            d["error"]
                .as_str()
                .unwrap()
                .contains("could no longer be read"),
            "{d}"
        );
        assert_eq!(
            (board.get("pwm1_enable"), board.get("pwm2_enable")),
            ("5".into(), "5".into())
        );
        // Too warm stops it too.
        board.set("temp1_input", "45000");
        assert_eq!(
            answer(&s, &json!({"op": "detect", "local": true}))["ok"],
            true
        );
        wait_until_driving(&board);
        board.set("temp1_input", "86000");
        assert!(wait_for_detection(&s)["error"]
            .as_str()
            .unwrap()
            .contains("too warm"));
        assert_eq!(board.get("pwm1_enable"), "5");
    }

    /// A change that can't be written to disk is refused, not shown as saved.
    #[test]
    fn a_change_that_cant_be_saved_is_refused() {
        let board = fans::tests::Board::new("readonly");
        let s = service(&board, None, 1);
        let output = first_output(&s, 0);
        // The settings folder is gone (a full or broken disk behaves the same).
        fs::remove_dir_all(board.0.join("config")).unwrap();
        fs::write(board.0.join("config"), "not a folder").unwrap();
        let r = answer(
            &s,
            &json!({"op": "fan", "local": true, "output": output, "setting": {"mode": "fixed", "fixed": 70}}),
        );
        assert_eq!(r["error"], "E46");
        assert_eq!(board.get("pwm1_enable"), "5", "the fan keeps its setting");
        assert!(!s
            .fans
            .lock()
            .unwrap()
            .controller
            .settings
            .contains_key(&output));
        s.lights.lock().unwrap().chosen = json!({"Strip": {"color": "#ff0000"}});
        let r = answer(
            &s,
            &json!({"op": "light", "local": true, "device": "Strip", "color": "#00ff00"}),
        );
        assert_eq!(r["error"], "E46");
        assert_eq!(s.lights.lock().unwrap().chosen["Strip"]["color"], "#ff0000");
    }

    /// An output that refuses to go back is never reported as handed back: it runs at
    /// full speed and stays recorded until it does go back.
    #[test]
    fn a_fan_that_wont_go_back_is_kept_and_retried() {
        let board = fans::tests::Board::new("stuck");
        let s = service(&board, None, 1);
        let output = first_output(&s, 0);
        let set = |mode: Value| {
            answer(
                &s,
                &json!({"op": "fan", "local": true, "output": output, "setting": mode}),
            )
        };
        set(json!({"mode": "fixed", "fixed": 40}));
        assert_eq!(board.get("pwm1_enable"), "1");
        // The driver stops accepting the mode (here: the file can't be written).
        let enable = board.hwmon().join("hwmon1/pwm1_enable");
        fs::remove_file(&enable).unwrap();
        fs::create_dir(&enable).unwrap();
        set(json!({"mode": "auto"}));
        let status = answer(&s, &json!({"op": "status"}));
        assert_eq!(status["fans"]["state"][&output]["mode"], "stuck");
        assert_eq!(board.get("pwm1"), "255", "full speed meanwhile");
        let journal = common::json(&board.0.join("run/originals.json"), 100000);
        assert_eq!(journal[&output]["enable"], "5", "still recorded");
        // A crash now: --restore can't do it either, and keeps the record.
        let (_, left) = restore_journals(&board.hwmon(), &board.0.join("run"));
        assert!(left.contains_key(&output));
        assert!(board.0.join("run/originals.json").exists());
        // The driver works again: the next step hands it back for real.
        fs::remove_dir(&enable).unwrap();
        fs::write(&enable, "1").unwrap();
        s.fans.lock().unwrap().controller.step();
        assert_eq!(board.get("pwm1_enable"), "5");
        let status = answer(&s, &json!({"op": "status"}));
        assert_eq!(status["fans"]["state"][&output]["mode"], "auto");
        assert!(s.fans.lock().unwrap().controller.originals.is_empty());
    }

    fn block(path: &Path) {
        // A path that can't be replaced: a folder with something in it.
        let _ = fs::remove_file(path);
        fs::create_dir_all(path).unwrap();
        fs::write(path.join("x"), "").unwrap();
    }

    /// Records of fans not yet handed back are never deleted before their replacement
    /// is safely on disk.
    #[test]
    fn recovery_records_survive_a_failed_save() {
        let board = fans::tests::Board::new("journal-save");
        let run = board.0.join("run");
        fs::create_dir_all(&run).unwrap();
        let output = fans::discover(&board.hwmon()).outputs[0].id.clone();
        let record = json!({&output: {"enable": "5", "pwm": "128"}});
        common::atomic_json(&run.join(JOURNALS[1]), &record).unwrap();
        block(&board.hwmon().join("hwmon1/pwm1_enable")); // the output refuses
        block(&run.join(JOURNALS[0])); // and the main journal can't be written
        let (_, left) = restore_journals(&board.hwmon(), &run);
        assert!(left.contains_key(&output));
        assert_eq!(
            common::json(&run.join(JOURNALS[1]), 100000),
            record,
            "kept as it was"
        );
    }

    #[test]
    fn detection_wont_start_while_a_fan_isnt_back() {
        let board = fans::tests::Board::new("detect-stuck");
        let s = service(&board, None, 1);
        let output = first_output(&s, 0);
        answer(
            &s,
            &json!({"op": "fan", "local": true, "output": output, "setting": {"mode": "fixed", "fixed": 40}}),
        );
        block(&board.hwmon().join("hwmon1/pwm1_enable"));
        let r = answer(&s, &json!({"op": "detect", "local": true}));
        assert_eq!(r["error"], "E42");
        assert!(r["message"].as_str().unwrap().contains("didn't go back"));
        assert!(!s.fans.lock().unwrap().detecting.load(Ordering::SeqCst));
        // NekoServer's own setting was never recorded as the original.
        let journal = common::json(&board.0.join("run").join(JOURNALS[0]), 100000);
        assert_eq!(journal[&output]["enable"], "5");
        assert!(!board.0.join("run").join(JOURNALS[1]).exists());
    }

    /// When detection can't hand a fan back and its record can't be moved to the main
    /// journal, detection's own journal stays.
    #[test]
    fn detection_keeps_its_journal_until_the_records_are_moved() {
        let board = fans::tests::Board::new("detect-move");
        let s = service(&board, None, 1);
        let run = board.0.join("run");
        assert_eq!(
            answer(&s, &json!({"op": "detect", "local": true}))["ok"],
            true
        );
        wait_until_driving(&board);
        block(&board.hwmon().join("hwmon1/pwm1_enable"));
        block(&run.join(JOURNALS[0]));
        s.stop_detection();
        let kept = fans::originals_from(&common::json(&run.join(JOURNALS[1]), 100000));
        assert_eq!(kept.len(), 1, "{kept:?}");
        assert_eq!(kept.values().next().unwrap().enable, "5");
    }

    /// Lighting stays pending until every saved device has been found and sent its
    /// lighting, so a device that shows up late still gets it.
    #[test]
    fn lights_stay_pending_until_every_device_got_its_setting() {
        let body = openrgb::tests::device_bytes("Strip", &[("Direct", 0)], 2, false);
        let (port, seen) = openrgb::tests::fake_server(vec![body], Duration::ZERO);
        let board = fans::tests::Board::new("late-light");
        let s = service(&board, None, port);
        let mut l = s.lights.lock().unwrap();
        l.chosen = json!({"Strip · Desk strip · HID": {"color": "#ff0000"}, "Fan hub · Desk strip · HID": {"color": "#00ff00"}});
        assert!(l.apply(None).is_err(), "the fan hub isn't there yet");
        assert!(l.apply(Some("Strip · Desk strip · HID")).is_ok());
        assert!(l.apply(Some("Fan hub · Desk strip · HID")).is_err());
        drop(l);
        thread::sleep(Duration::from_millis(200));
        assert!(
            seen.lock().unwrap().iter().any(|(_, id)| *id == 1050),
            "the strip was still set"
        );
    }

    /// A choice saved by 0.27.1 under a /dev/hidraw path moves to the device's
    /// lasting name (its serial number), and keeps working when the path changes.
    #[test]
    fn lighting_saved_under_a_changing_path_moves_to_the_serial() {
        let modes = [("Direct", 0)];
        let keyboard =
            |path| openrgb::tests::device_full("Keyboard", "KB7731A", path, &modes, 2, false);
        let (port, _) =
            openrgb::tests::fake_server(vec![keyboard("HID: /dev/hidraw5")], Duration::ZERO);
        let board = fans::tests::Board::new("hidraw");
        let s = service(&board, None, port);
        s.lights.lock().unwrap().chosen =
            json!({"Keyboard · Desk strip · HID: /dev/hidraw5": {"color": "#123456"}});
        let status = answer(&s, &json!({"op": "status"}));
        let device = &status["lights"]["devices"][0];
        assert_eq!(device["key"], "Keyboard · Desk strip · serial KB7731A");
        assert_eq!(device["chosen"]["color"], "#123456");
        let chosen = s.lights.lock().unwrap().chosen.clone();
        assert_eq!(chosen.as_object().unwrap().len(), 1, "{chosen}");
        // After a restart the same keyboard is /dev/hidraw2: same name, same colour.
        let (port, _) =
            openrgb::tests::fake_server(vec![keyboard("HID: /dev/hidraw2")], Duration::ZERO);
        s.lights.lock().unwrap().port = port;
        s.lights.lock().unwrap().devices = None;
        let status = answer(&s, &json!({"op": "status"}));
        assert_eq!(status["lights"]["devices"][0]["chosen"]["color"], "#123456");
    }

    /// A record with missing or wrong fields is never "restored" (that switched the fan
    /// to mode 2 and deleted the record): it is kept and reported.
    #[test]
    fn malformed_recovery_records_are_kept_not_applied() {
        let board = fans::tests::Board::new("journal-fields");
        let run = board.0.join("run");
        fs::create_dir_all(&run).unwrap();
        let output = fans::discover(&board.hwmon()).outputs[0].id.clone();
        board.set("pwm1_enable", "1"); // taken over by NekoServer
        for bad in [
            json!({&output: {"enable": "", "pwm": "128"}}),
            json!({&output: {"enable": 5, "pwm": "128"}}),
            json!({&output: {"enable": "5", "pwm": "300"}}),
            json!({&output: {"enable": "5"}}),
            json!({&output: "5"}),
        ] {
            common::atomic_json(&run.join(JOURNALS[0]), &bad).unwrap();
            let (restored, _) = restore_journals(&board.hwmon(), &run);
            assert_eq!(restored, 0, "{bad}");
            assert_eq!(board.get("pwm1_enable"), "1", "nothing changed for {bad}");
            assert_eq!(
                common::json(&run.join(JOURNALS[0]), 100000),
                bad,
                "kept as it was"
            );
            assert_eq!(unreadable_journals(&run), vec![run.join(JOURNALS[0])]);
            assert!(report_unreadable(&run).is_some());
        }
        // A record with valid fields still works.
        common::atomic_json(
            &run.join(JOURNALS[0]),
            &json!({&output: {"enable": "5", "pwm": "128"}}),
        )
        .unwrap();
        assert_eq!(restore_journals(&board.hwmon(), &run).0, 1);
        assert_eq!(board.get("pwm1_enable"), "5");
    }

    /// A reset device is noticed whatever it shows instead: another colour, another
    /// brightness, an effect with the wrong colour, or an animation.
    #[test]
    fn a_device_shows_its_choice_only_when_everything_matches() {
        let body = openrgb::tests::device_bytes(
            "Strip",
            &[("Direct", 0), ("Breathing", 1), ("Rainbow", 0)],
            2,
            false,
        );
        let device = || openrgb::parse_device(0, &body).unwrap(); // LEDs at #ff0080, Direct
        let steady = json!({"color": "#ff0080", "brightness": 100});
        assert!(shows(&device(), &steady));
        assert!(!shows(&device(), &json!({"color": "#00ff00"})));
        assert!(
            !shows(&device(), &json!({"color": "#ff0080", "brightness": 50})),
            "brightness counts"
        );
        // An animation that happens to leave the LED colours alone is not a steady colour.
        let mut rainbow = device();
        openrgb::tests::in_mode(&mut rainbow, 2, 0, vec![]);
        assert!(!shows(&rainbow, &steady));
        assert!(
            shows(&rainbow, &json!({"effect": "Rainbow"})),
            "an effect with its own colours"
        );
        // An effect that takes a colour must run with the chosen one, dimmed.
        let mut breathing = device();
        openrgb::tests::in_mode(&mut breathing, 1, 2, vec![openrgb::dimmed([0, 255, 0], 40)]);
        assert!(shows(
            &breathing,
            &json!({"effect": "Breathing", "color": "#00ff00", "brightness": 40})
        ));
        assert!(!shows(
            &breathing,
            &json!({"effect": "Breathing", "color": "#00ff00", "brightness": 100})
        ));
        assert!(!shows(
            &breathing,
            &json!({"effect": "Breathing", "color": "#ff0000", "brightness": 40})
        ));
        assert!(!shows(&breathing, &json!({"effect": "Rainbow"})));
        // Off: a steady mode with every LED dark.
        let mut dark = device();
        dark.colors = vec![[0, 0, 0]; 2];
        assert!(shows(&dark, &json!({"off": true, "color": "#ff0080"})));
        assert!(!shows(&device(), &json!({"off": true})));
    }

    /// Two different devices with the same serial string: 0.27.2 named them by their
    /// i2c place, 0.27.3 by serial. Choices saved by 0.27.2 must follow.
    #[test]
    fn lighting_saved_by_0_27_2_moves_to_the_serial_names() {
        let modes = [("Direct", 0)];
        let stick =
            |name, place| openrgb::tests::device_full(name, "SAME5678", place, &modes, 2, false);
        let devices = vec![
            stick("Vengeance", "I2C: /dev/i2c-1, address 0x58"),
            stick("Dominator", "I2C: /dev/i2c-1, address 0x59"),
        ];
        let (port, _) = openrgb::tests::fake_server(devices, Duration::ZERO);
        let board = fans::tests::Board::new("v272");
        let s = service(&board, None, port);
        s.lights.lock().unwrap().chosen = json!({
            "Vengeance · Desk strip · I2C: i2c, address 0x58": {"color": "#111111"},
            "Dominator · Desk strip · I2C: i2c, address 0x59": {"color": "#222222"},
        });
        let status = answer(&s, &json!({"op": "status"}));
        let shown = status["lights"]["devices"].as_array().unwrap();
        assert_eq!(shown[0]["key"], "Vengeance · Desk strip · serial SAME5678");
        assert_eq!(shown[0]["chosen"]["color"], "#111111");
        assert_eq!(shown[1]["chosen"]["color"], "#222222");
        let mut l = s.lights.lock().unwrap();
        assert_eq!(l.chosen.as_object().unwrap().len(), 2, "{}", l.chosen);
        assert!(l.apply(None).is_ok(), "every saved light found");
        let saved = common::json(&board.0.join("config/lights.json"), 100000);
        assert_eq!(
            saved["Dominator · Desk strip · serial SAME5678"]["color"],
            "#222222"
        );
    }

    /// RGB switched off in setup: OpenRGB is never asked or changed, even when one is
    /// running (its own app with the SDK server on), and lighting changes are refused.
    #[test]
    fn lighting_switched_off_never_touches_openrgb() {
        let body = openrgb::tests::device_bytes("Strip", &[("Direct", 0)], 2, false);
        let (port, seen) = openrgb::tests::fake_server(vec![body], Duration::ZERO);
        let board = fans::tests::Board::new("no-lights");
        let config = board.0.join("config");
        let run_dir = board.0.join("run");
        fs::create_dir_all(&config).unwrap();
        fs::create_dir_all(&run_dir).unwrap();
        let controller = Controller::new(fans::discover(&board.hwmon()), BTreeMap::new());
        let s = Service::new(
            Fans::new(controller, config.clone(), run_dir),
            Lighting::new(json!({}), port, config),
            Gate::new(None),
            false,
        );
        let status = answer(&s, &json!({"op": "status"}));
        assert_eq!(status["lights"]["status"], "disabled");
        let r = answer(
            &s,
            &json!({"op": "light", "local": true, "device": "Strip · Desk strip · HID", "color": "#ff0000"}),
        );
        assert_eq!(r["error"], "E42");
        assert!(r["message"].as_str().unwrap().contains("switched off"));
        thread::sleep(Duration::from_millis(200));
        assert!(
            seen.lock().unwrap().is_empty(),
            "OpenRGB was never contacted"
        );
        // Fans still work.
        let output = first_output(&s, 0);
        assert_eq!(
            answer(
                &s,
                &json!({"op": "fan", "local": true, "output": output, "setting": {"mode": "fixed", "fixed": 50}})
            )["ok"],
            true
        );
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

    /// Off, a steady colour and an effect each send the right OpenRGB requests.
    #[test]
    fn lighting_reaches_openrgb() {
        let body =
            openrgb::tests::device_bytes("Strip", &[("Direct", 0), ("Breathing", 1)], 2, false);
        let (port, seen) = openrgb::tests::fake_server(vec![body], Duration::ZERO);
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
        let ids = seen
            .lock()
            .unwrap()
            .iter()
            .map(|(_, id)| *id)
            .collect::<Vec<_>>();
        // name, count, data, then: custom mode + LEDs, effect, custom mode + LEDs (off).
        assert_eq!(ids, vec![50, 0, 1, 1100, 1050, 1101, 1100, 1050]);
    }
    #[test]
    fn a_second_sign_in_keeps_the_first_device_signed_in() {
        let mut gate = Gate::new(Some(pin_record("4321", "salt")));
        let phone = gate.unlock("4321")["token"].as_str().unwrap().to_string();
        let laptop = gate.unlock("4321")["token"].as_str().unwrap().to_string();
        assert_ne!(phone, laptop);
        assert!(gate.signed_in(&json!({"token": phone})));
        assert!(gate.signed_in(&json!({"token": laptop})));
        assert!(!gate.signed_in(&json!({})));
    }
    #[test]
    fn a_curve_needs_a_temperature_the_board_has() {
        let board = fans::tests::Board::new("curve-sensor");
        let s = service(&board, None, 1);
        let output = first_output(&s, 0);
        let sensor = s.fans.lock().unwrap().controller.inventory.temps[0]
            .id
            .clone();
        let curve = |sensor: &str| {
            answer(
                &s,
                &json!({"op": "fan", "local": true, "output": output, "setting": {
                    "mode": "curve", "sensor": sensor, "points": [[30, 20], [60, 60], [80, 100]]}}),
            )
        };
        let refused = curve("hwmon9/temp7_input");
        assert_eq!(refused["error"], "E42");
        assert_eq!(refused["message"], "Unknown temperature sensor");
        assert_eq!(board.get("pwm1_enable"), "5", "nothing changed");
        assert_eq!(curve(&sensor)["ok"], true);
    }
    #[test]
    fn an_unreadable_journal_is_reported_not_taken_for_absent() {
        let root = std::env::temp_dir().join(format!("neko-journal-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        assert!(matches!(
            read_journal(&root.join(JOURNALS[0])),
            Journal::Absent
        ));
        assert!(report_unreadable(&root).is_none());
        // A journal that cannot be read for another reason than being absent (here it is
        // a folder) is kept and named, never taken for "nothing to hand back".
        fs::create_dir_all(root.join(JOURNALS[1])).unwrap();
        assert!(matches!(
            read_journal(&root.join(JOURNALS[1])),
            Journal::Unreadable
        ));
        let report = report_unreadable(&root).unwrap();
        assert!(
            report.contains(JOURNALS[1]) && report.contains("can't be read"),
            "{report}"
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn saved_fan_settings_are_loaded_at_start() {
        let root = std::env::temp_dir().join(format!("neko-fan-settings-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        assert!(load_settings(&root).is_empty());
        fs::write(
            root.join("fans.json"),
            r#"{"outputs": {"hwmon1/pwm1": {"mode": "fixed", "fixed": 60}, "hwmon1/pwm2": {"mode": "nonsense"}}}"#,
        )
        .unwrap();
        let settings = load_settings(&root);
        assert_eq!(settings.keys().collect::<Vec<_>>(), ["hwmon1/pwm1"]);
        assert!(
            matches!(settings["hwmon1/pwm1"].mode, fans::Mode::Fixed(f) if (f - 60.0).abs() < 1e-9)
        );
        let _ = fs::remove_dir_all(root);
    }
}

#[cfg(test)]
mod review_extra_tests {
    use super::*;
    #[test]
    fn review_detection_respects_a_lower_sensor_critical_limit() {
        let b = fans::tests::Board::new("review-detect-critical");
        b.set("temp1_crit", "70000");
        b.set("temp1_input", "75000");
        let c = Controller::new(fans::discover(&b.hwmon()), BTreeMap::new());
        assert!(
            c.emergency().is_some(),
            "normal fan control recognizes the critical temperature"
        );
        let watched = readable_temps(&c);
        assert!(
            detect_unsafe(&c, &watched).is_some(),
            "detection must also recognize the critical temperature"
        );
    }
    #[test]
    fn review_main_journal_success_clears_a_failed_duplicate_detection_record() {
        let b = fans::tests::Board::new("review-journal-priority");
        let run = b.0.join("run");
        fs::create_dir_all(&run).unwrap();
        let output = fans::discover(&b.hwmon()).outputs[0].id.clone();
        common::atomic_json(
            &run.join(JOURNALS[0]),
            &json!({&output:{"enable":"5","pwm":"128"}}),
        )
        .unwrap();
        common::atomic_json(
            &run.join(JOURNALS[1]),
            &json!({&output:{"enable":"1","pwm":"255"}}),
        )
        .unwrap();
        let pwm = b.hwmon().join("hwmon1/pwm1");
        fs::remove_file(&pwm).unwrap();
        fs::create_dir(&pwm).unwrap();
        let (restored, left) = restore_journals(&b.hwmon(), &run);
        assert_eq!(restored, 1);
        assert_eq!(b.get("pwm1_enable"), "5");
        assert!(
            left.is_empty(),
            "firmware was restored, but stale manual record survives: {left:?}"
        );
    }
    #[test]
    fn review_corrupt_journal_is_reported_and_retained_for_diagnosis() {
        let b = fans::tests::Board::new("review-invalid-journal");
        let run = b.0.join("run");
        fs::create_dir_all(&run).unwrap();
        let path = run.join(JOURNALS[1]);
        fs::write(&path, b"{truncated").unwrap();
        let (_, left) = restore_journals(&b.hwmon(), &run);
        assert!(
            path.exists(),
            "unreadable recovery data was silently treated as empty and deleted; left={left:?}"
        );
    }
}

#[cfg(test)]
mod review_rgb_extra_tests {
    use super::*;
    #[test]
    fn review_startup_apply_persists_migrated_lighting_names() {
        let b = fans::tests::Board::new("review-rgb-startup");
        let config = b.0.join("config");
        fs::create_dir_all(&config).unwrap();
        let body = openrgb::tests::device_full(
            "Keyboard",
            "KB7731A",
            "HID: /dev/hidraw5",
            &[("Direct", 0)],
            2,
            false,
        );
        let (port, _) = openrgb::tests::fake_server(vec![body], Duration::ZERO);
        let old = json!({"Keyboard · Desk strip · HID: /dev/hidraw5":{"color":"#123456"}});
        common::atomic_json(&config.join("lights.json"), &old).unwrap();
        let mut lighting = Lighting::new(old, port, config.clone());
        lighting.apply(None).unwrap();
        let saved = common::json(&config.join("lights.json"), 100000);
        assert!(
            saved
                .get("Keyboard · Desk strip · serial KB7731A")
                .is_some(),
            "migration only in memory; saved={saved}"
        );
    }
    #[test]
    fn review_restarted_openrgb_is_reconfigured_even_after_an_earlier_success() {
        let b = fans::tests::Board::new("review-rgb-restart");
        let config = b.0.join("config");
        fs::create_dir_all(&config).unwrap();
        let body = openrgb::tests::device_full(
            "Keyboard",
            "KB7731A",
            "HID: /dev/hidraw5",
            &[("Direct", 0)],
            2,
            false,
        );
        let (port, _) = openrgb::tests::fake_server(vec![body.clone()], Duration::ZERO);
        let mut lighting = Lighting::new(
            json!({"Keyboard · Desk strip · serial KB7731A":{"color":"#00ff00"}}),
            port,
            config,
        );
        lighting.apply(None).unwrap();
        lighting.pending = false;
        let (port2, seen) = openrgb::tests::fake_server(vec![body], Duration::ZERO);
        lighting.port = port2;
        lighting.devices = None;
        let status = lighting.status();
        assert_eq!(status["devices"][0]["chosen"]["color"], "#00ff00");
        assert!(
            lighting.pending,
            "fresh server's actual colour is #ff0080, but no retry is scheduled; packets={:?}",
            seen.lock().unwrap()
        );
    }
}
