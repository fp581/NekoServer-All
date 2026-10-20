//! Live hardware readings for the dashboard: read-only, unprivileged, taken on demand.
//!
//! Everything comes from what Linux exposes to an ordinary user (/proc and /sys), so
//! the dashboard's web server can read it without root. Nothing here writes to
//! hardware: fan speeds, RGB lighting and processes are only reported. A reading
//! the kernel does not expose stays `null` ("unavailable"), never 0.
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

/// Readings are cached briefly so several open dashboards share one sample.
const LIVE_EVERY: Duration = Duration::from_secs(2);
const PROCESSES_EVERY: Duration = Duration::from_secs(3);
const RGB_EVERY: Duration = Duration::from_secs(60);
const MAX_PROCESSES: usize = 5000;
const MAX_SENSORS: usize = 256;
/// Chips never read every few seconds. drivetemp asks SATA drives for their
/// temperature, and on some hard drives that resets the spin-down timer, so an open
/// dashboard would keep the media HDD awake. Those temperatures come from the
/// collector's SMART check instead (every 30 minutes, never waking a sleeping drive).
const SKIPPED_CHIPS: [&str; 1] = ["drivetemp"];

pub struct Live {
    proc: PathBuf,
    sys: PathBuf,
    pci_ids: Vec<PathBuf>,
    rgb_port: u16,
    previous: Option<Counters>,
    processes_before: Option<ProcessTicks>,
    live: Option<(Instant, Value)>,
    processes: Option<(Instant, Value)>,
    rgb: Option<(Instant, Value)>,
    names: HashMap<String, Option<String>>,
}

/// When the last process scan ran, on which boot, and each process's (CPU ticks,
/// start time): a process ID reused by a new process is not compared.
type ProcessTicks = (Instant, String, HashMap<u32, (u64, u64)>);

/// Counters from the previous sample; rates and loads are differences between two.
struct Counters {
    at: Instant,
    boot: String,
    cpu: HashMap<String, (u64, u64)>,
    net: HashMap<String, (u64, u64)>,
    disk: HashMap<String, (u64, u64, u64)>,
    npu: HashMap<String, f64>,
}

impl Default for Live {
    fn default() -> Self {
        Self::new(Path::new("/proc"), Path::new("/sys"))
    }
}

/// A small text file from /proc or /sys (they never need more than this).
fn read(path: &Path) -> String {
    let mut text = String::new();
    if let Ok(file) = fs::File::open(path) {
        let _ = file.take(65536).read_to_string(&mut text);
    }
    text.trim().to_string()
}
fn number(path: &Path) -> Option<f64> {
    read(path).parse::<f64>().ok().filter(|v| v.is_finite())
}
fn name_of(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}
fn children(dir: &Path) -> Vec<PathBuf> {
    let mut rows = fs::read_dir(dir)
        .map(|r| r.flatten().map(|e| e.path()).collect::<Vec<_>>())
        .unwrap_or_default();
    rows.sort_by(|a, b| natural(&name_of(a), &name_of(b)));
    rows
}
/// "hwmon2" before "hwmon10", "cpu9" before "cpu10".
fn natural(a: &str, b: &str) -> std::cmp::Ordering {
    let split = |s: &str| {
        let digits = s.len() - s.bytes().rev().take_while(u8::is_ascii_digit).count();
        (
            s[..digits].to_string(),
            s[digits..].parse::<u64>().unwrap_or(0),
        )
    };
    split(a).cmp(&split(b))
}
fn clean(s: &str, n: usize) -> String {
    s.chars().filter(|c| !c.is_control()).take(n).collect()
}
fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}
fn percent(part: f64, whole: f64) -> Value {
    if whole > 0.0 && part >= 0.0 && part <= whole * 1.0001 {
        json!(round1((100.0 * part / whole).min(100.0)))
    } else {
        Value::Null
    }
}

impl Live {
    pub fn new(proc: &Path, sys: &Path) -> Self {
        Live {
            proc: proc.to_path_buf(),
            sys: sys.to_path_buf(),
            pci_ids: vec![
                "/usr/share/hwdata/pci.ids".into(),
                "/usr/share/misc/pci.ids".into(),
            ],
            rgb_port: 6742,
            previous: None,
            processes_before: None,
            live: None,
            processes: None,
            rgb: None,
            names: HashMap::new(),
        }
    }

    /// Everything except processes, at most every 2 seconds. The very first request
    /// takes two samples a moment apart, so loads and rates are never blank.
    pub fn sample(&mut self) -> Value {
        if let Some((at, v)) = &self.live {
            if at.elapsed() < LIVE_EVERY {
                return v.clone();
            }
        }
        if self
            .previous
            .as_ref()
            .is_none_or(|p| p.at.elapsed() > Duration::from_secs(60))
        {
            self.take(false);
            std::thread::sleep(Duration::from_millis(400));
        }
        let value = self.take(true);
        self.live = Some((Instant::now(), value.clone()));
        value
    }

    fn take(&mut self, report: bool) -> Value {
        let now = Instant::now();
        let boot = read(&self.proc.join("sys/kernel/random/boot_id"));
        let previous = self
            .previous
            .take()
            .filter(|p| p.boot == boot && now > p.at);
        let elapsed = previous
            .as_ref()
            .map(|p| now.duration_since(p.at).as_secs_f64())
            .filter(|s| *s > 0.0);
        let mut counters = Counters {
            at: now,
            boot,
            cpu: HashMap::new(),
            net: HashMap::new(),
            disk: HashMap::new(),
            npu: HashMap::new(),
        };
        let cpu = self.cpu(previous.as_ref(), &mut counters);
        let memory = self.memory();
        let (gpus, npus) = self.accelerators(previous.as_ref(), elapsed, &mut counters);
        let network = self.network(previous.as_ref(), elapsed, &mut counters);
        let disks = self.disks(previous.as_ref(), elapsed, &mut counters);
        self.previous = Some(counters);
        if !report {
            return Value::Null;
        }
        json!({
            "format": "neko-live-v1",
            "generated_at": crate::common::now(),
            "cpu": cpu,
            "memory": memory,
            "gpus": gpus,
            "npus": npus,
            "sensors": self.sensors(),
            "power": self.power_supplies(),
            "network": network,
            "disks": disks,
            "rgb": self.rgb(),
        })
    }

    fn cpu(&self, previous: Option<&Counters>, counters: &mut Counters) -> Value {
        for line in read(&self.proc.join("stat")).lines() {
            let mut fields = line.split_whitespace();
            let key = fields.next().unwrap_or("");
            if !key.starts_with("cpu") || !key[3..].bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            // user nice system idle iowait irq softirq steal (guest is inside user/nice)
            let ticks = fields
                .take(8)
                .map(str::parse::<u64>)
                .collect::<Result<Vec<_>, _>>()
                .unwrap_or_default();
            if ticks.len() == 8 {
                counters
                    .cpu
                    .insert(key.to_string(), (ticks.iter().sum(), ticks[3] + ticks[4]));
            }
        }
        let busy = |key: &str| -> Value {
            let (Some(now), Some(before)) =
                (counters.cpu.get(key), previous.and_then(|p| p.cpu.get(key)))
            else {
                return Value::Null;
            };
            let total = now.0.saturating_sub(before.0) as f64;
            let idle = now.1.saturating_sub(before.1) as f64;
            percent(total - idle, total)
        };
        let mut cores = counters
            .cpu
            .keys()
            .filter(|k| k.len() > 3)
            .cloned()
            .collect::<Vec<_>>();
        cores.sort_by(|a, b| natural(a, b));
        let cpuinfo = read(&self.proc.join("cpuinfo"));
        let model = cpuinfo
            .lines()
            .find(|l| l.starts_with("model name") || l.starts_with("Model"))
            .and_then(|l| l.split_once(':'))
            .map(|(_, v)| clean(v.trim(), 100))
            .unwrap_or_else(|| "Processor".into());
        let clocks = cores
            .iter()
            .filter_map(|c| {
                number(
                    &self
                        .sys
                        .join(format!("devices/system/cpu/{c}/cpufreq/scaling_cur_freq")),
                )
            })
            .collect::<Vec<_>>();
        let load = read(&self.proc.join("loadavg"))
            .split_whitespace()
            .take(3)
            .filter_map(|v| v.parse::<f64>().ok())
            .collect::<Vec<_>>();
        json!({
            "name": model,
            "logical_cpus": if cores.is_empty() { Value::Null } else { json!(cores.len()) },
            "percent": busy("cpu"),
            "cores": cores.iter().map(|c| busy(c)).collect::<Vec<_>>(),
            "clock_mhz": if clocks.is_empty() { Value::Null } else {
                json!((clocks.iter().sum::<f64>() / clocks.len() as f64 / 1000.0).round())
            },
            "load": if load.len() == 3 { json!(load) } else { Value::Null },
            "uptime_seconds": read(&self.proc.join("uptime")).split_whitespace().next()
                .and_then(|v| v.parse::<f64>().ok()).map(f64::floor),
        })
    }

    fn memory(&self) -> Value {
        let mut m = HashMap::new();
        for line in read(&self.proc.join("meminfo")).lines() {
            if let Some((k, v)) = line.split_once(':') {
                if let Some(kib) = v
                    .split_whitespace()
                    .next()
                    .and_then(|n| n.parse::<u64>().ok())
                {
                    m.insert(k.to_string(), kib * 1024);
                }
            }
        }
        let used = |total: &str, free: &str| match (m.get(total), m.get(free)) {
            (Some(t), Some(f)) if f <= t => Some(t - f),
            _ => None,
        };
        let ram = used("MemTotal", "MemAvailable");
        let swap = used("SwapTotal", "SwapFree");
        json!({
            "total": m.get("MemTotal"),
            "used": ram,
            "percent": ram.map(|u| percent(u as f64, *m.get("MemTotal").unwrap_or(&0) as f64)),
            "swap_total": m.get("SwapTotal"),
            "swap_used": swap,
        })
    }

    /// A readable name from the system's PCI ID list ("Intel · Alder Lake-S GT1 [UHD
    /// Graphics 730]"), looked up once per device.
    fn pci_name(&mut self, vendor: &str, device: &str) -> Option<String> {
        let key = format!("{vendor}:{device}");
        if let Some(found) = self.names.get(&key) {
            return found.clone();
        }
        let (v, d) = (
            vendor.trim_start_matches("0x").to_lowercase(),
            device.trim_start_matches("0x").to_lowercase(),
        );
        let mut found = None;
        for file in &self.pci_ids {
            let Ok(text) = fs::read_to_string(file) else {
                continue;
            };
            let mut vendor_name = None;
            for line in text.lines() {
                if line.starts_with('#') || line.is_empty() {
                    continue;
                }
                if !line.starts_with('\t') {
                    if vendor_name.is_some() {
                        break;
                    }
                    if line.starts_with(&format!("{v}  ")) {
                        vendor_name = Some(line[v.len() + 2..].to_string());
                    }
                } else if vendor_name.is_some()
                    && !line.starts_with("\t\t")
                    && line[1..].starts_with(&format!("{d}  "))
                {
                    found = Some(clean(&line[d.len() + 3..], 100));
                    break;
                }
            }
            if found.is_some() {
                break;
            }
        }
        self.names.insert(key, found.clone());
        found
    }

    fn accelerators(
        &mut self,
        previous: Option<&Counters>,
        elapsed: Option<f64>,
        counters: &mut Counters,
    ) -> (Vec<Value>, Vec<Value>) {
        let mut seen = vec![];
        let mut found = vec![];
        let drm = children(&self.sys.join("class/drm"))
            .into_iter()
            .filter(|p| {
                let n = name_of(p);
                n.starts_with("card") && n[4..].bytes().all(|b| b.is_ascii_digit())
            })
            .map(|p| (p.join("device"), Some(p), false));
        let accel = children(&self.sys.join("class/accel"))
            .into_iter()
            .map(|p| (p.join("device"), None, true));
        let pci = children(&self.sys.join("bus/pci/devices"))
            .into_iter()
            .filter(|p| read(&p.join("class")).starts_with("0x03"))
            .map(|p| (p, None, false));
        for (device, card, is_accel) in drm.chain(accel).chain(pci) {
            let Ok(real) = fs::canonicalize(&device) else {
                continue;
            };
            if seen.contains(&real) {
                continue;
            }
            seen.push(real.clone());
            found.push((real, card, is_accel));
        }
        let (mut gpus, mut npus) = (vec![], vec![]);
        for (real, card, is_accel) in found {
            let driver = fs::read_link(real.join("driver"))
                .map(|p| name_of(&p))
                .unwrap_or_default();
            let npu = is_accel || ["intel_vpu", "amdxdna"].contains(&driver.as_str());
            let vendor_id = read(&real.join("vendor"));
            let vendor = match vendor_id.as_str() {
                "0x8086" => "Intel",
                "0x1002" => "AMD",
                "0x10de" => "NVIDIA",
                _ => "",
            };
            let model = self.pci_name(&vendor_id, &read(&real.join("device")));
            let name = match (vendor, model) {
                ("", Some(m)) => m,
                (v, Some(m)) => format!("{v} {m}"),
                ("", None) if npu => "AI accelerator".into(),
                ("", None) => "Graphics adapter".into(),
                (v, None) => format!("{v} {}", if npu { "AI accelerator" } else { "graphics" }),
            };
            let id = name_of(&real);
            let mut row = json!({
                "id": id, "name": name, "vendor": vendor,
                "driver": if driver.is_empty() { Value::Null } else { json!(driver) },
                "percent": null, "source": null,
            });
            if npu {
                // Intel NPUs expose a busy-time counter; most others expose nothing.
                if let Some(busy) = number(&real.join("npu_busy_time_us")) {
                    counters.npu.insert(id.clone(), busy);
                    if let (Some(before), Some(secs)) =
                        (previous.and_then(|p| p.npu.get(&id)), elapsed)
                    {
                        row["percent"] = percent((busy - before) / 1e6, secs);
                        row["source"] = json!("Driver busy time");
                    }
                }
                npus.push(row);
                continue;
            }
            if let Some(busy) = number(&real.join("gpu_busy_percent")) {
                row["percent"] = percent(busy, 100.0);
                row["source"] = json!("Driver load");
            }
            row["memory_used"] = json!(number(&real.join("mem_info_vram_used")));
            row["memory_total"] = json!(number(&real.join("mem_info_vram_total")));
            // Intel reports its graphics clock (current and maximum), not a load.
            let clock = |files: &[&str]| {
                files.iter().find_map(|f| {
                    card.as_ref()
                        .and_then(|c| number(&c.join(f)))
                        .or_else(|| number(&real.join(f)))
                })
            };
            row["clock_mhz"] = json!(clock(&["gt_act_freq_mhz", "tile0/gt0/freq0/act_freq"]));
            row["clock_max_mhz"] = json!(clock(&["gt_RP0_freq_mhz", "tile0/gt0/freq0/max_freq"]));
            row["primary"] = json!(read(&real.join("boot_vga")) == "1");
            gpus.push(row);
        }
        gpus.truncate(16);
        npus.truncate(16);
        (gpus, npus)
    }

    /// Every hardware monitoring chip Linux found: temperatures, fans, fan duty,
    /// power and voltages, grouped for people ("Processor", "Graphics", ...).
    pub fn sensors(&self) -> Vec<Value> {
        let mut rows = vec![];
        for chip in children(&self.sys.join("class/hwmon")) {
            let driver = clean(&read(&chip.join("name")), 40);
            if SKIPPED_CHIPS.contains(&driver.as_str()) {
                continue;
            }
            let device = fs::canonicalize(chip.join("device")).ok();
            let model = device
                .as_ref()
                .map(|d| clean(&read(&d.join("model")), 60))
                .unwrap_or_default();
            let group = group_of(&driver);
            let source = if model.is_empty() {
                driver.clone()
            } else {
                format!("{driver} · {model}")
            };
            let mut files = children(&chip)
                .into_iter()
                .map(|p| name_of(&p))
                .collect::<Vec<_>>();
            files.sort_by(|a, b| natural(a, b));
            for file in files {
                let Some((kind, unit, scale, channel)) = reading_kind(&file) else {
                    continue;
                };
                let Some(raw) = number(&chip.join(&file)) else {
                    continue;
                };
                let label = read(&chip.join(format!("{channel}_label")));
                let value = raw / scale;
                let mut row = json!({
                    "id": format!("{}:{}", name_of(&chip), channel),
                    "group": group, "chip": source, "kind": kind,
                    "label": clean(if label.is_empty() { &channel } else { &label }, 60),
                    "value": round1(value), "unit": unit,
                });
                if kind == "temperature" {
                    if !(-40.0..=200.0).contains(&value) {
                        continue;
                    }
                    for limit in ["max", "crit"] {
                        if let Some(v) = number(&chip.join(format!("{channel}_{limit}"))) {
                            if v > 0.0 && v / 1000.0 <= 200.0 {
                                row[limit] = json!(round1(v / 1000.0));
                            }
                        }
                    }
                }
                if kind == "fan_duty" {
                    // 0-255 is the duty the chip drives; automatic or manual is
                    // reported, never changed here.
                    row["value"] = percent(raw, 255.0);
                    row["mode"] = json!(
                        match read(&chip.join(format!("{channel}_enable"))).as_str() {
                            "0" => "full speed",
                            "1" => "manual",
                            "" => "unknown",
                            _ => "automatic",
                        }
                    );
                }
                rows.push(row);
                if rows.len() >= MAX_SENSORS {
                    return rows;
                }
            }
        }
        if !rows.iter().any(|r| r["kind"] == "temperature") {
            // No hwmon temperatures (some ARM boards, VMs): use thermal zones instead.
            for zone in children(&self.sys.join("class/thermal")) {
                if !name_of(&zone).starts_with("thermal_zone") {
                    continue;
                }
                if let Some(v) = number(&zone.join("temp")).map(|t| t / 1000.0) {
                    if (-40.0..=200.0).contains(&v) {
                        let kind = clean(&read(&zone.join("type")), 40);
                        rows.push(json!({"id": name_of(&zone), "group": "System",
                            "chip": kind, "kind": "temperature", "label": kind,
                            "value": round1(v), "unit": "°C"}));
                    }
                }
            }
        }
        rows
    }

    /// Batteries and UPS units (a laptop used as a server, or a USB UPS).
    fn power_supplies(&self) -> Vec<Value> {
        children(&self.sys.join("class/power_supply"))
            .into_iter()
            .filter(|p| ["Battery", "UPS"].contains(&read(&p.join("type")).as_str()))
            .take(8)
            .map(|p| {
                json!({
                    "id": name_of(&p),
                    "name": clean(&[read(&p.join("manufacturer")), read(&p.join("model_name"))]
                        .iter().filter(|s| !s.is_empty()).cloned().collect::<Vec<_>>().join(" "), 80),
                    "kind": read(&p.join("type")),
                    "percent": number(&p.join("capacity")).filter(|v| (0.0..=100.0).contains(v)),
                    "status": clean(&read(&p.join("status")), 30),
                })
            })
            .collect()
    }

    /// Wired, Wi-Fi and Tailscale traffic. Docker's virtual links are left out: the
    /// same bytes already pass the real network card.
    fn network(
        &self,
        previous: Option<&Counters>,
        elapsed: Option<f64>,
        counters: &mut Counters,
    ) -> Vec<Value> {
        let mut rows = vec![];
        for line in read(&self.proc.join("net/dev")).lines().skip(2) {
            let Some((name, rest)) = line.split_once(':') else {
                continue;
            };
            let name = name.trim();
            let virtual_link = ["lo", "docker", "veth", "br-", "virbr", "vnet"]
                .iter()
                .any(|p| name.starts_with(p));
            let device = self.sys.join("class/net").join(name).join("device");
            let tunnel = name.starts_with("tailscale") || name.starts_with("wg");
            if virtual_link || !(device.exists() || tunnel) {
                continue;
            }
            let f = rest
                .split_whitespace()
                .filter_map(|v| v.parse::<u64>().ok())
                .collect::<Vec<_>>();
            if f.len() < 16 {
                continue;
            }
            let (rx, tx) = (f[0], f[8]);
            counters.net.insert(name.to_string(), (rx, tx));
            let rate = |now: u64, pick: fn(&(u64, u64)) -> u64| match (
                previous.and_then(|p| p.net.get(name)),
                elapsed,
            ) {
                (Some(b), Some(s)) if now >= pick(b) => json!(((now - pick(b)) as f64 / s).round()),
                _ => Value::Null,
            };
            let base = self.sys.join("class/net").join(name);
            let speed = number(&base.join("speed")).filter(|v| *v > 0.0);
            rows.push(json!({
                "id": name,
                "kind": if tunnel { "tailscale" } else if base.join("wireless").exists() { "wifi" } else { "wired" },
                "up": read(&base.join("operstate")) == "up" || tunnel,
                "speed_mbps": speed,
                "receive_bps": rate(rx, |b| b.0),
                "send_bps": rate(tx, |b| b.1),
                "received": rx, "sent": tx,
            }));
        }
        rows.truncate(16);
        rows
    }

    /// Whole physical disks (not partitions, loop devices or RAM disks).
    fn disks(
        &self,
        previous: Option<&Counters>,
        elapsed: Option<f64>,
        counters: &mut Counters,
    ) -> Vec<Value> {
        let mut rows = vec![];
        for line in read(&self.proc.join("diskstats")).lines() {
            let f = line.split_whitespace().collect::<Vec<_>>();
            if f.len() < 14 {
                continue;
            }
            let name = f[2];
            let block = self.sys.join("block").join(name);
            if ["loop", "ram", "zram", "dm-", "md", "sr", "fd"]
                .iter()
                .any(|p| name.starts_with(p))
                || !block.join("device").exists()
            {
                continue;
            }
            let n = |i: usize| f[i].parse::<u64>().unwrap_or(0);
            let (read_bytes, written, busy_ms) = (n(5) * 512, n(9) * 512, n(12));
            counters
                .disk
                .insert(name.to_string(), (read_bytes, written, busy_ms));
            let before = previous.and_then(|p| p.disk.get(name));
            let rate = |now: u64, then: Option<u64>| match (then, elapsed) {
                (Some(b), Some(s)) if now >= b => json!(((now - b) as f64 / s).round()),
                _ => Value::Null,
            };
            let rotational = read(&block.join("queue/rotational")) == "1";
            rows.push(json!({
                "id": name,
                "model": clean(&read(&block.join("device/model")), 60),
                "kind": if name.starts_with("nvme") { "nvme" } else if rotational { "hdd" } else { "ssd" },
                "size": number(&block.join("size")).map(|s| s * 512.0),
                "read_bps": rate(read_bytes, before.map(|b| b.0)),
                "write_bps": rate(written, before.map(|b| b.1)),
                "busy_percent": match (before, elapsed) {
                    (Some(b), Some(s)) if busy_ms >= b.2 => percent((busy_ms - b.2) as f64 / 1000.0, s),
                    _ => Value::Null,
                },
            }));
        }
        rows.truncate(32);
        rows
    }

    /// Visible processes with their sampled CPU load and memory. Names only: command
    /// lines, environments and users are never read. `apps` maps a container ID
    /// prefix to the NekoServer app it belongs to.
    pub fn processes(&mut self, apps: &HashMap<String, String>) -> Value {
        if let Some((at, v)) = &self.processes {
            if at.elapsed() < PROCESSES_EVERY {
                return v.clone();
            }
        }
        if self
            .processes_before
            .as_ref()
            .is_none_or(|p| p.0.elapsed() > Duration::from_secs(60))
        {
            self.scan(apps);
            std::thread::sleep(Duration::from_millis(400));
        }
        let value = self.scan(apps);
        self.processes = Some((Instant::now(), value.clone()));
        value
    }

    fn scan(&mut self, apps: &HashMap<String, String>) -> Value {
        let now = Instant::now();
        let boot = read(&self.proc.join("sys/kernel/random/boot_id"));
        let before = self.processes_before.take().filter(|(_, b, _)| *b == boot);
        let elapsed = before
            .as_ref()
            .map(|(at, _, _)| now.duration_since(*at).as_secs_f64())
            .filter(|s| *s > 0.0);
        // SAFETY: sysconf only reads system constants.
        let (hz, page) = unsafe {
            (
                libc::sysconf(libc::_SC_CLK_TCK).max(1) as f64,
                libc::sysconf(libc::_SC_PAGESIZE).max(1) as u64,
            )
        };
        let (mut rows, mut ticks, mut skipped, mut truncated) = (vec![], HashMap::new(), 0, false);
        let mut pids = fs::read_dir(&self.proc)
            .map(|r| {
                r.flatten()
                    .filter_map(|e| e.file_name().to_str()?.parse::<u32>().ok())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        pids.sort_unstable();
        for pid in pids {
            if rows.len() >= MAX_PROCESSES {
                truncated = true;
                break;
            }
            let stat = read(&self.proc.join(format!("{pid}/stat")));
            // The name can hold spaces and ')': the last ')' ends it.
            let (Some(open), Some(close)) = (stat.find('('), stat.rfind(')')) else {
                skipped += 1;
                continue;
            };
            let name = clean(&stat[open + 1..close], 60);
            let f = stat[close + 1..].split_whitespace().collect::<Vec<_>>();
            if f.len() < 22 {
                skipped += 1;
                continue;
            }
            let n = |i: usize| f[i].parse::<u64>().unwrap_or(0);
            let (used, start) = (n(11) + n(12), n(19));
            ticks.insert(pid, (used, start));
            let cpu = match (before.as_ref().and_then(|b| b.2.get(&pid)), elapsed) {
                (Some(&(old, old_start)), Some(s)) if old_start == start && used >= old => {
                    json!(round1((used - old) as f64 / hz / s * 100.0))
                }
                _ => Value::Null,
            };
            let cgroup = read(&self.proc.join(format!("{pid}/cgroup")));
            let app = container_of(&cgroup).and_then(|id| apps.get(&id[..12]).cloned());
            rows.push(json!({
                "pid": pid, "name": name, "state": f[0], "cpu": cpu,
                "memory": n(21) * page, "threads": n(17), "app": app,
            }));
        }
        let count = rows.len();
        self.processes_before = Some((now, boot, ticks));
        json!({"format": "neko-processes-v1", "generated_at": crate::common::now(),
            "rows": rows, "count": count, "skipped": skipped, "truncated": truncated})
    }

    /// RGB devices from an OpenRGB SDK server that is already running on this
    /// computer (read-only: names and effects; nothing is scanned or changed).
    fn rgb(&mut self) -> Value {
        if let Some((at, v)) = &self.rgb {
            if at.elapsed() < RGB_EVERY {
                return v.clone();
            }
        }
        let value = match openrgb_devices(self.rgb_port) {
            Ok(devices) if devices.is_empty() => json!({"status": "none_reported", "devices": []}),
            Ok(devices) => json!({"status": "detected", "devices": devices}),
            Err(_) => json!({"status": "not_running", "devices": []}),
        };
        self.rgb = Some((Instant::now(), value.clone()));
        value
    }
}

/// People-friendly groups for hwmon chips.
fn group_of(driver: &str) -> &'static str {
    match driver {
        "coretemp" | "k10temp" | "zenpower" | "cpu_thermal" => "Processor",
        "amdgpu" | "radeon" | "nouveau" | "i915" | "xe" | "nvidia" => "Graphics",
        "nvme" | "drivetemp" => "Drives",
        "acpitz" | "pch_cannonlake" | "pch_skylake" | "pch_cometlake" => "System",
        "iwlwifi_1" | "iwlwifi" | "mt7921_phy0" | "ath11k_hwmon" => "Wi-Fi",
        d if d.starts_with("nct")
            || d.starts_with("it8")
            || d.starts_with("asus")
            || d.starts_with("gigabyte")
            || d.starts_with("dell")
            || d.starts_with("thinkpad")
            || d.starts_with("f71") =>
        {
            "Motherboard"
        }
        d if d.starts_with("iwlwifi") || d.starts_with("mt79") || d.starts_with("ath") => "Wi-Fi",
        d if d.starts_with("corsair") || d.starts_with("nzxt") || d.contains("aquacomputer") => {
            "Cooling"
        }
        _ => "Other",
    }
}

/// (kind, unit, divisor, channel) for a hwmon reading file.
fn reading_kind(file: &str) -> Option<(&'static str, &'static str, f64, String)> {
    let channel = |prefix: &str, suffix: &str| {
        file.strip_prefix(prefix)?
            .strip_suffix(suffix)
            .filter(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
            .map(|n| format!("{prefix}{n}"))
    };
    if let Some(c) = channel("temp", "_input") {
        return Some(("temperature", "°C", 1000.0, c));
    }
    if let Some(c) = channel("fan", "_input") {
        return Some(("fan", "RPM", 1.0, c));
    }
    if let Some(c) = channel("pwm", "") {
        return Some(("fan_duty", "%", 1.0, c));
    }
    if let Some(c) = channel("power", "_average").or_else(|| channel("power", "_input")) {
        return Some(("power", "W", 1e6, c));
    }
    if let Some(c) = channel("in", "_input") {
        return Some(("voltage", "V", 1000.0, c));
    }
    None
}

/// The Docker container a process runs in, from its cgroup path.
fn container_of(cgroup: &str) -> Option<String> {
    cgroup.lines().find_map(|line| {
        let path = line.rsplit(':').next()?;
        path.split('/').find_map(|part| {
            let id = part
                .strip_prefix("docker-")
                .and_then(|p| p.strip_suffix(".scope"))
                .or_else(|| (part.len() == 64 && path.contains("/docker/")).then_some(part))?;
            (id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit())).then(|| id.to_string())
        })
    })
}

/// OpenRGB SDK (protocol 0, read-only requests only).
fn openrgb_devices(port: u16) -> std::io::Result<Vec<Value>> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(300))?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    stream.set_write_timeout(Some(Duration::from_secs(1)))?;
    let request = |stream: &mut TcpStream, device: u32, id: u32| -> std::io::Result<Vec<u8>> {
        let mut head = b"ORGB".to_vec();
        for n in [device, id, 0] {
            head.extend_from_slice(&n.to_le_bytes());
        }
        stream.write_all(&head)?;
        let mut reply = [0u8; 16];
        stream.read_exact(&mut reply)?;
        let size = u32::from_le_bytes(reply[12..16].try_into().unwrap()) as usize;
        if &reply[..4] != b"ORGB" || size > 4_000_000 {
            return Err(std::io::Error::other("Not an OpenRGB server"));
        }
        let mut body = vec![0; size];
        stream.read_exact(&mut body)?;
        Ok(body)
    };
    let count = request(&mut stream, 0, 0)?;
    let count = u32::from_le_bytes(
        count
            .get(..4)
            .ok_or(std::io::ErrorKind::InvalidData)?
            .try_into()
            .unwrap(),
    );
    let mut devices = vec![];
    for index in 0..count.min(32) {
        let body = request(&mut stream, index, 1)?;
        if let Some(device) = parse_openrgb_device(index, &body) {
            devices.push(device);
        }
    }
    Ok(devices)
}

/// Name, description and effect names from an OpenRGB controller description.
fn parse_openrgb_device(index: u32, body: &[u8]) -> Option<Value> {
    let mut at = 8; // data size (u32), device type (i32)
    let text = |at: &mut usize| -> Option<String> {
        let len = u16::from_le_bytes(body.get(*at..*at + 2)?.try_into().ok()?) as usize;
        let raw = body.get(*at + 2..*at + 2 + len)?;
        *at += 2 + len;
        Some(clean(&String::from_utf8_lossy(raw).replace('\0', ""), 80))
    };
    let kind = i32::from_le_bytes(body.get(4..8)?.try_into().ok()?);
    let name = text(&mut at)?;
    let description = text(&mut at)?;
    for _ in 0..3 {
        text(&mut at)?; // version, serial, location
    }
    let modes = u16::from_le_bytes(body.get(at..at + 2)?.try_into().ok()?) as usize;
    at += 2 + 4; // active mode (i32)
    let mut effects = vec![];
    for _ in 0..modes.min(64) {
        effects.push(text(&mut at)?);
        at += 4 * 9; // value, flags, speed min/max, colors min/max, speed, direction, color mode
        let colors = u16::from_le_bytes(body.get(at..at + 2)?.try_into().ok()?) as usize;
        at += 2 + 4 * colors;
    }
    let kinds = [
        "Motherboard",
        "Memory",
        "Graphics card",
        "Cooler",
        "LED strip",
        "Keyboard",
        "Mouse",
        "Mouse mat",
        "Headset",
        "Headset stand",
        "Gamepad",
        "Light",
        "Speaker",
        "Virtual",
        "Storage",
        "Case",
        "Microphone",
        "Accessory",
        "Keypad",
    ];
    Some(json!({
        "id": index.to_string(), "name": name, "description": description,
        "kind": kinds.get(kind as usize).copied().unwrap_or("Lighting"),
        "effects": effects,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Tree(PathBuf);
    impl Tree {
        fn new(name: &str) -> Self {
            let root =
                std::env::temp_dir().join(format!("neko-live-{name}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(&root).unwrap();
            Tree(root)
        }
        fn put(&self, rel: &str, text: &str) {
            let path = self.0.join(rel);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, text).unwrap();
        }
        fn link(&self, rel: &str, target: &str) {
            let path = self.0.join(rel);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::os::unix::fs::symlink(self.0.join(target), path).unwrap();
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn machine(t: &Tree, user: u64, rx: u64, sectors: u64) {
        t.put("proc/sys/kernel/random/boot_id", "boot-1");
        t.put("proc/stat", &format!(
            "cpu  {user} 0 100 1000 0 0 0 0 0 0\ncpu0 {user} 0 50 500 0 0 0 0 0 0\ncpu1 0 0 50 500 0 0 0 0 0 0\nintr 1\n"));
        t.put(
            "proc/meminfo",
            "MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 100 kB\nSwapFree: 100 kB\n",
        );
        t.put(
            "proc/cpuinfo",
            "processor : 0\nmodel name : Example CPU 9000\n",
        );
        t.put("proc/loadavg", "0.50 0.40 0.30 1/100 42\n");
        t.put("proc/uptime", "3600.5 100.0\n");
        t.put("proc/net/dev", &format!(
            "Inter-|\n face |\n    lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n  eno1: {rx} 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\nveth1: 7 0 0 0 0 0 0 0 7 0 0 0 0 0 0 0\n"));
        t.put("proc/diskstats", &format!(
            "   8 0 sda 1 0 {sectors} 0 1 0 8 0 0 {busy} 0 0 0 0\n   8 1 sda1 1 0 {sectors} 0 1 0 8 0 0 10 0 0 0 0\n   7 0 loop0 1 0 9 0 1 0 8 0 0 10 0 0 0 0\n", busy = sectors));
    }

    #[test]
    fn readings_come_from_two_samples_and_skip_virtual_devices() {
        let t = Tree::new("sample");
        machine(&t, 100, 1000, 0);
        t.put("sys/class/net/eno1/device/x", "");
        t.put("sys/class/net/eno1/operstate", "up");
        t.put("sys/class/net/eno1/speed", "1000");
        t.put("sys/block/sda/device/model", "Example SSD");
        t.put("sys/block/sda/queue/rotational", "0");
        t.put("sys/block/sda/size", "2000");
        t.put(
            "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
            "3000000",
        );
        t.put(
            "sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq",
            "1000000",
        );
        let mut live = Live::new(&t.0.join("proc"), &t.0.join("sys"));
        live.rgb_port = 1; // nothing listens there
        live.take(false);
        machine(&t, 200, 3000, 1000);
        let v = live.take(true);
        assert_eq!(v["cpu"]["name"], "Example CPU 9000");
        assert_eq!(v["cpu"]["logical_cpus"], 2);
        assert_eq!(v["cpu"]["percent"], 100.0); // only user time grew
        assert_eq!(v["cpu"]["cores"], json!([100.0, null]));
        assert_eq!(v["cpu"]["clock_mhz"], 2000.0);
        assert_eq!(v["cpu"]["load"], json!([0.5, 0.4, 0.3]));
        assert_eq!(v["memory"]["used"], 750 * 1024);
        assert_eq!(v["memory"]["percent"], 75.0);
        let net = v["network"].as_array().unwrap();
        assert_eq!(net.len(), 1, "loopback and veth are left out: {net:?}");
        assert_eq!(net[0]["id"], "eno1");
        assert!(net[0]["receive_bps"].as_f64().unwrap() > 0.0);
        let disks = v["disks"].as_array().unwrap();
        assert_eq!(disks.len(), 1, "partitions and loop devices are left out");
        assert_eq!(disks[0]["model"], "Example SSD");
        assert_eq!(disks[0]["size"], 1_024_000.0);
        assert!(disks[0]["read_bps"].as_f64().unwrap() > 0.0);
        assert_eq!(v["rgb"]["status"], "not_running");
    }

    #[test]
    fn a_new_boot_never_mixes_counters() {
        let t = Tree::new("boot");
        machine(&t, 100, 1000, 0);
        let mut live = Live::new(&t.0.join("proc"), &t.0.join("sys"));
        live.rgb_port = 1;
        live.take(false);
        t.put("proc/sys/kernel/random/boot_id", "boot-2");
        let v = live.take(true);
        assert!(v["cpu"]["percent"].is_null());
    }

    #[test]
    fn every_sensor_kind_is_found_and_grouped() {
        let t = Tree::new("sensors");
        t.put("sys/class/hwmon/hwmon0/name", "coretemp");
        t.put("sys/class/hwmon/hwmon0/temp1_input", "45000");
        t.put("sys/class/hwmon/hwmon0/temp1_label", "Package id 0");
        t.put("sys/class/hwmon/hwmon0/temp1_max", "80000");
        t.put("sys/class/hwmon/hwmon0/temp1_crit", "100000");
        t.put("sys/class/hwmon/hwmon1/name", "nct6798");
        t.put("sys/class/hwmon/hwmon1/fan2_input", "1200");
        t.put("sys/class/hwmon/hwmon1/pwm2", "128");
        t.put("sys/class/hwmon/hwmon1/pwm2_enable", "5");
        t.put("sys/class/hwmon/hwmon1/in0_input", "1104");
        t.put("sys/class/hwmon/hwmon1/temp9_input", "-273000"); // unplugged probe
        t.put("sys/class/hwmon/hwmon3/name", "drivetemp");
        t.put("sys/class/hwmon/hwmon3/temp1_input", "41000");
        t.put("sys/class/hwmon/hwmon10/name", "amdgpu");
        t.put("sys/class/hwmon/hwmon10/power1_average", "35000000");
        t.put("sys/devices/nvme0/model", "Example NVMe 1TB");
        t.put("sys/class/hwmon/hwmon2/name", "nvme");
        t.put("sys/class/hwmon/hwmon2/temp1_input", "38850");
        t.link("sys/class/hwmon/hwmon2/device", "sys/devices/nvme0");
        let rows = Live::new(&t.0.join("proc"), &t.0.join("sys")).sensors();
        let find = |id: &str| {
            rows.iter()
                .find(|r| r["id"] == id)
                .unwrap_or_else(|| panic!("{id}: {rows:?}"))
        };
        let cpu = find("hwmon0:temp1");
        assert_eq!(
            (cpu["group"].as_str(), cpu["label"].as_str()),
            (Some("Processor"), Some("Package id 0"))
        );
        assert_eq!(
            (
                cpu["value"].as_f64(),
                cpu["max"].as_f64(),
                cpu["crit"].as_f64()
            ),
            (Some(45.0), Some(80.0), Some(100.0))
        );
        assert_eq!(find("hwmon1:fan2")["value"], 1200.0);
        assert_eq!(find("hwmon1:pwm2")["value"], 50.2);
        assert_eq!(find("hwmon1:pwm2")["mode"], "automatic");
        assert_eq!(find("hwmon1:in0")["value"], 1.1);
        assert_eq!(find("hwmon1:in0")["group"], "Motherboard");
        assert!(!rows.iter().any(|r| r["id"] == "hwmon1:temp9"));
        assert_eq!(find("hwmon10:power1")["value"], 35.0);
        assert_eq!(find("hwmon2:temp1")["chip"], "nvme · Example NVMe 1TB");
        assert!(
            !rows.iter().any(|r| r["id"] == "hwmon3:temp1"),
            "drive temperatures that can wake a hard drive are not read live"
        );
        // hwmon2 is listed before hwmon10.
        let order = rows
            .iter()
            .map(|r| {
                r["id"]
                    .as_str()
                    .unwrap()
                    .split(':')
                    .next()
                    .unwrap()
                    .to_string()
            })
            .collect::<Vec<_>>();
        assert!(
            order.iter().position(|x| x == "hwmon2") < order.iter().position(|x| x == "hwmon10")
        );
    }

    #[test]
    fn thermal_zones_stand_in_when_no_chip_reports_a_temperature() {
        let t = Tree::new("thermal");
        t.put("sys/class/thermal/thermal_zone0/type", "x86_pkg_temp");
        t.put("sys/class/thermal/thermal_zone0/temp", "51000");
        let rows = Live::new(&t.0.join("proc"), &t.0.join("sys")).sensors();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["value"], 51.0);
    }

    #[test]
    fn graphics_and_ai_accelerators_are_named_from_the_pci_list() {
        let t = Tree::new("gpu");
        t.put("pci.ids", "# comment\n8086  Intel Corporation\n\t4680  AlderLake-S GT1 [UHD Graphics 770]\n\t\t1043 8694  subsystem\n\t7d1d  Meteor Lake NPU\n1002  Advanced Micro Devices, Inc. [AMD/ATI]\n\t744c  Navi 31 [Radeon RX 7900 XT/7900 XTX]\n");
        for (slot, vendor, device, class) in [
            ("0000:00:02.0", "0x8086", "0x4680", "0x030000"),
            ("0000:03:00.0", "0x1002", "0x744c", "0x030000"),
            ("0000:00:0b.0", "0x8086", "0x7d1d", "0x120000"),
        ] {
            t.put(&format!("sys/devices/pci0000:00/{slot}/vendor"), vendor);
            t.put(&format!("sys/devices/pci0000:00/{slot}/device"), device);
            t.put(&format!("sys/devices/pci0000:00/{slot}/class"), class);
            t.link(
                &format!("sys/bus/pci/devices/{slot}"),
                &format!("sys/devices/pci0000:00/{slot}"),
            );
        }
        t.put("sys/devices/pci0000:00/0000:03:00.0/gpu_busy_percent", "37");
        t.put(
            "sys/devices/pci0000:00/0000:03:00.0/mem_info_vram_used",
            "1073741824",
        );
        t.put("sys/devices/pci0000:00/0000:00:02.0/boot_vga", "1");
        t.put("sys/class/drm/card0/gt_act_freq_mhz", "350");
        t.put("sys/class/drm/card0/gt_RP0_freq_mhz", "1550");
        t.link(
            "sys/class/drm/card0/device",
            "sys/devices/pci0000:00/0000:00:02.0",
        );
        t.put("sys/drivers/intel_vpu/x", "");
        t.link(
            "sys/devices/pci0000:00/0000:00:0b.0/driver",
            "sys/drivers/intel_vpu",
        );
        t.put(
            "sys/devices/pci0000:00/0000:00:0b.0/npu_busy_time_us",
            "1000000",
        );
        t.put("sys/devices/accel0/x", "");
        t.link(
            "sys/class/accel/accel0/device",
            "sys/devices/pci0000:00/0000:00:0b.0",
        );
        let mut live = Live::new(&t.0.join("proc"), &t.0.join("sys"));
        live.pci_ids = vec![t.0.join("pci.ids")];
        let mut counters = Counters {
            at: Instant::now(),
            boot: String::new(),
            cpu: HashMap::new(),
            net: HashMap::new(),
            disk: HashMap::new(),
            npu: HashMap::new(),
        };
        let (_, first) = live.accelerators(None, None, &mut counters);
        assert!(first[0]["percent"].is_null(), "one sample is not a load");
        t.put(
            "sys/devices/pci0000:00/0000:00:0b.0/npu_busy_time_us",
            "1500000",
        );
        let mut next = Counters {
            at: Instant::now(),
            boot: String::new(),
            cpu: HashMap::new(),
            net: HashMap::new(),
            disk: HashMap::new(),
            npu: HashMap::new(),
        };
        let (gpus, npus) = live.accelerators(Some(&counters), Some(1.0), &mut next);
        assert_eq!(gpus.len(), 2, "{gpus:?}");
        let intel = gpus.iter().find(|g| g["vendor"] == "Intel").unwrap();
        assert_eq!(intel["name"], "Intel AlderLake-S GT1 [UHD Graphics 770]");
        assert_eq!(
            (intel["clock_mhz"].as_f64(), intel["clock_max_mhz"].as_f64()),
            (Some(350.0), Some(1550.0))
        );
        assert!(intel["percent"].is_null() && intel["primary"] == true);
        let amd = gpus.iter().find(|g| g["vendor"] == "AMD").unwrap();
        assert_eq!(amd["name"], "AMD Navi 31 [Radeon RX 7900 XT/7900 XTX]");
        assert_eq!(
            (amd["percent"].as_f64(), amd["memory_used"].as_f64()),
            (Some(37.0), Some(1073741824.0))
        );
        assert_eq!(npus.len(), 1);
        assert_eq!(npus[0]["name"], "Intel Meteor Lake NPU");
        assert_eq!(npus[0]["percent"], 50.0);
    }

    #[test]
    fn processes_are_sampled_and_matched_to_their_app() {
        let t = Tree::new("proc");
        let id = "a".repeat(64);
        let stat = |ticks: u64| {
            format!("42 (ffmpeg (x)) R 1 1 1 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 7 0 500 0 25 0 0")
        };
        t.put("proc/sys/kernel/random/boot_id", "b");
        t.put("proc/42/stat", &stat(100));
        t.put(
            "proc/42/cgroup",
            &format!("0::/system.slice/docker-{id}.scope\n"),
        );
        t.put("proc/43/stat", "garbage");
        let mut live = Live::new(&t.0.join("proc"), &t.0.join("sys"));
        let apps = HashMap::from([("a".repeat(12), "jellyfin".to_string())]);
        live.scan(&apps);
        t.put("proc/42/stat", &stat(200));
        let v = live.scan(&apps);
        let row = &v["rows"][0];
        assert_eq!(row["name"], "ffmpeg (x)");
        assert_eq!(row["app"], "jellyfin");
        assert_eq!(row["threads"], 7);
        assert!(row["cpu"].as_f64().unwrap() > 0.0);
        assert_eq!(v["skipped"], 1);
        assert_eq!(
            container_of(&("0::/docker/".to_owned() + &id)),
            Some(id.clone())
        );
        assert_eq!(container_of("0::/user.slice/user-1000.slice"), None);
    }

    #[test]
    fn an_openrgb_description_gives_name_and_effects() {
        let mut body = vec![0u8; 4];
        body.extend_from_slice(&4i32.to_le_bytes()); // LED strip
        let text = |s: &str| {
            let mut b = ((s.len() + 1) as u16).to_le_bytes().to_vec();
            b.extend_from_slice(s.as_bytes());
            b.push(0);
            b
        };
        for s in ["Strip", "Desk strip", "1.0", "", "HID"] {
            let t = text(s);
            body.extend(t);
        }
        body.extend_from_slice(&2u16.to_le_bytes());
        body.extend_from_slice(&0i32.to_le_bytes());
        for (name, colors) in [("Static", 1u16), ("Rainbow", 0)] {
            let t = text(name);
            body.extend(t);
            body.extend(vec![0u8; 36]);
            body.extend_from_slice(&colors.to_le_bytes());
            body.extend(vec![0u8; 4 * colors as usize]);
        }
        let v = parse_openrgb_device(3, &body).unwrap();
        assert_eq!(v["name"], "Strip");
        assert_eq!(v["kind"], "LED strip");
        assert_eq!(v["effects"], json!(["Static", "Rainbow"]));
        assert!(parse_openrgb_device(0, &body[..20]).is_none());
    }
}
