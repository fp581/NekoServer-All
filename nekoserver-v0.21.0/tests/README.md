# Running the release checks

Use a disposable Linux test container/VM, not an installed server. Native recovery fixtures use reserved `/run/nekoserver-*` locks and temporary `/etc/nekoserver-backups/*` journals/configuration. Mount, package-manager, Docker and service commands are replaced by fixture adapters; files/tar/checksums/native processes/local HTTP are real.

Run exactly one suite at a time. Concurrent suites contend for those shared locks and journals and can invalidate each other's evidence. Wait for the previous process to finish before starting another, and use a distinct log filename per run. Do not treat skipped root/Compose/native fixtures as passed integration tests.

The complete release command is:

```bash
NEKO_TEST_SERVER_BINARY=/absolute/path/to/isolated-test-fixtures-build \
  python3 -m unittest discover -s tests -v > /absolute/path/to/unique-log.txt 2>&1
```

The transport test-fixtures binary is for testing only; install only the separately built, checksummed production binary. The official Compose parser and pinned Immich fixture/provenance must exist where `install_simulation.py` expects them. Real hardware acceptance is a separate process described in ACCEPTANCE.md and INSTALLATION-GUIDE.pdf.
