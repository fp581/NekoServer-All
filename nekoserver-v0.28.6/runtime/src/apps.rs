//! App controls on the dashboard: restart a server app, close a desktop app.
//!
//! Runs inside the root control service (control.rs), which checks the PIN first.
//! Only these two actions exist, and each checks for itself what it may touch:
//!
//! - **Restart a server app**: only the NekoServer apps chosen in setup, as a whole.
//!   Its database and cache (`database`, `redis`) keep running; the rest of the app
//!   is stopped together and started again in order (Immich's server and machine
//!   learning; Nextcloud and its cron). It waits for the shared lock that backups
//!   and the monthly update hold, so it is refused while one of them runs, and while
//!   an interrupted job still waits to be finished. It counts as done only once Docker
//!   reports the app running (healthy, where the app has a health check) and the app
//!   answers on its web address.
//! - **Close a desktop app**: only an app someone started in a desktop session
//!   (systemd's app.slice, see live::desktop_of), never the desktop itself, a system
//!   service, a backup or an app container. Close asks every process of the app to
//!   quit (SIGTERM, as logging out does); Force close stops them at once (SIGKILL).
//!
//! Actions run in the background; the page follows them through the control status.
use crate::{
    app_snapshot, backup,
    collect::APPS,
    common::{self, Error, Result},
    host_snapshot,
    live::{self, PROTECTED_SERVICES},
    maintenance, media,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    os::fd::{FromRawFd, OwnedFd},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

/// Written by `recover.py snapshot` while Immich is stopped (recovery_tools.py).
const IMMICH_SNAPSHOT_RESUME: &str = "/etc/nekoserver-backups/immich-snapshot-resume.json";
/// Finished actions are shown for this long, then forgotten.
const KEEP_FINISHED: f64 = 600.0;
/// How long a restarted app may take to run and answer again.
const START_LIMIT: Duration = Duration::from_secs(240);
/// How long Close waits for an app to quit, and Force close for Linux to end it.
const CLOSE_WAIT: Duration = Duration::from_secs(10);
const FORCE_WAIT: Duration = Duration::from_secs(5);

/// The running and recently finished actions, by target ("app:immich",
/// "desktop:<cgroup path>"). One action per target at a time.
#[derive(Default)]
pub struct Actions(Mutex<BTreeMap<String, Value>>);
impl Actions {
    fn entries(&self) -> std::sync::MutexGuard<'_, BTreeMap<String, Value>> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Record a new action; false while one for the same target still runs (or, with
    /// `alone`, while any action of the same kind does: one app restarts at a time).
    fn begin(&self, key: &str, value: Value, alone: bool) -> bool {
        let mut map = self.entries();
        let kind = value["kind"].clone();
        if map
            .iter()
            .any(|(k, v)| v["state"] == "running" && (k == key || (alone && v["kind"] == kind)))
        {
            return false;
        }
        map.insert(key.to_string(), value);
        true
    }
    fn forget(&self, key: &str) {
        self.entries().remove(key);
    }
    fn update(&self, key: &str, fields: Value) {
        if let (Some(entry), Some(fields)) = (self.entries().get_mut(key), fields.as_object()) {
            for (k, v) in fields {
                entry[k] = v.clone();
            }
            entry["at"] = json!(common::now());
        }
    }
    /// Whether a restart is still stopping or starting parts (not just waiting for the
    /// app to answer): stopping the service then would leave the app half stopped.
    pub fn changing_apps(&self) -> bool {
        self.entries()
            .values()
            .any(|v| v["kind"] == "app" && v["state"] == "running" && v["phase"] != "checking")
    }
    /// What the page shows: running actions, and finished ones for 10 minutes.
    pub fn list(&self) -> Value {
        let now = common::now();
        let mut map = self.entries();
        map.retain(|_, v| {
            v["state"] == "running" || now - v["at"].as_f64().unwrap_or(0.0) < KEEP_FINISHED
        });
        json!(map.values().cloned().collect::<Vec<_>>())
    }
}

fn refused(code: &str, message: &str) -> Value {
    json!({"ok": false, "error": code, "message": message})
}
fn started(key: &str) -> Value {
    json!({"ok": true, "key": key})
}

/* ---------- server apps ---------- */

/// The parts of an app that are restarted, in the order they start (they stop in
/// the reverse order). The first is the app itself and must exist; the others are
/// optional (Immich's machine learning can be left out; Nextcloud's cron too). The
/// database and cache are never among them.
pub fn restart_plan(app: &str) -> Vec<&'static str> {
    match app {
        "immich" => vec!["immich-server", "immich-machine-learning"],
        "nextcloud" => vec!["nextcloud", "cron"],
        _ => APPS
            .iter()
            .find(|a| a.0 == app)
            .map(|a| vec![a.0])
            .unwrap_or_default(),
    }
}
/// The apps chosen in setup, from the dashboard's settings.
pub fn installed(cfg: &Value) -> Vec<&'static str> {
    APPS.iter()
        .map(|a| a.0)
        .filter(|app| match *app {
            "immich" => cfg["photos"]["deploy_immich"] == true,
            "nextcloud" => cfg["nextcloud"]["enabled"] == true,
            _ => cfg["media"]["services"]
                .as_array()
                .is_some_and(|s| s.iter().any(|v| v == app)),
        })
        .collect()
}
fn app_name(app: &str) -> &'static str {
    APPS.iter().find(|a| a.0 == app).map_or("The app", |a| a.1)
}
fn app_port(app: &str) -> Option<u16> {
    APPS.iter().find(|a| a.0 == app).map(|a| a.3)
}

/// An interrupted backup, snapshot or update that still has to be finished: until it
/// is, it may start or stop these same apps.
pub fn unfinished_job(journals: &[&Path]) -> Option<&'static str> {
    const WHAT: [&str; 4] = [
        "An interrupted monthly update",
        "An interrupted app backup",
        "An interrupted host backup",
        "An interrupted Immich snapshot",
    ];
    journals
        .iter()
        .zip(WHAT)
        .find(|(p, _)| p.exists())
        .map(|(_, w)| w)
}
fn journals() -> [&'static Path; 4] {
    [
        Path::new(maintenance::JOURNAL),
        Path::new(app_snapshot::JOURNAL),
        Path::new(host_snapshot::JOURNAL),
        Path::new(IMMICH_SNAPSHOT_RESUME),
    ]
}

/// Where the shared lock lives, once the drives in the settings are checked.
fn shared_lock(config: &Path) -> Result<PathBuf> {
    let cfg = common::json(config, 64000);
    let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
    let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
    backup::guard(config, &data, &media)?;
    Ok(data.join("installer/recovery.lock"))
}

pub struct Restart<'a> {
    pub config: &'a Path,
    pub app: &'a str,
    pub actions: &'a Arc<Actions>,
}
impl Restart<'_> {
    /// Check, take the lock and start restarting in the background.
    pub fn start(self) -> Value {
        let cfg = common::json(self.config, 64000);
        let Some(app) = installed(&cfg).into_iter().find(|a| *a == self.app) else {
            return refused("E42", "That app isn't installed on this server");
        };
        let name = app_name(app);
        let key = format!("app:{app}");
        if let Some(job) = unfinished_job(&journals()) {
            return refused("E47", &format!("{job} is still waiting to be finished, and it starts and stops apps itself. NekoServer finishes it on its own (or at the next restart of the computer); try again after that."));
        }
        let lock = match shared_lock(self.config) {
            Ok(path) => path,
            Err(_) => return refused("E47", "The app drive or media drive isn't the one in your settings, so nothing was restarted. Server health shows which drive is missing."),
        };
        // Recorded first, so two requests at once can't both take the lock.
        if !self.actions.begin(
            &key,
            json!({"key": key, "kind": "app", "target": app, "label": name,
            "verb": "restart", "state": "running", "phase": "stopping",
            "message": format!("Restarting {name}…"), "began": common::now(), "at": common::now()}),
            true,
        ) {
            return refused(
                "E47",
                "An app is already restarting. Wait for it to finish, then try again.",
            );
        }
        let Ok(held) = common::lock(&lock) else {
            self.actions.forget(&key);
            return refused("E47", &format!("A backup or the monthly update is running right now. Restarting {name} could disturb it, so wait for it to finish (Backups shows which one), then try again."));
        };
        let (actions, key2, app) = (self.actions.clone(), key.clone(), app.to_string());
        thread::spawn(move || {
            let result = restart_app(&app, |step| {
                let doing = if step == "checking" {
                    "checking it answers"
                } else {
                    step
                };
                actions.update(
                    &key2,
                    json!({"phase": step, "message": format!("Restarting {}… ({doing})", app_name(&app))}),
                )
            });
            drop(held);
            let name = app_name(&app);
            actions.update(&key2, match result {
                Ok(note) => json!({"state": "done", "message": format!("{name} restarted{note}.")}),
                Err(e) => json!({"state": "failed", "message": format!("{name} didn't restart properly: {e}")}),
            });
        });
        started(&key)
    }
}

/// One container of the app: its Compose service and ID.
fn containers(app: &str) -> Result<BTreeMap<String, String>> {
    let (code, out) = common::command(
        &[
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "ps",
            "-a",
            "--filter",
            &format!("label=com.docker.compose.project=neko-{app}"),
            "--format",
            "{{.ID}} {{.Label \"com.docker.compose.service\"}}",
        ],
        30,
    )?;
    if code != 0 {
        return Err(Error::new(
            "Docker didn't answer. Check Server health → Starts by itself",
        ));
    }
    let mut found = BTreeMap::new();
    for line in out.lines() {
        if let Some((id, service)) = line.trim().split_once(' ') {
            if found.insert(service.to_string(), id.to_string()).is_some() {
                return Err(Error::new(
                    "it has more than one container for the same part; run setup again to tidy it",
                ));
            }
        }
    }
    Ok(found)
}
fn docker(args: &[&str], timeout: u64) -> Result<String> {
    let mut all = vec!["docker", "--host", "unix:///var/run/docker.sock"];
    all.extend_from_slice(args);
    match common::command(&all, timeout)? {
        (0, out) => Ok(out),
        _ => Err(Error::new("Docker refused the request. Check Server health → Starts by itself, or run setup again")),
    }
}
/// Stop the app's parts (never its database or cache), start them again in order, then
/// wait until Docker reports them running (and healthy, where they have a health
/// check) and the app answers on its web address. Ok carries a note for the result.
fn restart_app(app: &str, progress: impl Fn(&str)) -> Result<&'static str> {
    let plan = restart_plan(app);
    if plan.is_empty() || plan.iter().any(|s| PROTECTED_SERVICES.contains(s)) {
        return Err(Error::new("it has no parts that may be restarted"));
    }
    let found = containers(app)?;
    let ids = plan
        .iter()
        .filter_map(|s| found.get(*s).cloned())
        .collect::<Vec<_>>();
    if !found.contains_key(plan[0]) {
        return Err(Error::new(
            "its container is missing; run setup again to put it back",
        ));
    }
    progress("stopping");
    // Whatever happens while stopping, every part is started again afterwards: a part
    // left stopped would stay stopped (Docker doesn't restart what was stopped on purpose).
    let stopped = ids
        .iter()
        .rev()
        .map(|id| docker(&["stop", "--time", "60", id], 120))
        .collect::<Vec<_>>();
    progress("starting");
    // Every part is started, whatever happened to the others: a part left stopped
    // would stay stopped until the next restart of the computer.
    let started = ids
        .iter()
        .map(|id| docker(&["start", id], 120))
        .collect::<Vec<_>>();
    if started.iter().any(Result::is_err) {
        return Err(Error::new("Docker couldn't start every part of it again. Check Server health → Starts by itself, or run setup again, which starts what should be running"));
    }
    let note = if stopped.iter().any(Result::is_err) {
        " (Docker couldn't stop every part, so some kept running instead of restarting)"
    } else {
        ""
    };
    progress("checking");
    let deadline = Instant::now() + START_LIMIT;
    let port = app_port(app);
    loop {
        let states = ids
            .iter()
            .map(|id| {
                docker(
                    &[
                        "inspect",
                        "--format",
                        "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                        id,
                    ],
                    30,
                )
            })
            .collect::<Result<Vec<_>>>()?;
        let verdict = verdict(&states, port.and_then(media::answers).is_some());
        match verdict {
            Verdict::Ready => return Ok(note),
            Verdict::Failed(why) => return Err(Error::new(why)),
            Verdict::Waiting if Instant::now() >= deadline => {
                return Err(Error::new("it is running but still doesn't answer after 4 minutes. It may need more time after an update; if it stays like this, open Help → An app is stopped or not responding"))
            }
            Verdict::Waiting => thread::sleep(Duration::from_secs(3)),
        }
    }
}
#[derive(Debug, PartialEq)]
enum Verdict {
    Ready,
    Waiting,
    Failed(&'static str),
}
/// From each part's "status health" (from docker inspect) and whether the app answers.
fn verdict(states: &[String], answers: bool) -> Verdict {
    let mut waiting = !answers;
    for s in states {
        let mut words = s.split_whitespace();
        let (status, health) = (words.next().unwrap_or(""), words.next().unwrap_or(""));
        match (status, health) {
            ("exited" | "dead", _) => return Verdict::Failed("it stopped again right after starting. Its own log says why: sudo docker logs on the server, or run setup again"),
            (_, "unhealthy") => return Verdict::Failed("it started, but its own health check reports a problem. Open Help → An app is stopped or not responding"),
            ("running", "healthy" | "") => {}
            _ => waiting = true,
        }
    }
    if waiting {
        Verdict::Waiting
    } else {
        Verdict::Ready
    }
}

/* ---------- desktop apps ---------- */

/// Where Linux describes processes and their groups (fixed; tests use a copy).
pub struct Host {
    pub proc: PathBuf,
    pub cgroup: PathBuf,
}
impl Default for Host {
    fn default() -> Self {
        Host {
            proc: PathBuf::from("/proc"),
            cgroup: PathBuf::from("/sys/fs/cgroup"),
        }
    }
}
impl Host {
    /// When the process started (clock ticks after boot), to tell it from a later one
    /// that got the same number.
    fn start_of(&self, pid: u32) -> Option<u64> {
        let stat = fs::read_to_string(self.proc.join(format!("{pid}/stat"))).ok()?;
        let after = &stat[stat.rfind(')')? + 1..];
        after.split_whitespace().nth(19)?.parse().ok()
    }
    fn desktop(&self, pid: u32) -> Option<live::Desktop> {
        live::desktop_of(&fs::read_to_string(self.proc.join(format!("{pid}/cgroup"))).ok()?)
    }
    /// Every process of the desktop app, from its cgroup and any groups below it.
    fn members(&self, path: &str) -> Vec<u32> {
        let mut pids = vec![];
        let mut folders = vec![self.cgroup.join(path.trim_start_matches('/'))];
        while let Some(folder) = folders.pop() {
            if let Ok(listed) = fs::read_to_string(folder.join("cgroup.procs")) {
                pids.extend(listed.lines().filter_map(|l| l.trim().parse::<u32>().ok()));
            }
            for entry in fs::read_dir(&folder).into_iter().flatten().flatten() {
                if entry.file_type().is_ok_and(|t| t.is_dir()) && folders.len() < 200 {
                    folders.push(entry.path());
                }
            }
        }
        pids.sort_unstable();
        pids.dedup();
        pids
    }
    /// Signal each process still in the app. Each is checked again right before (through
    /// a pidfd, so a number Linux has just given to another process is never signalled).
    fn signal(&self, path: &str, signal: libc::c_int) -> usize {
        let mut sent = 0;
        for pid in self.members(path) {
            // SAFETY: pidfd_open takes a pid and flags and returns a new descriptor or -1.
            let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::pid_t, 0) };
            if fd < 0 {
                continue;
            }
            // SAFETY: the descriptor was just returned to us and is owned from here on.
            let fd = unsafe { OwnedFd::from_raw_fd(fd as i32) };
            if self.desktop(pid).map(|d| d.path).as_deref() != Some(path) {
                continue;
            }
            use std::os::fd::AsRawFd;
            // SAFETY: a valid pidfd, a signal number, no siginfo, no flags.
            let ok = unsafe {
                libc::syscall(
                    libc::SYS_pidfd_send_signal,
                    fd.as_raw_fd(),
                    signal,
                    std::ptr::null::<libc::siginfo_t>(),
                    0,
                )
            } == 0;
            sent += usize::from(ok);
        }
        sent
    }
    fn gone(&self, path: &str) -> bool {
        self.members(path)
            .into_iter()
            .all(|pid| self.desktop(pid).map(|d| d.path).as_deref() != Some(path))
    }
}

pub struct Close<'a> {
    pub host: Arc<Host>,
    pub pid: u32,
    pub start: u64,
    pub force: bool,
    pub actions: &'a Arc<Actions>,
}
impl Close<'_> {
    pub fn start(self) -> Value {
        if self.host.start_of(self.pid) != Some(self.start) {
            return refused(
                "E42",
                "That program has already ended. The list updates in a moment.",
            );
        }
        let Some(app) = self.host.desktop(self.pid) else {
            return refused("E48", "This is part of the server or the desktop itself, so it can't be closed from here. Only apps you opened on the desktop can.");
        };
        let key = format!("desktop:{}", app.path);
        let (verb, doing) = if self.force {
            ("force", "Force closing")
        } else {
            ("close", "Stopping")
        };
        let label = app.label.clone();
        if !self.actions.begin(&key, json!({"key": key, "kind": "desktop", "target": app.path, "pid": self.pid,
            "label": label, "verb": verb, "state": "running", "message": format!("{doing} {label}…"),
            "began": common::now(), "at": common::now()}), false)
        {
            return refused("E47", &format!("{label} is already being closed."));
        }
        let signal = if self.force {
            libc::SIGKILL
        } else {
            libc::SIGTERM
        };
        if self.host.signal(&app.path, signal) == 0 {
            let message = if self.host.gone(&app.path) {
                json!({"state": "done", "message": format!("{label} has already closed.")})
            } else {
                json!({"state": "failed", "message": format!("{label} couldn't be asked to close: Linux refused the request.")})
            };
            self.actions.update(&key, message);
            return started(&key);
        }
        let (host, actions, key2, force) = (
            self.host.clone(),
            self.actions.clone(),
            key.clone(),
            self.force,
        );
        thread::spawn(move || {
            let deadline = Instant::now() + if force { FORCE_WAIT } else { CLOSE_WAIT };
            while !host.gone(&app.path) && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(250));
            }
            actions.update(&key2, match (host.gone(&app.path), force) {
                (true, false) => json!({"state": "done", "message": format!("{label} closed.")}),
                (true, true) => json!({"state": "done", "message": format!("{label} was force closed.")}),
                (false, false) => json!({"state": "failed", "can_force": true, "message": format!(
                    "{label} is still running. It may be asking something in its window (such as saving a file), or it isn't responding. Check its window, or use Force close.")}),
                (false, true) => json!({"state": "failed", "message": format!(
                    "{label} is still there. It is probably waiting for a drive or the network and ends as soon as that answers. If it never does, restart the computer.")}),
            });
        });
        started(&key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_restart_never_touches_a_database_or_cache() {
        for (app, ..) in APPS {
            let plan = restart_plan(app);
            assert!(!plan.is_empty(), "{app}");
            assert!(
                plan.iter().all(|s| !PROTECTED_SERVICES.contains(s)),
                "{app}"
            );
        }
        assert_eq!(
            restart_plan("immich"),
            ["immich-server", "immich-machine-learning"]
        );
        assert_eq!(restart_plan("nextcloud"), ["nextcloud", "cron"]);
        assert!(restart_plan("postgres").is_empty());
    }

    #[test]
    fn only_apps_chosen_in_setup_can_be_restarted() {
        let cfg = json!({"media": {"services": ["jellyfin", "sonarr"]}, "photos": {"deploy_immich": true}, "nextcloud": {"enabled": false}});
        assert_eq!(installed(&cfg), ["jellyfin", "immich", "sonarr"]);
        let actions = Arc::new(Actions::default());
        let root = std::env::temp_dir().join(format!("neko-apps-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let config = root.join("config.json");
        fs::write(&config, cfg.to_string()).unwrap();
        for app in ["nextcloud", "radarr", "docker", "../etc"] {
            let answer = Restart {
                config: &config,
                app,
                actions: &actions,
            }
            .start();
            assert_eq!(answer["error"], "E42", "{app}");
        }
        // The drives in these settings aren't mounted here, so nothing is restarted.
        let answer = Restart {
            config: &config,
            app: "jellyfin",
            actions: &actions,
        }
        .start();
        assert_eq!(answer["error"], "E47");
        assert!(answer["message"].as_str().unwrap().contains("drive"));
        assert_eq!(actions.list(), json!([]));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn an_unfinished_job_blocks_restarts_and_says_which() {
        let root = std::env::temp_dir().join(format!("neko-apps-journal-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let files = ["m.json", "a.json", "h.json", "i.json"].map(|n| root.join(n));
        let paths = files.iter().map(PathBuf::as_path).collect::<Vec<_>>();
        assert_eq!(unfinished_job(&paths), None);
        fs::write(&files[3], "{}").unwrap();
        assert_eq!(
            unfinished_job(&paths),
            Some("An interrupted Immich snapshot")
        );
        fs::write(&files[0], "{}").unwrap();
        assert_eq!(
            unfinished_job(&paths),
            Some("An interrupted monthly update")
        );
        let _ = fs::remove_dir_all(root);
        // The same records the jobs themselves write.
        assert!(include_str!("../../recovery_tools.py").contains(IMMICH_SNAPSHOT_RESUME));
    }

    #[test]
    fn a_restart_waits_for_health_checks_and_an_answer() {
        let s = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(
            verdict(&s(&["running healthy", "running "]), true),
            Verdict::Ready
        );
        assert_eq!(verdict(&s(&["running"]), true), Verdict::Ready);
        assert_eq!(verdict(&s(&["running starting"]), true), Verdict::Waiting);
        assert_eq!(verdict(&s(&["running healthy"]), false), Verdict::Waiting);
        assert_eq!(verdict(&s(&["restarting"]), true), Verdict::Waiting);
        assert!(matches!(
            verdict(&s(&["running unhealthy"]), true),
            Verdict::Failed(_)
        ));
        assert!(matches!(
            verdict(&s(&["running healthy", "exited"]), true),
            Verdict::Failed(_)
        ));
        // Being busy is never a failure: a fully used processor is not part of it at all.
    }

    #[test]
    fn actions_run_one_at_a_time_and_finished_ones_expire() {
        let actions = Actions::default();
        let running = |key: &str, kind: &str| json!({"key": key, "kind": kind, "state": "running", "phase": "stopping", "at": common::now()});
        assert!(actions.begin("app:jellyfin", running("app:jellyfin", "app"), true));
        assert!(!actions.begin("app:jellyfin", running("app:jellyfin", "app"), true));
        // One app at a time; a desktop app may close meanwhile.
        assert!(!actions.begin("app:immich", running("app:immich", "app"), true));
        assert!(actions.begin("desktop:/x", running("desktop:/x", "desktop"), false));
        assert!(actions.changing_apps());
        actions.update("app:jellyfin", json!({"phase": "checking"}));
        assert!(
            !actions.changing_apps(),
            "waiting for an answer is safe to interrupt"
        );
        actions.update("app:jellyfin", json!({"state": "done"}));
        assert!(actions.begin("app:immich", running("app:immich", "app"), true));
        actions.forget("app:immich");
        actions.forget("desktop:/x");
        assert_eq!(actions.list().as_array().unwrap().len(), 1);
        actions.entries().get_mut("app:jellyfin").unwrap()["at"] =
            json!(common::now() - KEEP_FINISHED - 1.0);
        assert_eq!(actions.list(), json!([]));
    }

    /// A fake /proc and cgroup tree describing real child processes, so the signals
    /// are real but only ever reach the processes this test started.
    struct Desk {
        root: PathBuf,
        host: Arc<Host>,
        path: String,
    }
    impl Desk {
        fn new(name: &str, unit: &str) -> Self {
            let root =
                std::env::temp_dir().join(format!("neko-desk-{name}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            let path = format!("/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}");
            fs::create_dir_all(root.join("cgroup").join(path.trim_start_matches('/'))).unwrap();
            let host = Arc::new(Host {
                proc: root.join("proc"),
                cgroup: root.join("cgroup"),
            });
            Desk { root, host, path }
        }
        fn describe(&self, pid: u32, start: u64, cgroup: &str) {
            let dir = self.root.join(format!("proc/{pid}"));
            fs::create_dir_all(&dir).unwrap();
            fs::write(
                dir.join("stat"),
                format!("{pid} (app) S 1 1 1 0 -1 0 0 0 0 0 1 0 0 0 20 0 1 0 {start} 0 25 0 0"),
            )
            .unwrap();
            fs::write(dir.join("cgroup"), format!("0::{cgroup}\n")).unwrap();
        }
        fn members(&self, pids: &[u32]) {
            let list = pids.iter().map(|p| format!("{p}\n")).collect::<String>();
            fs::write(
                self.root
                    .join("cgroup")
                    .join(self.path.trim_start_matches('/'))
                    .join("cgroup.procs"),
                list,
            )
            .unwrap();
        }
    }
    impl Drop for Desk {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }
    fn wait_done(actions: &Actions, key: &str) -> Value {
        for _ in 0..200 {
            let found = actions.entries().get(key).cloned().unwrap();
            if found["state"] != "running" {
                return found;
            }
            thread::sleep(Duration::from_millis(100));
        }
        panic!("action never finished");
    }

    #[test]
    fn close_asks_every_process_of_the_app_to_quit() {
        let desk = Desk::new("close", "app-org.kde.kate-77aa.scope");
        let mut children = (0..2)
            .map(|_| {
                std::process::Command::new("sleep")
                    .arg("30")
                    .spawn()
                    .unwrap()
            })
            .collect::<Vec<_>>();
        let pids = children.iter().map(|c| c.id()).collect::<Vec<_>>();
        for pid in &pids {
            desk.describe(*pid, 4242, &desk.path);
        }
        desk.members(&pids);
        let actions = Arc::new(Actions::default());
        // A number from an older process (another start time) is refused.
        let stale = Close {
            host: desk.host.clone(),
            pid: pids[0],
            start: 1,
            force: false,
            actions: &actions,
        }
        .start();
        assert_eq!(stale["error"], "E42");
        let answer = Close {
            host: desk.host.clone(),
            pid: pids[0],
            start: 4242,
            force: false,
            actions: &actions,
        }
        .start();
        assert_eq!(answer["ok"], true, "{answer}");
        for child in &mut children {
            let status = child.wait().unwrap();
            assert_eq!(
                std::os::unix::process::ExitStatusExt::signal(&status),
                Some(libc::SIGTERM)
            );
        }
        desk.members(&[]);
        let done = wait_done(&actions, answer["key"].as_str().unwrap());
        assert_eq!(done["state"], "done");
        assert_eq!(done["label"], "Kate");
    }

    #[test]
    fn an_app_that_ignores_close_can_be_force_closed() {
        let desk = Desk::new("force", "app-stubborn-1234.scope");
        // Ignores the polite request, like an app waiting on a "save changes?" question.
        let mut child = std::process::Command::new("sh")
            .args(["-c", "trap '' TERM; while :; do sleep 0.1; done"])
            .spawn()
            .unwrap();
        thread::sleep(Duration::from_millis(200));
        let pid = child.id();
        desk.describe(pid, 9, &desk.path);
        desk.members(&[pid]);
        let actions = Arc::new(Actions::default());
        let answer = Close {
            host: desk.host.clone(),
            pid,
            start: 9,
            force: false,
            actions: &actions,
        }
        .start();
        let key = answer["key"].as_str().unwrap().to_string();
        let first = wait_done(&actions, &key);
        assert_eq!(
            (first["state"].as_str(), first["can_force"].as_bool()),
            (Some("failed"), Some(true)),
            "{first}"
        );
        assert!(first["message"].as_str().unwrap().contains("Force close"));
        let forced = Close {
            host: desk.host.clone(),
            pid,
            start: 9,
            force: true,
            actions: &actions,
        }
        .start();
        assert_eq!(forced["ok"], true);
        let status = child.wait().unwrap();
        assert_eq!(
            std::os::unix::process::ExitStatusExt::signal(&status),
            Some(libc::SIGKILL)
        );
        desk.members(&[]);
        assert_eq!(wait_done(&actions, &key)["state"], "done");
    }

    #[test]
    fn the_desktop_itself_and_the_server_are_protected() {
        let desk = Desk::new("protected", "app-x-1.scope");
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let pid = child.id();
        let actions = Arc::new(Actions::default());
        for cgroup in [
            "/user.slice/user-1000.slice/user@1000.service/session.slice/plasma-kwin_wayland.service",
            "/system.slice/nekoserver-photo-backup.service",
            "/system.slice/docker-0123.scope",
        ] {
            desk.describe(pid, 5, cgroup);
            let answer = Close { host: desk.host.clone(), pid, start: 5, force: true, actions: &actions }.start();
            assert_eq!(answer["error"], "E48", "{cgroup}");
        }
        // A process that moved out of the app after it was chosen is left alone.
        desk.describe(pid, 5, &desk.path);
        desk.members(&[pid]);
        let other = Desk::new("protected-moved", "app-y-1.scope");
        fs::write(
            desk.root.join(format!("proc/{pid}/cgroup")),
            format!("0::{}\n", other.path),
        )
        .unwrap();
        assert_eq!(desk.host.signal(&desk.path, libc::SIGKILL), 0);
        assert!(child.try_wait().unwrap().is_none());
        let _ = child.kill();
        let _ = child.wait();
    }
}
