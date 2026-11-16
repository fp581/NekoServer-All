//! Cheap native startup guards use the same stored filesystem identities as jobs.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use serde_json::Value;
use std::{net::Ipv4Addr, time::Duration};
fn private(ip: u32) -> bool {
    ip >> 24 == 10 || ip >> 20 == 0xac1 || ip >> 16 == 0xc0a8
}
fn mask(prefix: u32) -> u32 {
    u32::MAX << (32 - prefix)
}
pub(crate) fn lan(cfg: &Value, rows: &Value) -> Result<()> {
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
    Err(Error::new(LAN_GONE))
}
/// The saved home-network address is on none of this PC's connections (a new router, or
/// one that gave the PC another address): the dashboard says so (collect.rs home_network).
pub(crate) const LAN_GONE: &str = "Configured LAN interface is not active";
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
            // An isolated restore test's own network (recovery_tools.py test_bridge).
            "neko-rt-",
        ]
        .iter()
        .any(|s| name.starts_with(s))
}
/// The block devices that carry filesystem `uuid`, each once (lsblk lists an LVM volume
/// under every disk it spans); None when lsblk can't say. LVM snapshots of the drive are
/// left out (`lvm_snapshots`): they are not copies.
pub fn devices_with_uuid(uuid: &str) -> Option<Vec<String>> {
    let (code, text) = common::command(
        &[
            "lsblk",
            "-J",
            "-l",
            "-o",
            "PATH,UUID,TYPE,NAME,KNAME,PKNAME",
        ],
        10,
    )
    .ok()?;
    if code != 0 {
        return None;
    }
    Some(with_uuid(&serde_json::from_str(&text).ok()?, uuid))
}
/// The devices under a software RAID array (lsblk lists the array once under each, with
/// the member as PKNAME). A member whose array superblock sits at its end (metadata 1.0,
/// how a drive is made a mirror in place) shows the filesystem's UUID too until udev
/// looks at it again, but it is the array's own drive, not a copy left connected.
fn raid_members(rows: &Value) -> Vec<String> {
    rows["blockdevices"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|row| {
            let kind = row["type"].as_str().unwrap_or("");
            kind.starts_with("raid") || kind == "md" || kind == "linear"
        })
        .filter_map(|row| row["pkname"].as_str().map(str::to_string))
        .collect()
}
fn with_uuid(rows: &Value, uuid: &str) -> Vec<String> {
    let snapshots = lvm_snapshots(rows);
    let members = raid_members(rows);
    let mut found: Vec<String> = rows["blockdevices"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|row| !uuid.is_empty() && row["uuid"].as_str() == Some(uuid))
        .filter(|row| !members.iter().any(|m| row["kname"].as_str() == Some(m)))
        .filter_map(|row| row["path"].as_str().map(str::to_string))
        .filter(|path| !snapshots.contains(path))
        .collect();
    found.sort();
    found.dedup();
    found
}
/// Whether a device-mapper name is LVM's hidden layer `suffix` ("real" or "cow") of a
/// volume: "<vg>-<lv>-real". LVM doubles every '-' inside the names, so the dash before
/// the suffix is a separator only when the dashes there are odd in number.
fn lvm_layer(name: &str, suffix: &str) -> bool {
    name.strip_suffix(suffix).is_some_and(|stem| {
        let dashes = stem.len() - stem.trim_end_matches('-').len();
        dashes % 2 == 1
    })
}
/// The paths of LVM snapshots (lvcreate -s) and their hidden layers, from lsblk's list
/// (TYPE, NAME, KNAME, PKNAME). A volume with snapshots sits on "<origin>-real", and each
/// snapshot on that "-real" and on its own "<snapshot>-cow"; lsblk lists such a volume
/// once under each. A snapshot carries the volume's filesystem UUID, but it is not a copy
/// left connected: Linux mounts the volume itself by that UUID (LVM gives the snapshot's
/// link the lower priority), so it is left out of the copy check. The origin stays in.
fn lvm_snapshots(rows: &Value) -> Vec<String> {
    let rows: Vec<&Value> = rows["blockdevices"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|row| row["type"] == "lvm")
        .collect();
    let name_of = |kname: &Value| {
        rows.iter()
            .find(|row| kname.is_string() && row["kname"] == *kname)
            .and_then(|row| row["name"].as_str())
            .unwrap_or("")
    };
    rows.iter()
        .filter(|row| {
            let name = row["name"].as_str().unwrap_or("");
            let parent = name_of(&row["pkname"]);
            let layer = lvm_layer(name, "real") || lvm_layer(name, "cow");
            let snapshot = (lvm_layer(parent, "real") && parent != format!("{name}-real"))
                || parent == format!("{name}-cow");
            !name.is_empty() && (layer || snapshot)
        })
        .filter_map(|row| row["path"].as_str().map(str::to_string))
        .collect()
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
    copies_of(cfg, &["data_ssd", "media_hdd"])
}
/// `copies` for the drives named in `keys` (data_ssd, media_hdd).
pub fn copies_of(cfg: &Value, keys: &[&str]) -> Result<()> {
    for (key, drive) in [("data_ssd", "SSD"), ("media_hdd", "HDD")]
        .into_iter()
        .filter(|(key, _)| keys.contains(key))
    {
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
/// Why a start of Docker, Samba or the gateway is refused now, or None: the NAS check holds
/// the apps while an app drive on the NAS fails writes, and it starts them itself once every
/// drive takes writes. Before, any job that needs Docker (an update, a retry at :17, "Update
/// now") started it again on the failing drive (the review of 0.47.0).
fn held_back(cfg: &serde_json::Value, held: bool) -> Option<String> {
    let on_a_nas = ["data_ssd", "media_hdd"]
        .iter()
        .any(|key| cfg["storage"][*key]["network"].is_object());
    (held && on_a_nas).then(|| {
        format!(
            "The apps are held: a drive on the network storage box does not take writes. The NAS check starts them by itself once every drive does (journalctl -u {})",
            common::shared("nas_watch", "service")
        )
    })
}
/// This PC's connections and their addresses (`ip -j address show up`).
fn interfaces() -> Result<Value> {
    let (code, text) = common::command(&["ip", "-j", "address", "show", "up"], 10)?;
    if code != 0 {
        return Err(Error::new("LAN inspection failed"));
    }
    Ok(serde_json::from_str(&text)?)
}
/// How often the start check looks again for the home-network address.
const LAN_POLL: Duration = Duration::from_secs(5);
/// How long it waits for it (shared.json gateway_start; the unit allows that long).
fn lan_wait() -> Duration {
    Duration::from_secs(
        common::SHARED["gateway_start"]["address_wait_seconds"]
            .as_u64()
            .expect("shared.json says how long the gateway waits for its address"),
    )
}
/// `lan`, waiting up to `wait` for the saved home-network address to appear. After a
/// power cut the PC is up before the router: the address comes a few minutes later.
/// Refused at once, the gateway used up its six tries in about a minute, gave up, and the
/// apps' home addresses stayed down until someone started it by hand. Any other refusal
/// (settings that name no usable address, the connections unreadable) is at once.
fn wait_for_lan(
    cfg: &Value,
    wait: Duration,
    rows: impl Fn() -> Result<Value>,
    sleep: impl Fn(Duration),
) -> Result<()> {
    let tries = wait.as_secs() / LAN_POLL.as_secs();
    for done in 0..tries {
        match rows().and_then(|rows| lan(cfg, &rows)) {
            Err(e) if e.to_string() == LAN_GONE => {
                if done == 0 {
                    println!(
                        "Waiting up to {} s for this server's home-network address {} (after a power cut the router may still be starting)",
                        wait.as_secs(),
                        cfg["gateway"]["lan_ip"].as_str().unwrap_or("")
                    );
                }
                sleep(LAN_POLL);
            }
            other => return other,
        }
    }
    rows().and_then(|rows| lan(cfg, &rows))
}
pub fn run(a: &Args, gateway: bool) -> Result<()> {
    let cfg = backup::open_job(a, 64000)?.cfg;
    if let Some(reason) = held_back(&cfg, crate::nas_watch::held()) {
        return Err(Error::new(&reason));
    }
    if gateway {
        wait_for_lan(&cfg, lan_wait(), interfaces, std::thread::sleep)?;
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
    fn nothing_that_writes_to_the_drives_starts_while_the_nas_check_holds_the_apps() {
        let nas = json!({"storage":{"data_ssd":{"uuid":"a"},"media_hdd":{"uuid":"b","network":{"transport":"iscsi","wwn":"0x1"}}}});
        let said = held_back(&nas, true).expect("refused while held");
        assert!(said.contains("nekoserver-nas-watch.service"), "{said}");
        assert!(held_back(&nas, false).is_none());
        // Only the backup drive on a NAS: the check never holds the apps, nothing is refused.
        let local = json!({"storage":{"data_ssd":{"uuid":"a"},"media_hdd":{"uuid":"b"}}});
        assert!(held_back(&local, true).is_none());
    }
    #[test]
    fn the_gateway_waits_for_an_address_the_router_has_not_given_yet() {
        // After a power cut the router answers a few minutes after the PC is up.
        let cfg = json!({"gateway":{"lan_ip":"192.168.1.20","lan_cidr":"192.168.1.0/24"}});
        let on = json!([{"ifname":"enp3s0","addr_info":[{"family":"inet","scope":"global","local":"192.168.1.20","prefixlen":24}]}]);
        let looks = std::cell::Cell::new(0);
        let rows = || {
            looks.set(looks.get() + 1);
            Ok(if looks.get() < 10 {
                json!([])
            } else {
                on.clone()
            })
        };
        let slept = std::cell::Cell::new(0);
        let wait = lan_wait();
        assert!(wait >= Duration::from_secs(120), "{wait:?}");
        assert!(wait_for_lan(&cfg, wait, rows, |_| slept.set(slept.get() + 1)).is_ok());
        assert_eq!((looks.get(), slept.get()), (10, 9));
        // Never there: refused as gone (what the dashboard's own check says), after the wait.
        slept.set(0);
        let gone = wait_for_lan(&cfg, wait, || Ok(json!([])), |_| slept.set(slept.get() + 1));
        assert_eq!(gone.unwrap_err().to_string(), LAN_GONE);
        assert_eq!(slept.get() as u64, wait.as_secs() / LAN_POLL.as_secs());
        // Settings that name no usable address are refused at once.
        slept.set(0);
        let bad = json!({"gateway":{"lan_ip":"8.8.8.8","lan_cidr":"8.8.0.0/16"}});
        assert!(wait_for_lan(&bad, wait, || Ok(on.clone()), |_| slept.set(1)).is_err());
        assert_eq!(slept.get(), 0);
    }
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
    fn the_drives_of_a_raid_array_are_its_own_not_copies_of_it() {
        // The HDD made a RAID-1 in place (metadata 1.0): until udev looks again, the
        // member sdb still shows the filesystem's UUID beside the array's.
        let rows = json!({"blockdevices":[
            {"path":"/dev/sdb","uuid":"u-1","type":"disk","name":"sdb","kname":"sdb","pkname":null},
            {"path":"/dev/sdc","uuid":null,"type":"disk","name":"sdc","kname":"sdc","pkname":null},
            {"path":"/dev/md0","uuid":"u-1","type":"raid1","name":"md0","kname":"md0","pkname":"sdb"},
            {"path":"/dev/md0","uuid":"u-1","type":"raid1","name":"md0","kname":"md0","pkname":"sdc"},
            {"path":"/dev/sdd1","uuid":"u-1","type":"part","name":"sdd1","kname":"sdd1","pkname":"sdd"}]});
        // sdb is left out; a copy on another drive (sdd1) is still found.
        assert_eq!(with_uuid(&rows, "u-1"), ["/dev/md0", "/dev/sdd1"]);
    }
    fn lvm_row(name: &str, kname: &str, parent: &str, uuid: Option<&str>) -> Value {
        json!({"path":format!("/dev/mapper/{name}"),"uuid":uuid,"type":"lvm","name":name,"kname":kname,"pkname":parent})
    }
    /// The HDD as LVM volume vg0/media on /dev/sdc1 with a snapshot (lvcreate -s -n
    /// media-snap), as `lsblk -J -l -o PATH,UUID,TYPE,NAME,KNAME,PKNAME` lists it: the
    /// volume and the snapshot both on the hidden vg0-media-real, the snapshot on its own
    /// vg0-media--snap-cow too, listed once under each; the hidden layers carry no UUID.
    fn lvm_snapshot(extra: &[Value]) -> Value {
        let hdd = Some("u-2");
        let mut rows = vec![
            json!({"path":"/dev/sdc","uuid":null,"type":"disk","name":"sdc","kname":"sdc","pkname":null}),
            json!({"path":"/dev/sdc1","uuid":"Xc4Qz1-pv01","type":"part","name":"sdc1","kname":"sdc1","pkname":"sdc"}),
            lvm_row("vg0-media-real", "dm-1", "sdc1", None),
            lvm_row("vg0-media", "dm-0", "dm-1", hdd),
            lvm_row("vg0-media--snap", "dm-3", "dm-1", hdd),
            lvm_row("vg0-media--snap-cow", "dm-2", "sdc1", None),
            lvm_row("vg0-media--snap", "dm-3", "dm-2", hdd),
        ];
        rows.extend_from_slice(extra);
        json!({ "blockdevices": rows })
    }
    #[test]
    fn an_lvm_snapshot_is_not_a_copy_but_a_real_copy_beside_it_is() {
        let rows = lvm_snapshot(&[]);
        assert_eq!(with_uuid(&rows, "u-2"), ["/dev/mapper/vg0-media"]);
        let mut hidden = lvm_snapshots(&rows);
        hidden.sort();
        hidden.dedup();
        assert_eq!(
            hidden,
            [
                "/dev/mapper/vg0-media--snap",
                "/dev/mapper/vg0-media--snap-cow",
                "/dev/mapper/vg0-media-real"
            ]
        );
        // A dd copy of the volume connected beside it is still found.
        let copy = json!({"path":"/dev/sdd1","uuid":"u-2","type":"part","name":"sdd1","kname":"sdd1","pkname":"sdd"});
        let two = with_uuid(&lvm_snapshot(&[copy]), "u-2");
        assert_eq!(two, ["/dev/mapper/vg0-media", "/dev/sdd1"]);
        assert_eq!(look_alikes("ext4", &two, |_: &str| None), two);
        // Two plain volumes with one UUID (a copy from one to the other) stay a copy.
        let copied = json!({"blockdevices":[lvm_row("vg0-media", "dm-0", "sdc1", Some("u-2")), lvm_row("vg0-copy", "dm-4", "sdc1", Some("u-2"))]});
        assert_eq!(
            with_uuid(&copied, "u-2"),
            ["/dev/mapper/vg0-copy", "/dev/mapper/vg0-media"]
        );
        // LVM doubles a '-' inside a name: a volume named "media-real" is no hidden layer.
        assert!(!lvm_layer("vg0-media--real", "real"));
        assert!(lvm_layer("vg0-media---real", "real"));
        assert!(lvm_layer("vg0-media--snap-cow", "cow"));
        assert!(!lvm_layer("cow", "cow"));
    }
    #[test]
    fn guard_config_does_not_allow_root_mount() {
        assert!(common::mount_path("/").is_err());
        assert!(common::safe(std::path::Path::new("/tmp/../etc")).is_err());
    }
}
