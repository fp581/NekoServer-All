//! Cheap native startup guards use the same stored filesystem identities as jobs.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use serde_json::Value;
use std::net::Ipv4Addr;
fn private(ip: u32) -> bool {
    ip >> 24 == 10 || ip >> 20 == 0xac1 || ip >> 16 == 0xc0a8
}
fn mask(prefix: u32) -> u32 {
    u32::MAX << (32 - prefix)
}
fn lan(cfg: &Value, rows: &Value) -> Result<()> {
    let address = cfg["gateway"]["lan_ip"].as_str().unwrap_or("");
    let ip = u32::from(
        address
            .parse::<Ipv4Addr>()
            .map_err(|_| Error::new("LAN IPv4 required"))?,
    );
    let (network, prefix) = cfg["gateway"]["lan_cidr"]
        .as_str()
        .unwrap_or("")
        .split_once('/')
        .ok_or_else(|| Error::new("LAN CIDR required"))?;
    let net = u32::from(
        network
            .parse::<Ipv4Addr>()
            .map_err(|_| Error::new("Invalid LAN network"))?,
    );
    let prefix = prefix
        .parse::<u32>()
        .map_err(|_| Error::new("Invalid LAN prefix"))?;
    if !(16..=30).contains(&prefix)
        || !private(ip)
        || !private(net)
        || net & mask(prefix) != net
        || ip & mask(prefix) != net
        || ip == net
        || ip == net | !mask(prefix)
    {
        return Err(Error::new("Restricted canonical LAN network required"));
    }
    let rows = rows
        .as_array()
        .ok_or_else(|| Error::new("Invalid interface inventory"))?;
    for row in rows {
        let name = row["ifname"].as_str().unwrap_or("");
        if name.is_empty()
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
            || internal_interface(name)
        {
            continue;
        }
        for info in row["addr_info"].as_array().into_iter().flatten() {
            let physical = info["prefixlen"].as_u64().unwrap_or(0);
            if info["family"] == "inet"
                && info["scope"] == "global"
                && info["local"] == address
                && (16..=30).contains(&physical)
                && prefix >= physical as u32
                && net & mask(physical as u32) == ip & mask(physical as u32)
            {
                return Ok(());
            }
        }
    }
    Err(Error::new("Configured LAN interface is not active"))
}
/// Interfaces that are never the home network: the loopback, Docker's (docker0, and
/// br-<12 hex> for each Docker network), Podman's, libvirt's virtual networks, container
/// veths, Tailscale and VPN tunnels. A bridge of your own that carries the home network
/// (br0, bridge0, br-lan) is not one of them (network_access.internal_interface agrees).
fn internal_interface(name: &str) -> bool {
    let docker_network = name.strip_prefix("br-").is_some_and(|id| {
        id.len() == 12
            && id
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    });
    docker_network
        || [
            "lo",
            "docker",
            "veth",
            "virbr",
            "podman",
            "cni-",
            "tailscale",
            "tun",
            "tap",
            "wg",
        ]
        .iter()
        .any(|s| name.starts_with(s))
}
/// The block devices that carry filesystem `uuid`, each once (lsblk lists an LVM volume
/// under every disk it spans); None when lsblk can't say.
pub fn devices_with_uuid(uuid: &str) -> Option<Vec<String>> {
    let (code, text) = common::command(&["lsblk", "-J", "-l", "-o", "PATH,UUID"], 10).ok()?;
    if code != 0 {
        return None;
    }
    Some(with_uuid(&serde_json::from_str(&text).ok()?, uuid))
}
fn with_uuid(rows: &Value, uuid: &str) -> Vec<String> {
    let mut found: Vec<String> = rows["blockdevices"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|row| !uuid.is_empty() && row["uuid"].as_str() == Some(uuid))
        .filter_map(|row| row["path"].as_str().map(str::to_string))
        .collect();
    found.sort();
    found.dedup();
    found
}
/// A Btrfs device's own UUID (UUID_SUB), read from the device itself; None when it
/// can't be read.
fn btrfs_device_id(device: &str) -> Option<String> {
    let (code, text) = common::command(
        &["blkid", "-p", "-s", "UUID_SUB", "-o", "value", "--", device],
        10,
    )
    .ok()?;
    let id = text.trim();
    (code == 0 && !id.is_empty()).then(|| id.to_string())
}
/// Which of `devices` (all carrying one filesystem UUID) look like copies of one another:
/// all of them, unless they are the devices of one Btrfs pool, each with a UUID_SUB of
/// its own (a copy of one has the same one). Nothing when a device ID can't be read: a
/// check that can't be made never stops a healthy drive.
fn look_alikes(
    fstype: &str,
    devices: &[String],
    id: impl Fn(&str) -> Option<String>,
) -> Vec<String> {
    if devices.len() < 2 {
        return vec![];
    }
    if fstype != "btrfs" {
        return devices.to_vec();
    }
    let Some(ids) = devices.iter().map(|d| id(d)).collect::<Option<Vec<_>>>() else {
        return vec![];
    };
    devices
        .iter()
        .zip(&ids)
        .filter(|(_, own)| ids.iter().filter(|other| other == own).count() > 1)
        .map(|(device, _)| device.clone())
        .collect()
}
fn copy_message(drive: &str, uuid: &str, copies: &[String]) -> String {
    format!(
        "Two drives look the same (one is probably a copy); unplug the copy and try again. {} carry the same filesystem as the {drive} (UUID {uuid}), so Linux could use either one.",
        copies.join(" and ")
    )
}
/// A copy of the SSD or HDD (dd, Clonezilla) left connected carries its UUID, type and
/// subvolume too, so the identity check passes for it, and fstab mounts by UUID: Linux
/// could have mounted the copy. Refused before Docker starts.
pub fn copies(cfg: &Value) -> Result<()> {
    for (key, drive) in [("data_ssd", "SSD"), ("media_hdd", "HDD")] {
        let uuid = cfg["storage"][key]["uuid"].as_str().unwrap_or("");
        let fstype = cfg["storage"][key]["fstype"].as_str().unwrap_or("");
        let devices = devices_with_uuid(uuid).unwrap_or_default();
        let found = look_alikes(fstype, &devices, btrfs_device_id);
        if !found.is_empty() {
            return Err(Error::new(&copy_message(drive, uuid, &found)));
        }
    }
    Ok(())
}
pub fn run(a: &Args, gateway: bool) -> Result<()> {
    let cfg = backup::open_job(a, 64000)?.cfg;
    if gateway {
        let (code, text) = common::command(&["ip", "-j", "address", "show", "up"], 10)?;
        if code != 0 {
            return Err(Error::new("LAN inspection failed"));
        }
        lan(&cfg, &serde_json::from_str(&text)?)?;
    } else {
        copies(&cfg)?;
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn physical_lan_only() {
        let cfg = json!({"gateway":{"lan_ip":"192.168.1.20","lan_cidr":"192.168.1.0/24"}});
        for name in [
            "enp3s0",
            "br0",
            "bridge0",
            "br-lan",
            "tailscale0",
            "docker0",
            "br-0123456789ab",
            "virbr0",
            "veth12ab",
        ] {
            let rows = json!([{"ifname":name,"addr_info":[{"family":"inet","scope":"global","local":"192.168.1.20","prefixlen":24}]}]);
            let home = ["enp3s0", "br0", "bridge0", "br-lan"].contains(&name);
            assert_eq!(lan(&cfg, &rows).is_ok(), home, "{name}");
        }
    }
    #[test]
    fn broad_wrong_or_noncanonical_lan_refused() {
        for (ip, cidr) in [
            ("10.1.2.3", "10.0.0.0/8"),
            ("192.168.1.20", "192.168.1.20/24"),
            ("192.168.1.20", "192.168.2.0/24"),
            ("8.8.8.8", "8.8.0.0/16"),
        ] {
            assert!(lan(
                &json!({"gateway":{"lan_ip":ip,"lan_cidr":cidr}}),
                &json!([])
            )
            .is_err());
        }
    }
    fn ids<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        |device| {
            pairs
                .iter()
                .find(|(d, _)| *d == device)
                .map(|(_, id)| id.to_string())
        }
    }
    #[test]
    fn a_connected_copy_is_found_but_a_pool_or_an_unreadable_one_is_not() {
        let rows = json!({"blockdevices":[
            {"path":"/dev/sda","uuid":null},{"path":"/dev/sdb1","uuid":"u-1"},
            {"path":"/dev/sdc1","uuid":"u-1"},{"path":"/dev/mapper/vg-media","uuid":"u-2"},
            {"path":"/dev/mapper/vg-media","uuid":"u-2"}]});
        let two = with_uuid(&rows, "u-1");
        assert_eq!(two, ["/dev/sdb1", "/dev/sdc1"]);
        // An LVM volume lsblk lists under each of its disks is one device.
        assert_eq!(with_uuid(&rows, "u-2"), ["/dev/mapper/vg-media"]);
        assert!(with_uuid(&rows, "").is_empty());
        let none = |_: &str| None;
        assert_eq!(look_alikes("ext4", &two, none), two);
        assert!(look_alikes("ext4", &two[..1], none).is_empty());
        // The devices of one Btrfs pool each have their own device ID; a copy the same one.
        let pool = [("/dev/sdb1", "d-1"), ("/dev/sdc1", "d-2")];
        assert!(look_alikes("btrfs", &two, ids(&pool)).is_empty());
        let copy = [("/dev/sdb1", "d-1"), ("/dev/sdc1", "d-1")];
        assert_eq!(look_alikes("btrfs", &two, ids(&copy)), two);
        // A device ID that can't be read: today's behaviour, nothing is refused.
        assert!(look_alikes("btrfs", &two, ids(&pool[..1])).is_empty());
        let text = copy_message("HDD", "u-1", &two);
        assert!(text.contains("Two drives look the same (one is probably a copy); unplug the copy"));
        assert!(text.contains("/dev/sdb1 and /dev/sdc1"));
    }
    #[test]
    fn guard_config_does_not_allow_root_mount() {
        assert!(common::mount_path("/").is_err());
        assert!(common::safe(std::path::Path::new("/tmp/../etc")).is_err());
    }
}
