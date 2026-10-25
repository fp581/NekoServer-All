//! Fan control: find every fan output the motherboard (or graphics card) lets Linux
//! drive, follow temperature curves, and find out which fan each output drives.
//!
//! Safety first, always:
//! - an output is only taken over when its curve or fixed speed is saved; "Automatic"
//!   hands it back to the firmware;
//! - never below MIN_FLOOR %, whatever the curve says;
//! - full speed when the curve's sensor can't be read, or any sensor nears its
//!   critical temperature (or 95 °C);
//! - every output goes back to the firmware when control stops (and, after a crash,
//!   through `control --restore`, which systemd runs whenever the service ends). For
//!   that, the firmware's own setting is written to a journal file *before* an output
//!   is taken over, and removed only after it has been put back.
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

pub const MIN_FLOOR: f64 = 20.0;
/// Above this for HOT_FOR, or at EMERGENCY_NOW_C at once, everything goes to full speed.
/// Current processors run at 95 °C (AMD) or 100 °C (Intel) under full load by design and
/// many report no critical limit, so one reading at 95 °C is not an emergency.
const EMERGENCY_C: f64 = 95.0;
const EMERGENCY_NOW_C: f64 = 105.0;
const HOT_FOR: std::time::Duration = std::time::Duration::from_secs(300);
/// What a probe input with nothing connected reads on many boards (nct67xx AUXTIN): no
/// working part of a PC reaches it, so it is never taken for a temperature.
const UNCONNECTED_C: f64 = 127.0;
/// How fast a fan may slow down per control step (speeding up is immediate).
const STEP_DOWN: f64 = 5.0;
/// SATA drive temperatures are never read here (it can keep a hard drive awake).
const SKIPPED_CHIPS: [&str; 1] = ["drivetemp"];

fn read(path: &Path) -> String {
    read_ok(path).unwrap_or_default()
}
/// A reading that worked (None when the file couldn't be read at all).
fn read_ok(path: &Path) -> Option<String> {
    fs::read_to_string(path).ok().map(|s| s.trim().to_string())
}
fn number(path: &Path) -> Option<f64> {
    read(path).parse::<f64>().ok().filter(|v| v.is_finite())
}
fn write(path: &Path, value: &str) -> std::io::Result<()> {
    fs::write(path, value)
}

/// A fan output (pwmN with a pwmN_enable switch), a fan speed sensor or a temperature.
#[derive(Clone, Debug, PartialEq)]
pub struct Channel {
    /// Stable across boots: "<chip>@<device path>/<file>", e.g. "nct6798@/sys/devices/platform/nct6775.656/pwm2".
    pub id: String,
    /// The chip's own folder (resolved, never the reusable /sys/class/hwmon/hwmonN).
    pub dir: PathBuf,
    /// The device the chip belongs to, checked before every change.
    pub device: PathBuf,
    pub file: String,
    pub label: String,
    pub chip: String,
}
impl Channel {
    fn path(&self, suffix: &str) -> PathBuf {
        self.dir.join(format!("{}{suffix}", self.file))
    }
    /// Still the same chip on the same device. Linux reuses hwmon numbers when chips
    /// come and go, so a folder found earlier may now belong to another chip.
    pub fn intact(&self) -> bool {
        read_ok(&self.dir.join("name")).as_deref() == Some(self.chip.as_str())
            && device_of(&self.dir) == self.device
    }
}
fn device_of(dir: &Path) -> PathBuf {
    fs::canonicalize(dir.join("device")).unwrap_or_else(|_| dir.to_path_buf())
}
#[derive(Clone, Debug, Default)]
pub struct Inventory {
    pub outputs: Vec<Channel>,
    pub fans: Vec<Channel>,
    pub temps: Vec<Channel>,
}

/// Every controllable output, fan sensor and temperature under `hwmon` (/sys/class/hwmon).
pub fn discover(hwmon: &Path) -> Inventory {
    let mut inv = Inventory::default();
    let mut chips = fs::read_dir(hwmon)
        .map(|r| r.flatten().map(|e| e.path()).collect::<Vec<_>>())
        .unwrap_or_default();
    chips.sort();
    for dir in chips {
        let chip = read(&dir.join("name"));
        if chip.is_empty() || SKIPPED_CHIPS.contains(&chip.as_str()) {
            continue;
        }
        let dir = fs::canonicalize(&dir).unwrap_or(dir);
        let device = device_of(&dir);
        let mut files = fs::read_dir(&dir)
            .map(|r| {
                r.flatten()
                    .filter_map(|e| e.file_name().into_string().ok())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        files.sort_by_key(|f| (f.len(), f.clone()));
        for file in files {
            let digits = |prefix: &str, suffix: &str| {
                file.strip_prefix(prefix)?
                    .strip_suffix(suffix)
                    .filter(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
                    .map(|n| format!("{prefix}{n}"))
            };
            let (list, base, default_label) = if let Some(base) = digits("pwm", "") {
                if !dir.join(format!("{base}_enable")).exists() {
                    continue; // no automatic/manual switch: not safely controllable
                }
                let n = base.trim_start_matches("pwm").to_string();
                (&mut inv.outputs, base, format!("Fan output {n}"))
            } else if let Some(base) = digits("fan", "_input") {
                let n = base.trim_start_matches("fan").to_string();
                (&mut inv.fans, base, format!("Fan {n}"))
            } else if let Some(base) = digits("temp", "_input") {
                let n = base.trim_start_matches("temp").to_string();
                (&mut inv.temps, base, format!("Sensor {n}"))
            } else {
                continue;
            };
            let label = read(&dir.join(format!("{base}_label")));
            list.push(Channel {
                id: format!("{chip}@{}/{base}", device.display()),
                dir: dir.clone(),
                device: device.clone(),
                file: base.clone(),
                label: if label.is_empty() {
                    default_label
                } else {
                    label.chars().take(60).collect()
                },
                chip: chip.clone(),
            });
        }
    }
    inv
}

/// A speed in percent as the 0–255 value fan outputs take.
fn pwm_value(percent: f64) -> String {
    ((percent.clamp(0.0, 100.0) * 255.0 / 100.0).round() as u32).to_string()
}
/// Speed (%) for a temperature from curve points (temperature °C, speed %): straight
/// lines between points, flat before the first and after the last.
pub fn curve_duty(points: &[(f64, f64)], temp: f64) -> f64 {
    let Some(first) = points.first() else {
        return 100.0;
    };
    if temp <= first.0 {
        return first.1;
    }
    for pair in points.windows(2) {
        let ((t0, d0), (t1, d1)) = (pair[0], pair[1]);
        if temp <= t1 {
            return if t1 > t0 {
                d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
            } else {
                d1
            };
        }
    }
    points.last().unwrap().1
}

/// What the user chose for one output.
#[derive(Clone, Debug, PartialEq)]
pub enum Mode {
    Automatic,
    Fixed(f64),
    Curve {
        sensor: String,
        points: Vec<(f64, f64)>,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub struct Setting {
    pub mode: Mode,
    pub min: f64,
    pub name: String,
    pub fans: Vec<String>,
}

/// A saved or requested setting, checked: 2–8 points rising in temperature, 0–110 °C,
/// 0–100 %, a minimum of at least MIN_FLOOR %.
pub fn setting_from(value: &Value) -> Result<Setting, &'static str> {
    let min = value["min"].as_f64().unwrap_or(MIN_FLOOR);
    if !(MIN_FLOOR..=100.0).contains(&min) {
        return Err("The lowest speed must be between 20% and 100%");
    }
    let mode = match value["mode"].as_str().unwrap_or("auto") {
        "auto" => Mode::Automatic,
        "fixed" => {
            let pct = value["fixed"].as_f64().ok_or("A fixed speed is needed")?;
            if !(0.0..=100.0).contains(&pct) {
                return Err("Speeds go from 0% to 100%");
            }
            Mode::Fixed(pct)
        }
        "curve" => {
            let sensor = value["sensor"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() < 400)
                .ok_or("Choose the temperature the curve follows")?;
            let points = value["points"]
                .as_array()
                .filter(|p| (2..=8).contains(&p.len()))
                .ok_or("A curve needs 2 to 8 points")?
                .iter()
                .map(|p| Some((p[0].as_f64()?, p[1].as_f64()?)))
                .collect::<Option<Vec<_>>>()
                .ok_or("Curve points are (temperature, speed)")?;
            if points
                .iter()
                .any(|(t, d)| !(0.0..=110.0).contains(t) || !(0.0..=100.0).contains(d))
                || points.windows(2).any(|w| w[1].0 <= w[0].0)
            {
                return Err(
                    "Curve points must rise in temperature (0–110 °C) with speeds of 0–100%",
                );
            }
            Mode::Curve {
                sensor: sensor.to_string(),
                points,
            }
        }
        _ => return Err("Unknown fan mode"),
    };
    let name = value["name"]
        .as_str()
        .unwrap_or("")
        .chars()
        .filter(|c| !c.is_control())
        .take(40)
        .collect();
    let fans = value["fans"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|f| f.as_str().map(str::to_string))
                .take(8)
                .collect()
        })
        .unwrap_or_default();
    Ok(Setting {
        mode,
        min,
        name,
        fans,
    })
}
pub fn setting_json(s: &Setting) -> Value {
    let mut v = json!({"min": s.min, "name": s.name, "fans": s.fans});
    match &s.mode {
        Mode::Automatic => v["mode"] = json!("auto"),
        Mode::Fixed(pct) => {
            v["mode"] = json!("fixed");
            v["fixed"] = json!(pct);
        }
        Mode::Curve { sensor, points } => {
            v["mode"] = json!("curve");
            v["sensor"] = json!(sensor);
            v["points"] = json!(points.iter().map(|(t, d)| [t, d]).collect::<Vec<_>>());
        }
    }
    v
}

/// The firmware's own setting of an output, put back when NekoServer lets go.
#[derive(Clone, Debug, PartialEq)]
pub struct Original {
    pub enable: String,
    pub pwm: String,
}
impl Original {
    /// A mode number and a speed of 0–255, as Linux reports them.
    pub fn valid(&self) -> bool {
        (1..=2).contains(&self.enable.len())
            && self.enable.bytes().all(|b| b.is_ascii_digit())
            && self.pwm.parse::<u32>().is_ok_and(|n| n <= 255)
    }
}
/// Every entry of a journal is a valid record (a damaged one is kept, not used).
pub fn originals_valid(saved: &Value) -> bool {
    saved.as_object().is_some_and(|m| {
        m.values().all(|o| {
            o.as_object().is_some_and(|f| {
                f.get("enable").is_some_and(Value::is_string)
                    && f.get("pwm").is_some_and(Value::is_string)
            })
        })
    }) && originals_from(saved).values().all(Original::valid)
}

/// Follows the saved settings, one step every couple of seconds.
pub struct Controller {
    pub inventory: Inventory,
    pub settings: BTreeMap<String, Setting>,
    /// Outputs NekoServer has taken over, with the firmware setting to restore.
    pub originals: BTreeMap<String, Original>,
    last: BTreeMap<String, f64>,
    pub status: BTreeMap<String, Value>,
    /// Where `originals` is kept for `control --restore` (None only in tests).
    journal: Option<PathBuf>,
    /// An output moved or vanished: look for the outputs again soon.
    pub rediscover: bool,
    /// Outputs whose linked fans stopped while being driven: (steps seen stopped, the
    /// setting full speed was latched for). See `watch_stall`.
    stalls: BTreeMap<String, (u32, Option<Setting>)>,
    /// When each sensor went above EMERGENCY_C (kept by every step), for HOT_FOR.
    pub hot_since: BTreeMap<String, std::time::Instant>,
}
/// The journal's form: {output id: {"enable": …, "pwm": …}}.
pub fn originals_json(originals: &BTreeMap<String, Original>) -> Value {
    json!(originals
        .iter()
        .map(|(k, o)| (k.clone(), json!({"enable": o.enable, "pwm": o.pwm})))
        .collect::<serde_json::Map<_, _>>())
}
pub fn originals_from(saved: &Value) -> BTreeMap<String, Original> {
    saved
        .as_object()
        .into_iter()
        .flatten()
        .map(|(id, o)| {
            let field = |k: &str| o[k].as_str().unwrap_or("").to_string();
            (
                id.clone(),
                Original {
                    enable: field("enable"),
                    pwm: field("pwm"),
                },
            )
        })
        .collect()
}
impl Controller {
    pub fn new(inventory: Inventory, settings: BTreeMap<String, Setting>) -> Self {
        Controller {
            inventory,
            settings,
            originals: BTreeMap::new(),
            last: BTreeMap::new(),
            status: BTreeMap::new(),
            journal: None,
            rediscover: false,
            stalls: BTreeMap::new(),
            hot_since: BTreeMap::new(),
        }
    }
    pub fn with_journal(mut self, path: PathBuf) -> Self {
        self.journal = Some(path);
        self
    }
    fn save_journal(&self) -> std::io::Result<()> {
        match &self.journal {
            Some(path) => crate::common::atomic_json(path, &originals_json(&self.originals))
                .map_err(|_| std::io::Error::other("journal")),
            None => Ok(()),
        }
    }
    /// A sensor's reading in °C, or None when it can't be read or is not a reading at all:
    /// a probe input with nothing connected reads 127 °C (or -128 °C) on many boards, or
    /// says so in its `_fault` file. No working part of a PC gets that hot.
    pub fn temperature(&self, id: &str) -> Option<f64> {
        let t = self.inventory.temps.iter().find(|t| t.id == id)?;
        if read(&t.path("_fault")) == "1" {
            return None;
        }
        number(&t.path("_input"))
            .map(|v| v / 1000.0)
            .filter(|v| (-40.0..UNCONNECTED_C).contains(v))
    }
    /// Any sensor near its own critical limit (less 3 °C) or at 105 °C, or above 95 °C for
    /// five minutes: everything to full speed.
    pub fn emergency(&self) -> Option<String> {
        // Without a single temperature, overheating can't be noticed: full speed.
        if !self
            .inventory
            .temps
            .iter()
            .any(|t| self.temperature(&t.id).is_some())
        {
            return Some("no temperature can be read".to_string());
        }
        self.inventory.temps.iter().find_map(|t| {
            let v = self.temperature(&t.id)?;
            let crit = number(&t.path("_crit"))
                .map(|c| c / 1000.0)
                .filter(|c| *c > 40.0);
            let held = v > EMERGENCY_C
                && self
                    .hot_since
                    .get(&t.id)
                    .is_some_and(|since| since.elapsed() >= HOT_FOR);
            let hot = crit.is_some_and(|c| v >= c - 3.0) || v >= EMERGENCY_NOW_C || held;
            hot.then(|| format!("{} {} at {v:.0} °C", t.chip, t.label))
        })
    }
    /// Note which sensors are above EMERGENCY_C now, and since when.
    fn track_heat(&mut self) {
        let hot: Vec<String> = self
            .inventory
            .temps
            .iter()
            .filter(|t| self.temperature(&t.id).is_some_and(|v| v > EMERGENCY_C))
            .map(|t| t.id.clone())
            .collect();
        self.hot_since.retain(|id, _| hot.contains(id));
        for id in hot {
            self.hot_since
                .entry(id)
                .or_insert_with(std::time::Instant::now);
        }
    }
    fn take_over(&mut self, out: &Channel) -> std::io::Result<()> {
        if !out.intact() {
            self.rediscover = true;
            return Err(std::io::Error::other("The output moved"));
        }
        if !self.originals.contains_key(&out.id) {
            // Only taken over when its own setting is known, so it can be put back.
            let enable = read_ok(&out.path("_enable"))
                .filter(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))
                .ok_or_else(|| std::io::Error::other("Its setting can't be read"))?;
            let pwm = read_ok(&out.path(""))
                .filter(|v| v.parse::<u32>().is_ok_and(|n| n <= 255))
                .ok_or_else(|| std::io::Error::other("Its speed can't be read"))?;
            self.originals
                .insert(out.id.clone(), Original { enable, pwm });
            // Recorded first: if the journal can't be written, the fan isn't touched.
            if let Err(e) = self.save_journal() {
                self.originals.remove(&out.id);
                return Err(e);
            }
        }
        if read(&out.path("_enable")) != "1" {
            write(&out.path("_enable"), "1")?;
        }
        Ok(())
    }
    /// Hand one output back to the firmware. True once it is back (or was never taken).
    /// When the output refuses, its firmware setting stays recorded (in memory and in the
    /// journal) so it is tried again, and the fan is set to full speed meanwhile.
    pub fn release(&mut self, id: &str) -> bool {
        self.hand_back(id) == HandBack::Back
    }
    fn hand_back(&mut self, id: &str) -> HandBack {
        let Some(original) = self.originals.get(id).cloned() else {
            return HandBack::Back;
        };
        let out = self
            .inventory
            .outputs
            .iter()
            .find(|o| o.id == id && o.intact())
            .cloned();
        let Some(out) = out else {
            self.rediscover = true; // moved or gone: found again by the next look
            return HandBack::Unknown;
        };
        if !restore_output(&out, &original) {
            let full = write(&out.path(""), "255").is_ok() && read(&out.path("")) == "255";
            return if full {
                HandBack::FullSpeed
            } else {
                HandBack::Unknown
            };
        }
        self.originals.remove(id);
        self.last.remove(id);
        let _ = self.save_journal(); // a stale entry only restores the same setting again
        HandBack::Back
    }
    /// True when every output is back with the firmware.
    pub fn release_all(&mut self) -> bool {
        let mut all = true;
        for id in self.originals.keys().cloned().collect::<Vec<_>>() {
            all &= self.release(&id);
        }
        all
    }
    /// The page's view of an output that should be with the motherboard.
    fn released_state(&mut self, id: &str, note: Option<&str>) -> Value {
        match self.hand_back(id) {
            HandBack::Back => json!({"mode": "auto", "note": note}),
            HandBack::FullSpeed => json!({"mode": "stuck", "percent": 100.0, "note": NOT_BACK}),
            HandBack::Unknown => json!({"mode": "stuck", "note": NOT_BACK_UNKNOWN}),
        }
    }
    /// Firmware settings not yet put back (after a crash, or by detection), kept so
    /// this controller keeps trying.
    pub fn adopt_originals(
        &mut self,
        originals: BTreeMap<String, Original>,
    ) -> std::io::Result<()> {
        for (id, o) in originals {
            self.originals.entry(id).or_insert(o);
        }
        self.save_journal()
    }
    /// One control step: every output to its target speed (or back to the firmware).
    pub fn step(&mut self) {
        self.track_heat();
        let emergency = self.emergency();
        for out in self.inventory.outputs.clone() {
            let setting = self.settings.get(&out.id).cloned();
            let target = match setting.as_ref().map(|s| &s.mode) {
                None | Some(Mode::Automatic) => {
                    let state = self.released_state(&out.id, None);
                    self.status.insert(out.id.clone(), state);
                    continue;
                }
                Some(Mode::Fixed(pct)) => Ok(*pct),
                Some(Mode::Curve { sensor, points }) => self
                    .temperature(sensor)
                    .map(|t| curve_duty(points, t))
                    .ok_or("The curve's temperature can't be read"),
            };
            let min = setting.as_ref().map_or(MIN_FLOOR, |s| s.min.max(MIN_FLOOR));
            let (wanted, why) = match (&emergency, target) {
                (Some(hot), _) => (100.0, Some(format!("Full speed: {hot}"))),
                (None, Err(e)) => (100.0, Some(format!("Full speed: {e}"))),
                (None, Ok(_)) if self.stall_latched(&out.id, setting.as_ref()) => {
                    (100.0, Some(STALLED.to_string()))
                }
                (None, Ok(pct)) => (pct.clamp(min, 100.0), None),
            };
            // Speed up at once; slow down gently so fans don't hunt up and down.
            let previous = self.last.get(&out.id).copied().unwrap_or(wanted);
            let duty = if wanted < previous {
                (previous - STEP_DOWN).max(wanted)
            } else {
                wanted
            };
            let result = self
                .take_over(&out)
                .and_then(|_| write(&out.path(""), &pwm_value(duty)));
            match result {
                Ok(()) => {
                    self.watch_stall(&out.id, setting.as_ref(), duty);
                    self.last.insert(out.id.clone(), duty);
                    self.status.insert(out.id.clone(), json!({"mode": "controlled", "percent": (duty * 10.0).round() / 10.0, "note": why}));
                }
                Err(_) => {
                    // Can't drive it: give it back to the firmware rather than leave it stuck.
                    let state = self.released_state(
                        &out.id,
                        Some("NekoServer couldn't control this output, so the motherboard is in charge"),
                    );
                    self.status.insert(out.id.clone(), state);
                }
            }
        }
    }
}
/// Below this a fan counts as stopped (a spinning fan reads several hundred RPM).
const STALL_RPM: f64 = 150.0;
/// Control steps (2 s each) a linked fan must read stopped before full speed is set.
const STALL_STEPS: u32 = 3;
const STALLED: &str = "Full speed: a fan on this output stopped at the speed it was given. Raise \"Never slower than\" for it, then Save";
impl Controller {
    /// Full speed latched for this output after its fans stopped, until its setting is
    /// changed (a higher minimum, say) or the service starts again.
    fn stall_latched(&mut self, id: &str, setting: Option<&Setting>) -> bool {
        match self.stalls.get(id) {
            Some((_, Some(latched))) if Some(latched) == setting => true,
            Some((_, Some(_))) => {
                self.stalls.remove(id); // the setting changed: try it
                false
            }
            _ => false,
        }
    }
    /// Count the steps in which every fan linked to this output (by Detect fans) reads
    /// stopped while it is driven; after STALL_STEPS, full speed until the setting
    /// changes. Only ever raises a speed. A speed that can't be read never counts.
    fn watch_stall(&mut self, id: &str, setting: Option<&Setting>, duty: f64) {
        let linked = setting.map(|s| s.fans.as_slice()).unwrap_or_default();
        let speeds: Vec<Option<f64>> = linked
            .iter()
            .filter_map(|f| self.inventory.fans.iter().find(|c| &c.id == f))
            .map(|f| number(&f.path("_input")))
            .collect();
        let stopped = duty > 0.0
            && duty < 100.0
            && !speeds.is_empty()
            && speeds.iter().all(|v| v.is_some_and(|rpm| rpm < STALL_RPM));
        let entry = self.stalls.entry(id.to_string()).or_insert((0, None));
        if entry.1.is_some() {
            return;
        }
        entry.0 = if stopped { entry.0 + 1 } else { 0 };
        if entry.0 >= STALL_STEPS {
            entry.1 = setting.cloned();
        }
    }
}
const NOT_BACK: &str =
    "The motherboard didn't take this fan back, so it runs at full speed; NekoServer keeps trying";
const NOT_BACK_UNKNOWN: &str =
    "The motherboard didn't take this fan back and its speed couldn't be set; NekoServer keeps trying. Check the fans";
#[derive(Clone, Copy, Debug, PartialEq)]
enum HandBack {
    Back,
    /// Not back, but set to full speed meanwhile.
    FullSpeed,
    /// Not back, and full speed couldn't be set either.
    Unknown,
}
/// Put the firmware's setting back, and check that it took. False when it didn't.
pub fn restore_output(out: &Channel, original: &Original) -> bool {
    // A record that isn't a real setting is never "put back" (that could switch the
    // fan to a mode it never had): it stays, to be checked.
    if !original.valid() || !out.intact() {
        return false;
    }
    // Manual with a fixed value before: put that value back too.
    if original.enable == "1" && write(&out.path(""), &original.pwm).is_err() {
        return false;
    }
    write(&out.path("_enable"), &original.enable).is_ok()
        && read(&out.path("_enable")) == original.enable
}

/// Which fans each output drives: all outputs to full speed, then one at a time to
/// 35 % for a few seconds; the fans that slow down by 30 % or more belong to it.
/// `wait(seconds)` sleeps (and lets tests move the fans); it returns false to stop
/// (cancelled, or a temperature too high), and every output goes back to the
/// firmware either way.
pub fn detect<F: FnMut(&Controller, u64) -> bool>(
    control: &mut Controller,
    mut wait: F,
) -> Result<BTreeMap<String, Vec<String>>, String> {
    if !control.release_all() {
        return Err("A fan didn't go back to the motherboard, so detection can't start".into());
    }
    let outputs = control.inventory.outputs.clone();
    let fans = control.inventory.fans.clone();
    // A speed that can't be read is unknown, never 0 RPM (which would look like a
    // slowdown). All fans are read together; the ones that failed are read again after
    // a short wait that still watches temperatures, Stop and shutdown (at most twice,
    // so one measurement never takes more than about two seconds, however many fans
    // fail). A fan that stays unreadable is left out of that measurement.
    let rpm = |f: &Channel| number(&f.path("_input")).filter(|v| v.is_finite() && *v >= 0.0);
    let measure = |c: &Controller, wait: &mut F| -> Result<BTreeMap<String, Option<f64>>, String> {
        let mut speeds = fans
            .iter()
            .map(|f| (f.id.clone(), rpm(f)))
            .collect::<BTreeMap<_, _>>();
        for _ in 0..2 {
            if speeds.values().all(Option::is_some) {
                break;
            }
            if !wait(c, 1) {
                return Err("Stopped".to_string());
            }
            for f in &fans {
                if speeds[&f.id].is_none() {
                    speeds.insert(f.id.clone(), rpm(f));
                }
            }
        }
        Ok(speeds)
    };
    let set = |c: &mut Controller, out: &Channel, pct: f64| -> Result<(), String> {
        c.take_over(out)
            .and_then(|_| write(&out.path(""), &pwm_value(pct)))
            .map_err(|_| format!("{} refused a new speed", out.label))
    };
    let result = (|| {
        for out in &outputs {
            set(control, out, 100.0)?;
        }
        if !wait(control, 6) {
            return Err("Stopped".to_string());
        }
        let baseline = measure(control, &mut wait)?;
        let mut found = BTreeMap::new();
        for out in &outputs {
            set(control, out, 35.0)?;
            if !wait(control, 7) {
                return Err("Stopped".to_string());
            }
            let now = measure(control, &mut wait)?;
            let slowed = fans
                .iter()
                .filter(|f| match (baseline[&f.id], now[&f.id]) {
                    (Some(before), Some(now)) => before >= 100.0 && now <= before * 0.7,
                    _ => false,
                })
                .map(|f| f.id.clone())
                .collect::<Vec<_>>();
            found.insert(out.id.clone(), slowed);
            set(control, out, 100.0)?;
            if !wait(control, 3) {
                return Err("Stopped".to_string());
            }
        }
        Ok(found)
    })();
    control.release_all();
    result
}

pub fn inventory_json(inv: &Inventory) -> Value {
    let one = |c: &Channel, value: Value| json!({"id": c.id, "label": c.label, "chip": c.chip, "value": value});
    json!({
        "outputs": inv.outputs.iter().map(|o| one(o, json!(number(&o.path("")).map(|v| (v * 100.0 / 255.0).round())))).collect::<Vec<_>>(),
        "fans": inv.fans.iter().map(|f| one(f, json!(number(&f.path("_input"))))).collect::<Vec<_>>(),
        "temps": inv.temps.iter().map(|t| one(t, json!(number(&t.path("_input")).map(|v| (v / 100.0).round() / 10.0)))).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
pub mod tests {
    use super::*;

    pub struct Board(pub PathBuf);
    impl Board {
        /// A motherboard chip with two fan outputs and two fans (fan1 on pwm1, fan2 on
        /// pwm2), a processor sensor, and a drive sensor that must never be read.
        pub fn new(name: &str) -> Self {
            let root =
                std::env::temp_dir().join(format!("neko-fans-{name}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            let put = |rel: &str, v: &str| {
                let p = root.join(rel);
                fs::create_dir_all(p.parent().unwrap()).unwrap();
                fs::write(p, v).unwrap();
            };
            put("devices/nct/x", "");
            put("hwmon/hwmon1/name", "nct6798");
            for n in [1, 2] {
                put(&format!("hwmon/hwmon1/pwm{n}"), "128");
                put(&format!("hwmon/hwmon1/pwm{n}_enable"), "5");
                put(&format!("hwmon/hwmon1/fan{n}_input"), "1000");
            }
            put("hwmon/hwmon1/fan2_label", "CPU fan");
            put("hwmon/hwmon1/temp1_input", "45000");
            put("hwmon/hwmon1/temp1_label", "CPUTIN");
            put("hwmon/hwmon1/pwm3", "128"); // no _enable: not controllable
            put("hwmon/hwmon2/name", "drivetemp");
            put("hwmon/hwmon2/temp1_input", "40000");
            std::os::unix::fs::symlink(root.join("devices/nct"), root.join("hwmon/hwmon1/device"))
                .unwrap();
            Board(root)
        }
        pub fn hwmon(&self) -> PathBuf {
            self.0.join("hwmon")
        }
        pub fn get(&self, rel: &str) -> String {
            read(&self.0.join("hwmon/hwmon1").join(rel))
        }
        pub fn set(&self, rel: &str, v: &str) {
            fs::write(self.0.join("hwmon/hwmon1").join(rel), v).unwrap();
        }
        /// Fans follow their outputs (fanN on pwmN): 2000 RPM at full speed.
        pub fn spin(&self) {
            for n in [1, 2] {
                let pwm: f64 = self.get(&format!("pwm{n}")).parse().unwrap();
                self.set(
                    &format!("fan{n}_input"),
                    &((pwm / 255.0 * 2000.0) as u32).to_string(),
                );
            }
        }
    }
    impl Drop for Board {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn curve(sensor: &str) -> Setting {
        Setting {
            mode: Mode::Curve {
                sensor: sensor.into(),
                points: vec![(30.0, 20.0), (60.0, 60.0), (80.0, 100.0)],
            },
            min: 25.0,
            name: String::new(),
            fans: vec![],
        }
    }

    #[test]
    fn outputs_fans_and_temperatures_are_found_with_stable_names() {
        let b = Board::new("find");
        let inv = discover(&b.hwmon());
        assert_eq!(
            inv.outputs
                .iter()
                .map(|o| o.file.as_str())
                .collect::<Vec<_>>(),
            ["pwm1", "pwm2"]
        );
        assert_eq!(inv.fans[1].label, "CPU fan");
        assert_eq!(inv.temps.len(), 1, "drive temperatures are never read");
        let id = &inv.outputs[0].id;
        assert!(
            id.starts_with("nct6798@") && id.ends_with("/devices/nct/pwm1"),
            "{id}"
        );
    }

    #[test]
    fn curves_are_straight_lines_between_points() {
        let p = [(30.0, 20.0), (60.0, 60.0), (80.0, 100.0)];
        assert_eq!(curve_duty(&p, 10.0), 20.0);
        assert_eq!(curve_duty(&p, 45.0), 40.0);
        assert_eq!(curve_duty(&p, 70.0), 80.0);
        assert_eq!(curve_duty(&p, 99.0), 100.0);
        assert_eq!(curve_duty(&[], 50.0), 100.0);
    }

    #[test]
    fn settings_are_checked() {
        let ok = setting_from(
            &json!({"mode":"curve","sensor":"x","points":[[30,20],[70,100]],"min":30}),
        )
        .unwrap();
        assert_eq!(setting_from(&setting_json(&ok)).unwrap(), ok);
        for bad in [
            json!({"mode":"curve","sensor":"x","points":[[30,20]]}),
            json!({"mode":"curve","sensor":"x","points":[[70,20],[30,100]]}),
            json!({"mode":"curve","sensor":"x","points":[[30,20],[70,140]]}),
            json!({"mode":"fixed","fixed":50,"min":5}),
            json!({"mode":"turbo"}),
        ] {
            assert!(setting_from(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn curves_drive_the_fans_with_a_floor_and_gentle_slowdown() {
        let b = Board::new("drive");
        let inv = discover(&b.hwmon());
        let (out, temp) = (inv.outputs[1].id.clone(), inv.temps[0].id.clone());
        let mut c = Controller::new(inv, BTreeMap::from([(out.clone(), curve(&temp))]));
        c.step();
        assert_eq!(b.get("pwm2_enable"), "1");
        assert_eq!(b.get("pwm2"), "102"); // 45 °C → 40 %
        assert_eq!(
            b.get("pwm1_enable"),
            "5",
            "outputs without a setting stay with the firmware"
        );
        b.set("temp1_input", "20000");
        c.step();
        assert_eq!(b.get("pwm2"), "89"); // heading for the 25 % floor, 5 % per step: 35 %
        b.set("temp1_input", "75000");
        c.step();
        assert_eq!(b.get("pwm2"), "230"); // 90 % at once
        c.release_all();
        assert_eq!(
            (b.get("pwm2_enable").as_str(), b.get("pwm2").as_str()),
            ("5", "230")
        );
    }

    #[test]
    fn heat_or_a_lost_sensor_means_full_speed() {
        let b = Board::new("safe");
        let inv = discover(&b.hwmon());
        let out = inv.outputs[0].id.clone();
        let mut c = Controller::new(
            inv,
            BTreeMap::from([(out.clone(), curve("chip@/gone/temp9"))]),
        );
        c.step();
        assert_eq!(b.get("pwm1"), "255");
        assert!(c.status[&out]["note"]
            .as_str()
            .unwrap()
            .contains("can't be read"));
        let inv = discover(&b.hwmon());
        let mut c = Controller::new(
            inv,
            BTreeMap::from([(
                out.clone(),
                Setting {
                    mode: Mode::Fixed(30.0),
                    min: 20.0,
                    name: String::new(),
                    fans: vec![],
                },
            )]),
        );
        // 96 °C once is how current processors run under load: not yet an emergency...
        b.set("temp1_input", "96000");
        c.step();
        assert_eq!(b.get("pwm1"), "77");
        // ...but five minutes above 95 °C is.
        for since in c.hot_since.values_mut() {
            *since = std::time::Instant::now() - HOT_FOR;
        }
        c.step();
        assert_eq!(b.get("pwm1"), "255");
        // Cooling below 95 °C starts the five minutes over; 105 °C is full speed at once.
        b.set("temp1_input", "90000");
        c.step();
        assert!(c.hot_since.is_empty());
        b.set("temp1_input", "106000");
        c.step();
        assert_eq!(b.get("pwm1"), "255");
        b.set("temp1_crit", "80000");
        b.set("temp1_input", "78000");
        c.step();
        assert_eq!(b.get("pwm1"), "255", "within 3 °C of critical");
    }

    #[test]
    fn detect_pairs_each_output_with_its_fan_and_restores_the_firmware() {
        let b = Board::new("detect");
        let mut c = Controller::new(discover(&b.hwmon()), BTreeMap::new());
        let found = detect(&mut c, |_, _| {
            b.spin();
            true
        })
        .unwrap();
        let ids = |i: usize| c.inventory.fans[i].id.clone();
        assert_eq!(found[&c.inventory.outputs[0].id], vec![ids(0)]);
        assert_eq!(found[&c.inventory.outputs[1].id], vec![ids(1)]);
        assert_eq!(
            (b.get("pwm1_enable").as_str(), b.get("pwm2_enable").as_str()),
            ("5", "5")
        );
        // Cancelled half-way: still back with the firmware.
        let mut steps = 0;
        assert!(detect(&mut c, |_, _| {
            steps += 1;
            steps < 2
        })
        .is_err());
        assert_eq!(b.get("pwm1_enable"), "5");
    }

    /// Unreadable fan speeds never pause the safety checks: every wait between reads
    /// goes through `wait` (which watches temperatures and Stop), and a measurement
    /// gives up on them after two short retries.
    #[test]
    fn failed_fan_speeds_never_hold_up_the_safety_checks() {
        let b = Board::new("detect-many-bad");
        for n in 1..=10 {
            b.set(&format!("fan{n}_input"), "read error");
        }
        let mut c = Controller::new(discover(&b.hwmon()), BTreeMap::new());
        assert_eq!(c.inventory.fans.len(), 10);
        let mut last = std::time::Instant::now();
        let mut longest = std::time::Duration::ZERO;
        let mut retries = 0;
        let found = detect(&mut c, |_, seconds| {
            longest = longest.max(last.elapsed());
            retries += usize::from(seconds == 1);
            last = std::time::Instant::now();
            true
        })
        .unwrap();
        assert!(
            longest < std::time::Duration::from_millis(100),
            "{longest:?}"
        );
        assert_eq!(
            retries,
            2 * (1 + c.inventory.outputs.len()),
            "two short retries per measurement"
        );
        assert!(
            found.values().all(Vec::is_empty),
            "unreadable fans are left out"
        );
        // Stop (or heat) during a retry ends detection, fans back with the firmware.
        let mut calls = 0;
        let stopped = detect(&mut c, |_, seconds| {
            calls += 1;
            seconds != 1
        });
        assert!(stopped.is_err() && calls == 2);
        assert_eq!(b.get("pwm1_enable"), "5");
    }

    /// A speed that stops reading is unknown, not a slowdown: it never pairs a fan
    /// with the wrong output.
    #[test]
    fn an_unreadable_fan_speed_is_never_taken_for_a_slowdown() {
        let b = Board::new("detect-rpm");
        let mut c = Controller::new(discover(&b.hwmon()), BTreeMap::new());
        let mut calls = 0;
        let found = detect(&mut c, |_, seconds| {
            b.spin();
            calls += 1;
            if seconds == 7 && calls == 2 {
                b.set("fan2_input", "read error"); // while output 1 is tested
            }
            true
        })
        .unwrap();
        let first = &found[&c.inventory.outputs[0].id];
        assert_eq!(
            first,
            &vec![c.inventory.fans[0].id.clone()],
            "fan 2 not taken for output 1"
        );
    }
}

#[cfg(test)]
mod review_extra_tests {
    use super::*;
    #[test]
    fn review_fixed_speed_fails_safe_when_all_temperature_readings_disappear() {
        let b = tests::Board::new("review-fixed-blind");
        let inv = discover(&b.hwmon());
        let out = inv.outputs[0].id.clone();
        let setting = setting_from(&json!({"mode":"fixed","fixed":25})).unwrap();
        let mut c = Controller::new(inv, BTreeMap::from([(out, setting)]));
        c.step();
        fs::remove_file(b.hwmon().join("hwmon1/temp1_input")).unwrap();
        c.step();
        assert_eq!(
            b.get("pwm1"),
            "255",
            "loss of all temperature monitoring must trigger full speed"
        );
    }
}

#[cfg(test)]
mod review_hotplug_tests {
    use super::*;
    #[test]
    fn review_reused_hwmon_number_must_not_control_an_unrelated_chip() {
        let b = tests::Board::new("review-hwmon-reuse");
        let inv = discover(&b.hwmon());
        let id = inv.outputs[0].id.clone();
        let setting = setting_from(&json!({"mode":"fixed","fixed":25})).unwrap();
        let mut c = Controller::new(inv, BTreeMap::from([(id, setting)]));
        fs::rename(b.hwmon().join("hwmon1"), b.hwmon().join("hwmon9")).unwrap();
        let other = b.hwmon().join("hwmon1");
        fs::create_dir_all(&other).unwrap();
        for (k, v) in [
            ("name", "unrelated_chip"),
            ("pwm1", "180"),
            ("pwm1_enable", "5"),
            ("temp1_input", "45000"),
        ] {
            fs::write(other.join(k), v).unwrap();
        }
        c.step();
        assert_eq!(
            fs::read_to_string(other.join("pwm1")).unwrap(),
            "180",
            "old channel path now controls another chip"
        );
    }
}

#[cfg(test)]
mod review_fault_tests {
    use super::*;
    #[test]
    fn review_takeover_requires_a_readable_original_mode() {
        let b = tests::Board::new("review-unreadable-original");
        let inv = discover(&b.hwmon());
        let output = inv.outputs[0].id.clone();
        fs::write(b.hwmon().join("hwmon1/pwm1_enable"), [0xff]).unwrap();
        let mut c = Controller::new(
            inv,
            BTreeMap::from([(
                output,
                setting_from(&json!({"mode":"fixed","fixed":25})).unwrap(),
            )]),
        );
        c.step();
        assert_eq!(
            b.get("pwm1"),
            "128",
            "controller took over even though its original mode could not be read"
        );
    }
    #[test]
    fn an_unconnected_probe_is_not_an_overheating_one() {
        let b = tests::Board::new("unconnected-probe");
        // AUXTIN with nothing connected reads 127 °C on many nct67xx boards.
        b.set("temp2_input", "127000");
        b.set("temp2_label", "AUXTIN3");
        let inv = discover(&b.hwmon());
        let c = Controller::new(inv, BTreeMap::new());
        assert_eq!(c.emergency(), None);
        let probe = c
            .inventory
            .temps
            .iter()
            .find(|t| t.label == "AUXTIN3")
            .map(|t| t.id.clone());
        if let Some(id) = probe {
            assert_eq!(c.temperature(&id), None);
        }
        // A real one that overheats still sends everything to full speed.
        b.set("temp1_input", "106000");
        assert!(c.emergency().is_some());
        // A sensor that says it has a fault is not read either; with no other reading
        // left, that is "no temperature can be read": full speed, as before.
        b.set("temp1_input", "45000");
        b.set("temp1_fault", "1");
        let cpu = c
            .inventory
            .temps
            .iter()
            .find(|t| t.label == "CPUTIN")
            .unwrap()
            .id
            .clone();
        assert_eq!(c.temperature(&cpu), None);
        b.set("temp1_fault", "0");
        assert_eq!(c.temperature(&cpu), Some(45.0));
        assert_eq!(c.emergency(), None);
    }
    #[test]
    fn review_full_speed_is_not_claimed_when_the_emergency_write_fails() {
        let b = tests::Board::new("review-refused-full-speed");
        let inv = discover(&b.hwmon());
        let output = inv.outputs[0].id.clone();
        let mut c = Controller::new(
            inv,
            BTreeMap::from([(
                output.clone(),
                setting_from(&json!({"mode":"fixed","fixed":40})).unwrap(),
            )]),
        );
        c.step();
        for f in ["pwm1", "pwm1_enable"] {
            let p = b.hwmon().join("hwmon1").join(f);
            fs::remove_file(&p).unwrap();
            fs::create_dir(&p).unwrap();
        }
        c.settings.insert(
            output.clone(),
            setting_from(&json!({"mode":"auto"})).unwrap(),
        );
        c.step();
        assert_ne!(
            c.status[&output]["percent"],
            json!(100.0),
            "full speed claimed despite refused PWM write: {}",
            c.status[&output]
        );
    }
}

#[cfg(test)]
mod stall_tests {
    use super::*;
    fn setting(fixed: f64, fans: &[String]) -> Setting {
        let mut s = setting_from(&json!({"mode":"fixed","fixed":fixed})).unwrap();
        s.fans = fans.to_vec();
        s
    }
    /// A fan that stops at the speed it was given (common for 3-pin fans at 20 %) is
    /// noticed after a few steps: full speed, with a note, until the setting changes.
    #[test]
    fn a_fan_that_stops_at_its_speed_gets_full_speed() {
        let b = tests::Board::new("stall");
        let inv = discover(&b.hwmon());
        let (out, fan) = (inv.outputs[0].id.clone(), inv.fans[0].id.clone());
        let mut c = Controller::new(
            inv,
            BTreeMap::from([(out.clone(), setting(20.0, std::slice::from_ref(&fan)))]),
        );
        b.set("fan1_input", "0");
        for _ in 0..STALL_STEPS {
            c.step();
        }
        assert_eq!(
            b.get("pwm1"),
            "51",
            "not before {STALL_STEPS} stopped readings"
        );
        c.step();
        assert_eq!(b.get("pwm1"), "255");
        assert!(c.status[&out]["note"].as_str().unwrap().contains("stopped"));
        // Spinning again at full speed: it stays at full speed (no hunting).
        b.set("fan1_input", "2000");
        c.step();
        assert_eq!(b.get("pwm1"), "255");
        // A new setting (a higher minimum) is tried again, slowing down gently.
        c.settings.insert(out.clone(), setting(40.0, &[fan]));
        c.step();
        assert_eq!(b.get("pwm1"), "242");
        assert!(c.status[&out]["note"].is_null());
    }
    /// Never for a fan that spins, can't be read, or isn't linked to the output.
    #[test]
    fn spinning_unreadable_or_unlinked_fans_change_nothing() {
        let b = tests::Board::new("no-stall");
        let inv = discover(&b.hwmon());
        let (out, fan) = (inv.outputs[0].id.clone(), inv.fans[0].id.clone());
        drop(inv);
        for (reading, linked) in [("900", true), ("read error", true), ("0", false)] {
            b.set("fan1_input", reading);
            let fans = if linked { vec![fan.clone()] } else { vec![] };
            let mut c = Controller::new(
                discover(&b.hwmon()),
                BTreeMap::from([(out.clone(), setting(20.0, &fans))]),
            );
            for _ in 0..6 {
                c.step();
            }
            assert_eq!(b.get("pwm1"), "51", "{reading} linked={linked}");
            assert!(c.status[&out]["note"].is_null());
        }
    }
}
