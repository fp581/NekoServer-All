# Slim installer package - NekoServer v0.21.0

This is the same executable code, configuration defaults, runtime and 21-page guide as the full v0.21.0 release. Use INSTALLATION-GUIDE.pdf for setup. All installation, acceptance, migration, diagnostic and recovery entry points remain present.

Removed from this package only: tests/, historical AUDIT-v*.md notes, historical audit-evidence subtrees and dashboard/preview.html (the offline demonstration). The full nekoserver-v0.21.0.zip retains these development/history files. Any references to historical notes, the test directory or the offline preview in the unchanged documentation refer to that full package. Current audit results and logs remain here.

Kept intentionally: runtime source/build metadata for native integrity verification; third-party licenses; active dashboard assets; optional Python diagnostics and recovery helpers; component/profiles and cleanup modules that disable retired jobs; current operational guides and audit evidence. Do not delete these as presumed duplicates.

This packaging change does not require upgrading a server already using v0.21.0. It does not remove anything from an installed system or existing backups. There is no need to prune live /srv/nekoserver contents by hand.
