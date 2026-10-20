//! OpenRGB SDK client: list lighting devices, set their colour and effect.
//!
//! OpenRGB (installed by setup and run as a local service) does the hardware work for
//! hundreds of devices; NekoServer speaks its network protocol on 127.0.0.1:6742.
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
    for _ in 0..3 {
        r.text()?; // version, serial, location
    }
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
        let mut b = vec![0u8; 4];
        b.extend_from_slice(&4i32.to_le_bytes()); // LED strip
        for s in [name, "Desk strip", "1.0", "", "HID"] {
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
