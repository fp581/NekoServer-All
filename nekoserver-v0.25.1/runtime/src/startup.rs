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
            || [
                "lo",
                "docker",
                "br",
                "veth",
                "virbr",
                "tailscale",
                "tun",
                "tap",
                "wg",
            ]
            .iter()
            .any(|s| name.starts_with(s))
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
pub fn run(a: &Args, gateway: bool) -> Result<()> {
    common::require_root()?;
    let path = a.path("config")?;
    let cfg: Value = serde_json::from_slice(&common::bytes(&path, 64000)?)?;
    let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
    let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
    backup::guard(&path, &data, &media)?;
    if gateway {
        let (code, text) = common::command(&["ip", "-j", "address", "show", "up"], 10)?;
        if code != 0 {
            return Err(Error::new("LAN inspection failed"));
        }
        lan(&cfg, &serde_json::from_str(&text)?)?;
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
        for name in ["enp3s0", "tailscale0", "docker0"] {
            let rows = json!([{"ifname":name,"addr_info":[{"family":"inet","scope":"global","local":"192.168.1.20","prefixlen":24}]}]);
            assert_eq!(lan(&cfg, &rows).is_ok(), name == "enp3s0");
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
    #[test]
    fn guard_config_does_not_allow_root_mount() {
        assert!(common::mount_path("/").is_err());
        assert!(common::safe(std::path::Path::new("/tmp/../etc")).is_err());
    }
}
