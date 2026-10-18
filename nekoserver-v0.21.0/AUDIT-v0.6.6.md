# NekoServer v0.6.6 — integration audit

October 8, 2026. Based on the supplied v0.6.5 ZIP and project summary.

**Result: the original package was not fully connected or ready for deployment.** Its 73 tests passed, but several failure, reboot and recovery paths were not covered. This release fixes verified code defects and expands the suite to **111 passing tests**. It is an audited development release, not hardware acceptance.

## Verified in this runner

- Original baseline: 73/73 tests passed before editing.
- Corrected release: 111/111 tests passed, including 38 new regression tests.
- All six profiles passed dependency-plan validation and dry runs (12 checks).
- All eight rendered media Compose templates parsed as YAML. Checked service names, host/container storage paths, localhost port bindings, and the shared external network.
- Generated Docker mount dependencies/startup guard and all four photo/monitoring service/timer pairs passed systemd definition validation using fixture dependencies.
- Python compilation and shell syntax checks passed.
- Local HTTP fixture exercised the connector's actual urllib request code, API version, authentication header, JSON payload and error redaction.
- Actual temporary-file tests covered interrupted copy recovery, preserved archives after source deletion, collision refusal, symlink escape refusal and atomic publication without hard links.
- Simulated full-profile execution confirmed that a media failure blocks its monitor, independent photo/network modules continue, and `--retry-failed` subsequently runs the failed and blocked modules.

Docker Engine, the Compose CLI, pacman, a running Nyarch desktop, physical drives and the real applications were not available for live deployment. YAML parsing is not a substitute for `docker compose config` or startup.

## Corrections

| Fault in v0.6.5 | Correction in v0.6.6 |
|---|---|
| Docker's reboot dependency covered only the SSD; media containers could restart before the HDD was mounted. | Require both SSD and HDD, bind Docker to their mount units, and check the configured UUID/filesystem types before startup. Samba also gains an HDD dependency. |
| Fresh Docker 29+ can use a separate containerd image store despite a custom Docker data-root. | Fresh installs explicitly disable the containerd snapshotter. Active snapshotter installations and unverified stopped installations are rejected rather than silently changing backend. |
| `--retry-failed` considered only modules enabled by default, overlooking optional modules deployed through profiles. | Retry every known failed/blocked module, including profile-selected modules. |
| Background photo scripts still used the v0.5.2 runtime directory, blocking upgrades when their contents differed. | Deploy a new immutable v0.6.6 runtime; keep old versions. Managed photo units can point to the new runtime. |
| Custom photo archive paths were not used consistently; weekly jobs inferred the media mount from a fixed directory depth. | Pass the configured archive path and explicit media mount through photo deployment and scheduled jobs. |
| External backup advertised exFAT support but published files using hard links only. | Fall back to Linux `renameat2(RENAME_NOREPLACE)` when hard links are unsupported; refuse overwriting and fail if the filesystem cannot provide safe publication. Physical exFAT validation remains pending. |
| Predictable temporary report/config files and some parent symlinks could redirect writes. | Shared safe-path checks and exclusive random temporary files for atomic reports/configuration. Strengthen source, destination and deployment path checks. |
| Duplicate filesystem UUIDs were silently collapsed; nested mount assignments were accepted. | Reject cloned UUIDs, nested mounts, unsafe paths and duplicate media service entries. |
| Existing connection names were treated as proof that apps were linked. | Verify connection fields, app sync mode, client enable state, host, port, username and categories. Existing differing entries are flagged for review and preserved. Stored masked client passwords still require UI verification. |
| Some rejected/skipped API configurations returned a success exit code. | Return incomplete/failure status and continue independent requested connector actions; blank client passwords also count as incomplete. |
| New Prowlarr connections did not explicitly establish sync mode. | Use additive `addOnly` sync for newly created app connections. Existing disabled sync is flagged. |
| Onboarding ignored configured service subsets and reported before configuration only. | Follow `--config` and selected services, reject conflicting flags, report after connector execution, and return non-success for incomplete setup. |
| Health audit counted any provider record as configured, including disabled or wrongly routed records. | Check enabled indexers, expected Prowlarr application endpoints, qBittorrent routing/categories and Arr root folders. HTTP 5xx is failure. |
| Database secondary copies were published directly while still being written; dump errors could echo provider stderr. | Publish the HDD copy after verification using a partial file; make SQL files private and avoid printing provider stderr. |
| Photo archive omitted common video/RAW formats. | Broaden the supported original formats, including WebM, MKV and additional RAW formats. The archive still scans only Immich upload/library trees. |
| Docker migration rejected an explicitly configured default source root and retained the old SSD-only guard. | Accept the explicit `/var/lib/docker` source, verify the running storage backend, and install the same SSD/HDD guard used for fresh deployment. Actual migration remains untested. |

The Docker guard intentionally makes the media HDD a prerequisite for **the entire Docker daemon**. If that mount disappears, systemd can stop Docker, including SSD-only containers. This favors data safety; it does not detect every hardware I/O error. A missing disk must be corrected before restarting Docker. Upgrading the guard on an already healthy running daemon does not restart its containers.

## Actual handoffs and remaining gaps

| Flow | Current behavior |
|---|---|
| System → storage → Docker | Connected in module dependency order. Storage asks for assignments; Docker uses the dedicated SSD and guarded mounts. |
| Docker → eight media projects | Connected: separate persistent appdata, independent Compose projects, shared DNS network and consistent `/data` paths. Runtime connectivity remains untested. |
| Installer → application onboarding | **Separate command.** The installer does not invoke `onboard.py` or wait for app initialization. |
| Source wizard → local/remote media access | **Inventory only.** Stored references are not mounted, imported, or converted into container mappings. Onboarding does not invoke this wizard. |
| Prowlarr → Sonarr/Radarr/Lidarr | Connector can create additive API connections and detect differing existing entries. Provider compatibility and real sync must be validated. |
| Arr managers → qBittorrent | Connector registers the client and categories. It does not configure qBittorrent's actual save path or authenticate directly to qBittorrent. Set `/data/downloads` in its UI and test the credentials. |
| Arr root folders | Health checker verifies them. The connector does not create them: Sonarr `/data/tv`, Radarr `/data/movies`, Lidarr `/data/music`. |
| Seerr → Jellyfin/Arr | Network exists, but accounts, authentication, Jellyfin linkage and Arr settings remain manual. |
| Jellyfin/Navidrome first use | Accounts, libraries and playback require onboarding. GPU passthrough is declared for Jellyfin but hardware acceleration is untested. |
| Immich → archive → USB backup | Script and timer handoffs corrected. Source-only deletions are preserved in file tests; full application restore and phone deletion behavior are untested. |
| Immich archive → database backup | Separate archive and nightly SQL copies on SSD/HDD. SQL dumps are not included in the weekly external photo archive copy. |
| Tailscale → Samba | Tailscale sign-in, existing share folders and an SMB password are required. Running `full` before login can fail Samba. Use staged setup. |
| Replacing the OS | Media appdata, Docker data and job scripts reside on the SSD. A complete installer snapshot, restore automation, users, systemd units and network identity recovery are not implemented. Keep this release ZIP independently. Bazzite backend remains future work. |

Photo timers archive mature originals periodically, not immediately. Deleting an original before the archive job has copied it can lose the only copy. Hourly archival and the ten-minute minimum file age are not a guarantee that every new upload has already been preserved. Verify the archive and independent backup first.

## Reproducible checks

Run from the extracted package directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
bash -n install.sh
bash install.sh --profile full --check
bash install.sh --profile full --dry-run
```

`--check` and `--dry-run` validate configuration/dependency planning; they do not execute hardware or application checks. `--all` still includes only enabled modules by default (system/storage/Docker), not every optional feature.

## Server acceptance sequence

1. Read the installer and confirm the SSD/HDD assignments. These must already have supported filesystems; formatting is never automatic. Confirm target app UID/GID, filesystem permissions and GPU device availability.
2. Deploy foundation, then media, then photos as separate profiles. The photos profile includes external backup: set the external drive UUID/filesystem or expect that module to fail visibly. Other independent photo modules can still complete.
3. Check Docker's root, classic image store, mounted filesystem UUIDs and both mount dependencies. Reboot and test missing-drive startup in a controlled environment before storing irreplaceable data.
4. Complete initial app authentication, qBittorrent save path, Arr root folders, Jellyfin libraries, Navidrome user and Seerr connections.
5. Run the connector preview, then apply; run the health and live doctor tools. Treat nonzero status as a remaining action, not success.
6. Use permitted test media to prove request → acquisition → import → playback. Validate music separately. Confirm hardware acceleration on the actual Intel GPU.
7. Upload disposable photo/video samples. Confirm archive and USB checksums, source/Immich deletion behavior, absent/full USB behavior and a complete isolated Immich restore using the matching app version/database/upload tree. Do not delete your only photo originals before this succeeds.
8. Install Tailscale and authenticate, then Samba. Set the SMB password; verify read permissions and listening interfaces. Remote browser gateway remains deferred.
9. Keep the release ZIP and verified backups independently; perform an OS replacement/restore exercise before claiming recovery is complete.

Container templates still use `latest` image tags. Their versions/digests must be reviewed and pinned before production; the audit did not invent version pins for untested images. An existing customized Compose manifest is preserved and blocks automatic overwrite.

## Reference checks

The audit cross-checked storage and API behavior against primary sources:

- Docker daemon data directory: https://docs.docker.com/engine/daemon/
- Docker containerd storage and backend switching: https://docs.docker.com/engine/storage/containerd/
- Docker bind mounts: https://docs.docker.com/engine/storage/bind-mounts/
- systemd dependency semantics: https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html
- Prowlarr API schema and sync modes: https://raw.githubusercontent.com/Prowlarr/Prowlarr/develop/src/Prowlarr.Api.V1/openapi.json
- Linux no-replace rename: https://man7.org/linux/man-pages/man2/rename.2.html

Release evidence is included under `audit-evidence/`. Tests with external command mocks prove orchestration and failure handling; they do not certify the real hardware, packages, API services or container images.
