# Changelog

## v0.21.1 — 2026-10-18

### Fixed
- **Lidarr imports no longer copy music.** Lidarr now mounts the SSD library once at `/data`, so `/data/downloads/music` and `/data/music` share one mount. Previously they were separate bind mounts, so imports failed to hardlink/move (EXDEV) and fell back to copying, doubling SSD use while seeding. Container paths and app settings are unchanged; mount checks in `acceptance.py`, `media-doctor.py`, `media-health.py` and the Rust runtime were updated to match.
- **Seerr honours `media.uid` / `media.gid`** instead of a hardcoded `1000:1000`.
- **Timezone is configurable.** A new top-level `timezone` config key drives container `TZ`, Immich's `.env` and every systemd timer. It falls back to the old `media.timezone` / `maintenance.timezone` keys, then the host's `/etc/localtime`, then UTC. The default stays `Asia/Jerusalem`.

### Added
- Upgrade path: unedited v0.21.0 Lidarr and Seerr manifests are recognised and migrated (`previous_templates()` in `modules/media.py`), alongside the existing Navidrome migration.
- `tests/test_review_fixes.py` with regression tests for the fixes above.
- GitHub Actions CI: Python compile/lint/tests, `docker compose config`, `cargo fmt`/`clippy`/`test`, a check that the bundled runtime matches its sources, and a static musl runtime build that can commit the rebuilt binary.
- `.gitignore`.

### Upgrade notes
- The bundled `runtime/bin/nekoserver-runtime` must be rebuilt for this release (Rust sources and version changed). Run the CI workflow with **commit_runtime**, or `python3 runtime/build.py` as a normal user.
- Rerunning the installer upgrades unedited v0.21.0 Lidarr and Seerr manifests in place, keeps the old one as `compose.before-v0.21.1.yaml`, and recreates those containers. A manifest you edited by hand is left alone and reported, as before.
- The installation guide PDF still shows v0.21.0 paths; substitute `v0.21.1`.
