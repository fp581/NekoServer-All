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
const EMERGENCY_C: f64 = 95.0;
/// How fast a fan may slow down per control step (speeding up is immediate).
const STEP_DOWN: f64 = 5.0;
/// SATA drive temperatures are never read here (it can keep a hard drive awake).
const SKIPPED_CHIPS: [&str; 1] = ["drivetemp"];

fn read(path: &Path) -> String {
    fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
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
    pub dir: PathBuf,
    pub file: String,
    pub label: String,
    pub chip: String,
}
impl Channel {
    fn path(&self, suffix: &str) -> PathBuf {
        self.dir.join(format!("{}{suffix}", self.file))
    }
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
        let device = fs::canonicalize(dir.join("device")).unwrap_or_else(|_| dir.clone());
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
    pub fn temperature(&self, id: &str) -> Option<f64> {
        let t = self.inventory.temps.iter().find(|t| t.id == id)?;
        number(&t.path("_input"))
            .map(|v| v / 1000.0)
            .filter(|v| (-40.0..=150.0).contains(v))
    }
    /// Any sensor at its critical temperature (less 3 °C), or at 95 °C: everything to full.
    pub fn emergency(&self) -> Option<String> {
        self.inventory.temps.iter().find_map(|t| {
            let v = number(&t.path("_input"))? / 1000.0;
            let crit = number(&t.path("_crit"))
                .map(|c| c / 1000.0)
                .filter(|c| *c > 40.0);
            let hot = crit.map_or(v >= EMERGENCY_C, |c| v >= c - 3.0) || v >= EMERGENCY_C;
            hot.then(|| format!("{} {} at {v:.0} °C", t.chip, t.label))
        })
    }
    fn take_over(&mut self, out: &Channel) -> std::io::Result<()> {
        if !self.originals.contains_key(&out.id) {
            self.originals.insert(
                out.id.clone(),
                Original {
                    enable: read(&out.path("_enable")),
                    pwm: read(&out.path("")),
                },
            );
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
        let Some(original) = self.originals.get(id).cloned() else {
            return true;
        };
        let out = self.inventory.outputs.iter().find(|o| o.id == id).cloned();
        if !out
            .as_ref()
            .is_some_and(|out| restore_output(out, &original))
        {
            if let Some(out) = out {
                let _ = write(&out.path(""), "255");
            }
            return false;
        }
        self.originals.remove(id);
        self.last.remove(id);
        let _ = self.save_journal(); // a stale entry only restores the same setting again
        true
    }
    /// True when every output is back with the firmware.
    pub fn release_all(&mut self) -> bool {
        let mut all = true;
        for id in self.originals.keys().cloned().collect::<Vec<_>>() {
            all &= self.release(&id);
        }
        all
    }
    /// Firmware settings not yet put back (after a crash, or by detection), kept so
    /// this controller keeps trying.
    pub fn adopt_originals(&mut self, originals: BTreeMap<String, Original>) {
        for (id, o) in originals {
            self.originals.entry(id).or_insert(o);
        }
        let _ = self.save_journal();
    }
    /// One control step: every output to its target speed (or back to the firmware).
    pub fn step(&mut self) {
        let emergency = self.emergency();
        for out in self.inventory.outputs.clone() {
            let setting = self.settings.get(&out.id).cloned();
            let target = match setting.as_ref().map(|s| &s.mode) {
                None | Some(Mode::Automatic) => {
                    let state = if self.release(&out.id) {
                        json!({"mode": "auto"})
                    } else {
                        json!({"mode": "stuck", "percent": 100.0, "note": NOT_BACK})
                    };
                    self.status.insert(out.id.clone(), state);
                    continue;
                }
                Some(Mode::Fixed(pct)) => Ok(*pct),
                Some(Mode::Curve { sensor, points }) => self
                    .temperature(sensor)
                    .map(|t| curve_duty(points, t))
                    .ok_or("The curve's temperature can't be read"),
            };
            let min = setting.map_or(MIN_FLOOR, |s| s.min.max(MIN_FLOOR));
            let (wanted, why) = match (&emergency, target) {
                (Some(hot), _) => (100.0, Some(format!("Full speed: {hot}"))),
                (None, Err(e)) => (100.0, Some(format!("Full speed: {e}"))),
                (None, Ok(pct)) => (pct.clamp(min, 100.0), None),
            };
            // Speed up at once; slow down gently so fans don't hunt up and down.
            let previous = self.last.get(&out.id).copied().unwrap_or(wanted);
            let duty = if wanted < previous {
                (previous - STEP_DOWN).max(wanted)
            } else {
                wanted
            };
            let result = self.take_over(&out).and_then(|_| {
                write(
                    &out.path(""),
                    &((duty * 255.0 / 100.0).round() as u32).min(255).to_string(),
                )
            });
            match result {
                Ok(()) => {
                    self.last.insert(out.id.clone(), duty);
                    self.status.insert(out.id.clone(), json!({"mode": "controlled", "percent": (duty * 10.0).round() / 10.0, "note": why}));
                }
                Err(_) => {
                    // Can't drive it: give it back to the firmware rather than leave it stuck.
                    let state = if self.release(&out.id) {
                        json!({"mode": "auto", "note": "This output refused the new speed, so the motherboard is in charge"})
                    } else {
                        json!({"mode": "stuck", "note": NOT_BACK})
                    };
                    self.status.insert(out.id.clone(), state);
                }
            }
        }
    }
}
const NOT_BACK: &str =
    "The motherboard didn't take this fan back, so it runs at full speed; NekoServer keeps trying";
/// Put the firmware's setting back, and check that it took. False when it didn't.
pub fn restore_output(out: &Channel, original: &Original) -> bool {
    // Manual with a fixed value before: put that value back too.
    if original.enable == "1"
        && !original.pwm.is_empty()
        && write(&out.path(""), &original.pwm).is_err()
    {
        return false;
    }
    let enable = if original.enable.is_empty() {
        "2"
    } else {
        &original.enable
    };
    write(&out.path("_enable"), enable).is_ok() && read(&out.path("_enable")) == enable
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
    control.release_all();
    let outputs = control.inventory.outputs.clone();
    let fans = control.inventory.fans.clone();
    let rpm = |f: &Channel| number(&f.path("_input")).unwrap_or(0.0);
    let set = |c: &mut Controller, out: &Channel, pct: f64| -> Result<(), String> {
        c.take_over(out)
            .and_then(|_| {
                write(
                    &out.path(""),
                    &((pct * 255.0 / 100.0).round() as u32).to_string(),
                )
            })
            .map_err(|_| format!("{} refused a new speed", out.label))
    };
    let result = (|| {
        for out in &outputs {
            set(control, out, 100.0)?;
        }
        if !wait(control, 6) {
            return Err("Stopped".to_string());
        }
        let baseline = fans
            .iter()
            .map(|f| (f.id.clone(), rpm(f)))
            .collect::<BTreeMap<_, _>>();
        let mut found = BTreeMap::new();
        for out in &outputs {
            set(control, out, 35.0)?;
            if !wait(control, 7) {
                return Err("Stopped".to_string());
            }
            let slowed = fans
                .iter()
                .filter(|f| {
                    let before = baseline[&f.id];
                    before >= 100.0 && rpm(f) <= before * 0.7
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
        b.set("temp1_input", "96000");
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
}
