//! OpenRGB SDK client: list lighting devices, set their colour and effect.
//!
//! OpenRGB (installed by setup and run as a local service) does the hardware work for
//! hundreds of devices; NekoServer speaks its network protocol on 127.0.0.1:6742.
//! That address is inside a private network OpenRGB shares with the control service
//! only (modules/dashboard.py), so no other program on the computer can reach it.
//! Protocol 0 is used: every OpenRGB server understands it. Little-endian throughout.
use serde_json::{json, Value};
use std::{
    io::{self, Read, Write},
    net::{SocketAddr, TcpStream},
    time::Duration,
};

pub const PORT: u16 = 6742;
const REQUEST_CONTROLLER_COUNT: u32 = 0;
const REQUEST_CONTROLLER_DATA: u32 = 1;
const SET_CLIENT_NAME: u32 = 50;
const UPDATE_LEDS: u32 = 1050;
const SET_CUSTOM_MODE: u32 = 1100;
const UPDATE_MODE: u32 = 1101;
/// A mode that can take colours chosen for the whole effect (Breathing in red, say).
const MODE_SPECIFIC_COLORS: u32 = 2;
const DEVICE_KINDS: [&str; 19] = [
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

/// One lighting effect of a device, as OpenRGB describes it.
#[derive(Clone, Debug, PartialEq)]
pub struct Mode {
    pub name: String,
    value: i32,
    flags: u32,
    speed_min: u32,
    speed_max: u32,
    colors_min: u32,
    colors_max: u32,
    speed: u32,
    direction: u32,
    color_mode: u32,
    colors: Vec<[u8; 3]>,
}
impl Mode {
    /// A mode that shows LEDs' own colours (where a steady colour is set), not an
    /// animation.
    pub fn is_steady(&self) -> bool {
        const MODE_COLORS_PER_LED: u32 = 1;
        ["direct", "custom", "static"].contains(&self.name.to_lowercase().as_str())
            || self.color_mode == MODE_COLORS_PER_LED
    }
    /// The colours this effect runs with (empty when it makes its own).
    pub fn effect_colors(&self) -> &[[u8; 3]] {
        &self.colors
    }
    /// Does this effect take colours chosen by the user?
    pub fn takes_color(&self) -> bool {
        self.colors_max > 0
    }
    fn write(&self, out: &mut Vec<u8>) {
        put_text(out, &self.name);
        out.extend_from_slice(&self.value.to_le_bytes());
        for n in [
            self.flags,
            self.speed_min,
            self.speed_max,
            self.colors_min,
            self.colors_max,
            self.speed,
            self.direction,
            self.color_mode,
        ] {
            out.extend_from_slice(&n.to_le_bytes());
        }
        out.extend_from_slice(&(self.colors.len() as u16).to_le_bytes());
        for c in &self.colors {
            out.extend_from_slice(&[c[0], c[1], c[2], 0]);
        }
    }
}

#[derive(Clone, Debug)]
pub struct Device {
    pub index: u32,
    pub name: String,
    pub description: String,
    /// Where it is connected (for example an i2c address); tells identical devices apart.
    pub location: String,
    pub serial: String,
    pub kind: &'static str,
    pub modes: Vec<Mode>,
    pub active_mode: i32,
    pub leds: usize,
    pub colors: Vec<[u8; 3]>,
}
impl Device {
    pub fn to_json(&self) -> Value {
        let first = self.colors.first().copied().unwrap_or([0, 0, 0]);
        json!({
            "id": self.index.to_string(), "name": self.name, "description": self.description,
            "location": self.location,
            "kind": self.kind, "leds": self.leds,
            "effects": self.modes.iter().map(|m| json!({"name": m.name, "takes_color": m.takes_color()})).collect::<Vec<_>>(),
            "effect": self.modes.get(self.active_mode.max(0) as usize).map(|m| m.name.clone()),
            "color": format!("#{:02x}{:02x}{:02x}", first[0], first[1], first[2]),
        })
    }
}

/// Reads the fields of a controller description in order, never past its end.
struct Reader<'a> {
    data: &'a [u8],
    at: usize,
}
impl Reader<'_> {
    fn take(&mut self, n: usize) -> Option<&[u8]> {
        let slice = self.data.get(self.at..self.at.checked_add(n)?)?;
        self.at += n;
        Some(slice)
    }
    fn u16(&mut self) -> Option<u16> {
        Some(u16::from_le_bytes(self.take(2)?.try_into().ok()?))
    }
    fn u32(&mut self) -> Option<u32> {
        Some(u32::from_le_bytes(self.take(4)?.try_into().ok()?))
    }
    fn i32(&mut self) -> Option<i32> {
        Some(i32::from_le_bytes(self.take(4)?.try_into().ok()?))
    }
    /// A length-prefixed string; the length includes its terminating zero.
    fn text(&mut self) -> Option<String> {
        let len = self.u16()? as usize;
        let raw = self.take(len)?;
        let clean = String::from_utf8_lossy(raw).replace('\0', "");
        Some(
            clean
                .chars()
                .filter(|c| !c.is_control())
                .take(100)
                .collect(),
        )
    }
    fn colors(&mut self, n: usize) -> Option<Vec<[u8; 3]>> {
        (0..n)
            .map(|_| self.take(4).map(|c| [c[0], c[1], c[2]]))
            .collect()
    }
    fn mode(&mut self) -> Option<Mode> {
        let name = self.text()?;
        let value = self.i32()?;
        let mut n = [0u32; 8];
        for slot in &mut n {
            *slot = self.u32()?;
        }
        let count = self.u16()? as usize;
        Some(Mode {
            name,
            value,
            flags: n[0],
            speed_min: n[1],
            speed_max: n[2],
            colors_min: n[3],
            colors_max: n[4],
            speed: n[5],
            direction: n[6],
            color_mode: n[7],
            colors: self.colors(count)?,
        })
    }
}
fn put_text(out: &mut Vec<u8>, text: &str) {
    out.extend_from_slice(&((text.len() + 1) as u16).to_le_bytes());
    out.extend_from_slice(text.as_bytes());
    out.push(0);
}

/// A controller description (REQUEST_CONTROLLER_DATA reply, protocol 0).
pub fn parse_device(index: u32, body: &[u8]) -> Option<Device> {
    let mut r = Reader { data: body, at: 4 }; // data size
    let kind = r.i32()?;
    let name = r.text()?;
    let description = r.text()?;
    r.text()?; // version
    let serial = r.text()?;
    let location = r.text()?;
    let mode_count = r.u16()? as usize;
    let active_mode = r.i32()?;
    let modes = (0..mode_count.min(256))
        .map(|_| r.mode())
        .collect::<Option<Vec<_>>>()?;
    for _ in 0..r.u16()? {
        // zone: name, type, leds min/max/count, matrix (its length includes its size)
        r.text()?;
        r.take(4 * 4)?;
        let matrix = r.u16()? as usize;
        r.take(matrix)?;
    }
    let leds = r.u16()? as usize;
    for _ in 0..leds {
        r.text()?;
        r.u32()?;
    }
    let count = r.u16()? as usize;
    let colors = r.colors(count)?;
    Some(Device {
        index,
        name,
        description,
        location,
        serial,
        kind: DEVICE_KINDS
            .get(kind.max(0) as usize)
            .copied()
            .unwrap_or("Lighting"),
        modes,
        active_mode,
        leds: colors.len().max(leds),
        colors,
    })
}

/// The name lighting choices are kept under in 0.27.0, before identical devices were
/// told apart.
pub fn legacy_identity(d: &Device) -> String {
    format!("{} · {}", d.name, d.description)
}
/// A serial number that really tells devices apart: not empty, not a placeholder
/// ("0000", "Default string"…), and not shared with another device.
/// `same_name_only`: count a clash only between devices with the same name (0.27.3 on);
/// 0.27.2 counted it between any devices.
fn serial_checked<'a>(d: &'a Device, all: &[Device], same_name_only: bool) -> Option<&'a str> {
    let serial = d.serial.trim();
    let placeholder = serial.len() < 4
        || serial
            .chars()
            .all(|c| c == serial.chars().next().unwrap_or('0'))
        || [
            "default string",
            "to be filled by o.e.m.",
            "none",
            "n/a",
            "unknown",
        ]
        .contains(&serial.to_lowercase().as_str());
    // Only devices that would get the same name matter: another kind of device with
    // the same serial string can come and go without renaming this one.
    let shared = all
        .iter()
        .filter(|o| {
            o.serial.trim() == serial
                && (!same_name_only || legacy_identity(o) == legacy_identity(d))
        })
        .count()
        > 1;
    (!placeholder && !shared).then_some(serial)
}
/// Where a device is connected, without the parts Linux may number differently after a
/// restart: HID paths (/dev/hidrawN) are left out entirely, and an i2c bus number is
/// dropped (the address on it stays).
fn stable_location(d: &Device) -> Option<String> {
    let place = d.location.trim();
    if place.is_empty() || place.contains("hidraw") {
        return None;
    }
    let mut out = String::new();
    let mut rest = place;
    while let Some(at) = rest.find("/dev/i2c-") {
        out.push_str(&rest[..at]);
        out.push_str("i2c");
        rest = rest[at + "/dev/i2c-".len()..].trim_start_matches(|c: char| c.is_ascii_digit());
    }
    out.push_str(rest);
    Some(out)
}
/// Numbers devices that still look the same, in the order OpenRGB lists them.
fn numbered(base: Vec<String>) -> Vec<String> {
    base.iter()
        .enumerate()
        .map(|(i, b)| {
            let before = base[..i].iter().filter(|x| *x == b).count();
            let total = base.iter().filter(|x| *x == b).count();
            if total > 1 {
                format!("{b} · #{}", before + 1)
            } else {
                b.clone()
            }
        })
        .collect()
}
/// A lasting name for each device, so its lighting comes back after a restart: name
/// and description, plus a usable serial number or else a stable connection place.
/// Devices that still look the same are numbered in the order OpenRGB lists them.
pub fn identities(devices: &[Device]) -> Vec<String> {
    identities_with(devices, true)
}
fn identities_with(devices: &[Device], same_name_only: bool) -> Vec<String> {
    numbered(
        devices
            .iter()
            .map(|d| {
                match serial_checked(d, devices, same_name_only)
                    .map(|s| format!("serial {s}"))
                    .or_else(|| stable_location(d))
                {
                    Some(p) => format!("{} · {p}", legacy_identity(d)),
                    None => legacy_identity(d),
                }
            })
            .collect(),
    )
}
/// The names earlier releases kept lighting under, for each device: 0.27.0 (name and
/// description), 0.27.1 (plus the raw location or serial) and 0.27.2 (a serial only
/// when no other device at all had it, else the stable location). Newest last.
pub fn earlier_identities(devices: &[Device]) -> Vec<[String; 3]> {
    let v1 = numbered(
        devices
            .iter()
            .map(|d| {
                match [&d.location, &d.serial]
                    .into_iter()
                    .find(|v| !v.trim().is_empty())
                {
                    Some(p) => format!("{} · {}", legacy_identity(d), p.trim()),
                    None => legacy_identity(d),
                }
            })
            .collect(),
    );
    let v2 = identities_with(devices, false);
    devices
        .iter()
        .zip(v1.into_iter().zip(v2))
        .map(|(d, (v1, v2))| [legacy_identity(d), v1, v2])
        .collect()
}

/// The colour shown when a device is set to `color` at `brightness` percent.
pub fn dimmed(color: [u8; 3], brightness: u8) -> [u8; 3] {
    let scale = |c: u8| ((c as u32 * brightness.min(100) as u32 + 50) / 100) as u8;
    [scale(color[0]), scale(color[1]), scale(color[2])]
}
/// "#ff8800" → [255, 136, 0].
pub fn parse_color(text: &str) -> Option<[u8; 3]> {
    let hex = text.strip_prefix('#')?;
    if hex.len() != 6 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let byte = |i: usize| u8::from_str_radix(&hex[i..i + 2], 16).ok();
    Some([byte(0)?, byte(2)?, byte(4)?])
}
fn packet(device: u32, id: u32, body: &[u8]) -> Vec<u8> {
    let mut out = b"ORGB".to_vec();
    for n in [device, id, body.len() as u32] {
        out.extend_from_slice(&n.to_le_bytes());
    }
    out.extend_from_slice(body);
    out
}
/// UPDATELEDS: every LED of the device set to one colour.
pub fn leds_packet(device: &Device, color: [u8; 3]) -> Vec<u8> {
    let count = device.leds.min(u16::MAX as usize);
    let mut body = ((4 + 2 + 4 * count) as u32).to_le_bytes().to_vec();
    body.extend_from_slice(&(count as u16).to_le_bytes());
    for _ in 0..count {
        body.extend_from_slice(&[color[0], color[1], color[2], 0]);
    }
    packet(device.index, UPDATE_LEDS, &body)
}
/// UPDATEMODE: switch to effect `mode`, with the user's colour when it takes one.
pub fn mode_packet(device: &Device, mode: usize, color: Option<[u8; 3]>) -> Option<Vec<u8>> {
    let mut chosen = device.modes.get(mode)?.clone();
    if let (Some(c), true) = (color, chosen.takes_color()) {
        chosen.color_mode = MODE_SPECIFIC_COLORS;
        chosen.colors = vec![c; chosen.colors_min.max(1) as usize];
    }
    let mut data = (mode as i32).to_le_bytes().to_vec();
    chosen.write(&mut data);
    let mut body = ((4 + data.len()) as u32).to_le_bytes().to_vec();
    body.extend_from_slice(&data);
    Some(packet(device.index, UPDATE_MODE, &body))
}

pub struct Client {
    stream: TcpStream,
}
impl Client {
    pub fn connect(port: u16) -> io::Result<Self> {
        let address = SocketAddr::from(([127, 0, 0, 1], port));
        let stream = TcpStream::connect_timeout(&address, Duration::from_millis(300))?;
        stream.set_read_timeout(Some(Duration::from_secs(3)))?;
        stream.set_write_timeout(Some(Duration::from_secs(2)))?;
        let mut client = Client { stream };
        client.send(&packet(0, SET_CLIENT_NAME, b"NekoServer\0"))?;
        Ok(client)
    }
    fn send(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.stream.write_all(bytes)
    }
    fn request(&mut self, device: u32, id: u32) -> io::Result<Vec<u8>> {
        self.send(&packet(device, id, &[]))?;
        let mut head = [0u8; 16];
        self.stream.read_exact(&mut head)?;
        let size = u32::from_le_bytes(head[12..16].try_into().unwrap()) as usize;
        if &head[..4] != b"ORGB" || size > 4_000_000 {
            return Err(io::Error::other("Not an OpenRGB server"));
        }
        let mut body = vec![0; size];
        self.stream.read_exact(&mut body)?;
        Ok(body)
    }
    pub fn devices(&mut self) -> io::Result<Vec<Device>> {
        let count = self.request(0, REQUEST_CONTROLLER_COUNT)?;
        let count = u32::from_le_bytes(
            count
                .get(..4)
                .ok_or(io::ErrorKind::InvalidData)?
                .try_into()
                .unwrap(),
        );
        let mut devices = vec![];
        for index in 0..count.min(64) {
            let body = self.request(index, REQUEST_CONTROLLER_DATA)?;
            if let Some(device) = parse_device(index, &body) {
                devices.push(device);
            }
        }
        Ok(devices)
    }
    /// One steady colour on every LED (OpenRGB's "direct"/custom mode).
    pub fn set_color(&mut self, device: &Device, color: [u8; 3]) -> io::Result<()> {
        self.send(&packet(device.index, SET_CUSTOM_MODE, &[]))?;
        self.send(&leds_packet(device, color))
    }
    pub fn set_mode(
        &mut self,
        device: &Device,
        mode: usize,
        color: Option<[u8; 3]>,
    ) -> io::Result<()> {
        let bytes = mode_packet(device, mode, color).ok_or(io::ErrorKind::InvalidInput)?;
        self.send(&bytes)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    pub fn text(s: &str) -> Vec<u8> {
        let mut b = vec![];
        put_text(&mut b, s);
        b
    }
    /// A controller description as an OpenRGB server sends it (protocol 0).
    pub fn device_bytes(name: &str, modes: &[(&str, u32)], leds: usize, matrix: bool) -> Vec<u8> {
        device_at(name, "HID", modes, leds, matrix)
    }
    pub fn device_at(
        name: &str,
        location: &str,
        modes: &[(&str, u32)],
        leds: usize,
        matrix: bool,
    ) -> Vec<u8> {
        device_full(name, "", location, modes, leds, matrix)
    }
    pub fn device_full(
        name: &str,
        serial: &str,
        location: &str,
        modes: &[(&str, u32)],
        leds: usize,
        matrix: bool,
    ) -> Vec<u8> {
        let mut b = vec![0u8; 4];
        b.extend_from_slice(&4i32.to_le_bytes()); // LED strip
        for s in [name, "Desk strip", "1.0", serial, location] {
            b.extend(text(s));
        }
        b.extend_from_slice(&(modes.len() as u16).to_le_bytes());
        b.extend_from_slice(&0i32.to_le_bytes());
        for (mode, colors_max) in modes {
            let m = Mode {
                name: mode.to_string(),
                value: 1,
                flags: 0,
                speed_min: 0,
                speed_max: 4,
                colors_min: if *colors_max > 0 { 1 } else { 0 },
                colors_max: *colors_max,
                speed: 2,
                direction: 0,
                color_mode: 1,
                colors: vec![[9, 9, 9]; *colors_max as usize],
            };
            m.write(&mut b);
        }
        b.extend_from_slice(&1u16.to_le_bytes()); // one zone
        b.extend(text("Strip"));
        b.extend(vec![0u8; 16]);
        if matrix {
            b.extend_from_slice(&16u16.to_le_bytes());
            b.extend(vec![0u8; 16]);
        } else {
            b.extend_from_slice(&0u16.to_le_bytes());
        }
        b.extend_from_slice(&(leds as u16).to_le_bytes());
        for i in 0..leds {
            b.extend(text(&format!("LED {i}")));
            b.extend_from_slice(&0u32.to_le_bytes());
        }
        b.extend_from_slice(&(leds as u16).to_le_bytes());
        for _ in 0..leds {
            b.extend_from_slice(&[255, 0, 128, 0]);
        }
        b
    }

    /// Put a device in mode `active`, with that mode's colour mode and colours.
    pub fn in_mode(d: &mut Device, active: i32, color_mode: u32, colors: Vec<[u8; 3]>) {
        d.active_mode = active;
        let m = &mut d.modes[active as usize];
        m.color_mode = color_mode;
        m.colors = colors;
    }
    /// Requests a stand-in server received, as (device, request id).
    pub type Requests = std::sync::Arc<std::sync::Mutex<Vec<(u32, u32)>>>;
    /// A stand-in OpenRGB server: answers with `devices` and records every request as
    /// (device, request id). `delay` makes it slow, like a busy or hung OpenRGB.
    pub fn fake_server(devices: Vec<Vec<u8>>, delay: Duration) -> (u16, Requests) {
        use std::net::TcpListener;
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen = std::sync::Arc::new(std::sync::Mutex::new(vec![]));
        let log = seen.clone();
        std::thread::spawn(move || {
            for mut s in listener.incoming().flatten() {
                let (devices, log) = (devices.clone(), log.clone());
                std::thread::spawn(move || loop {
                    let mut head = [0u8; 16];
                    if s.read_exact(&mut head).is_err() {
                        break;
                    }
                    let word = |i: usize| u32::from_le_bytes(head[i..i + 4].try_into().unwrap());
                    let (dev, id, size) = (word(4), word(8), word(12) as usize);
                    let mut data = vec![0; size];
                    if s.read_exact(&mut data).is_err() {
                        break;
                    }
                    log.lock().unwrap().push((dev, id));
                    let reply = match id {
                        REQUEST_CONTROLLER_COUNT => {
                            Some((devices.len() as u32).to_le_bytes().to_vec())
                        }
                        REQUEST_CONTROLLER_DATA => devices.get(dev as usize).cloned(),
                        _ => None,
                    };
                    if let Some(r) = reply {
                        // A slow OpenRGB is slow to answer; packets it doesn't answer
                        // (the client's name, colours) are taken at once, as the real one does.
                        std::thread::sleep(delay);
                        if s.write_all(&packet(dev, id, &r)).is_err() {
                            break;
                        }
                    }
                });
            }
        });
        (port, seen)
    }

    #[test]
    fn identical_devices_get_their_own_lasting_names() {
        let modes = [("Direct", 0)];
        let dev = |name, serial, place| {
            parse_device(0, &device_full(name, serial, place, &modes, 2, false)).unwrap()
        };
        let devices = vec![
            dev("Vengeance", "", "I2C: /dev/i2c-1, address 0x58"),
            dev("Vengeance", "", "I2C: /dev/i2c-1, address 0x59"),
            dev("Strip", "", ""),
            dev("Strip", "0000", ""),
            dev("Hub", "", "HID: /dev/hidraw3"),
            dev("Keyboard", "KB7731A", "HID: /dev/hidraw5"),
            dev("Mouse", "SAME01", "HID: /dev/hidraw6"),
            dev("Mouse", "SAME01", "HID: /dev/hidraw7"),
        ];
        assert_eq!(
            identities(&devices),
            [
                "Vengeance · Desk strip · I2C: i2c, address 0x58",
                "Vengeance · Desk strip · I2C: i2c, address 0x59",
                "Strip · Desk strip · #1",
                "Strip · Desk strip · #2",
                "Hub · Desk strip",
                "Keyboard · Desk strip · serial KB7731A",
                "Mouse · Desk strip · #1",
                "Mouse · Desk strip · #2",
            ]
        );
        // After a restart Linux numbers buses and HID paths differently: same names.
        let moved = vec![
            dev("Vengeance", "", "I2C: /dev/i2c-4, address 0x58"),
            dev("Keyboard", "KB7731A", "HID: /dev/hidraw1"),
            dev("Hub", "", "HID: /dev/hidraw9"),
        ];
        assert_eq!(
            identities(&moved),
            [
                "Vengeance · Desk strip · I2C: i2c, address 0x58",
                "Keyboard · Desk strip · serial KB7731A",
                "Hub · Desk strip",
            ]
        );
        let earlier = earlier_identities(&devices);
        assert_eq!(earlier[0][0], "Vengeance · Desk strip");
        assert_eq!(
            earlier[0][1],
            "Vengeance · Desk strip · I2C: /dev/i2c-1, address 0x58"
        );
        assert_eq!(earlier[5][1], "Keyboard · Desk strip · HID: /dev/hidraw5");
    }

    #[test]
    fn a_device_description_is_read_completely() {
        let body = device_bytes(
            "Strip",
            &[("Direct", 0), ("Breathing", 1), ("Rainbow", 0)],
            3,
            true,
        );
        let d = parse_device(2, &body).unwrap();
        assert_eq!((d.name.as_str(), d.kind, d.leds), ("Strip", "LED strip", 3));
        assert_eq!(
            d.modes.iter().map(|m| m.name.as_str()).collect::<Vec<_>>(),
            ["Direct", "Breathing", "Rainbow"]
        );
        assert_eq!(d.colors[0], [255, 0, 128]);
        assert_eq!(d.to_json()["color"], "#ff0080");
        assert!(d.modes[1].takes_color() && !d.modes[2].takes_color());
        for cut in [10, 40, body.len() - 1] {
            assert!(parse_device(0, &body[..cut]).is_none(), "cut at {cut}");
        }
    }

    #[test]
    fn colour_and_effect_packets_match_the_protocol() {
        let d = parse_device(
            5,
            &device_bytes("Strip", &[("Direct", 0), ("Breathing", 1)], 2, false),
        )
        .unwrap();
        let leds = leds_packet(&d, [1, 2, 3]);
        assert_eq!(&leds[..4], b"ORGB");
        assert_eq!(u32::from_le_bytes(leds[4..8].try_into().unwrap()), 5);
        assert_eq!(
            u32::from_le_bytes(leds[8..12].try_into().unwrap()),
            UPDATE_LEDS
        );
        assert_eq!(
            u32::from_le_bytes(leds[16..20].try_into().unwrap()),
            4 + 2 + 8
        ); // data size
        assert_eq!(&leds[22..30], &[1, 2, 3, 0, 1, 2, 3, 0]);
        // An effect is sent back exactly as described, except for the chosen colour.
        let unchanged = mode_packet(&d, 1, None).unwrap();
        let mut expected = vec![];
        d.modes[1].write(&mut expected);
        assert_eq!(&unchanged[24..], &expected[..]);
        assert_eq!(i32::from_le_bytes(unchanged[20..24].try_into().unwrap()), 1);
        let red = mode_packet(&d, 1, Some([200, 0, 0])).unwrap();
        assert!(red.ends_with(&[1, 0, 200, 0, 0, 0]));
        assert!(mode_packet(&d, 7, None).is_none());
    }

    #[test]
    fn colours_are_read_and_dimmed() {
        assert_eq!(parse_color("#ff8800"), Some([255, 136, 0]));
        assert_eq!(parse_color("ff8800"), None);
        assert_eq!(parse_color("#ff88zz"), None);
        assert_eq!(dimmed([200, 100, 0], 50), [100, 50, 0]);
        assert_eq!(dimmed([200, 100, 0], 0), [0, 0, 0]);
    }
}

#[cfg(test)]
mod review_extra_tests {
    use super::*;
    #[test]
    fn review_unrelated_device_with_same_serial_must_not_rename_a_saved_device() {
        let dev = |name, place| {
            parse_device(
                0,
                &tests::device_full(name, "SHARED123", place, &[("Direct", 0)], 2, false),
            )
            .unwrap()
        };
        let keyboard = dev("Keyboard", "HID: /dev/hidraw1");
        let before = identities(std::slice::from_ref(&keyboard))[0].clone();
        let after = identities(&[keyboard, dev("Mouse", "HID: /dev/hidraw2")])[0].clone();
        assert_eq!(
            after, before,
            "plugging in an unrelated device changes the existing saved key"
        );
    }
}
