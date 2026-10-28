# Windows Launcher

The root `Holdexar.exe` is built with Go. It selects the versioned application, validates `state/current.json` data compatibility through a read-only SQLite connection, safely snapshots legacy data, and starts the application with the installation and data directory context. Existing `current.json`, `transaction.json`, and `migration.json` remain the only state records.

SQLite is supplied by the pinned `modernc.org/sqlite` module. The build uses no CGO or external C compiler. `go.sum` records the module checksums.

```powershell
go test ./...
go build -trimpath '-ldflags=-s -w -H=windowsgui' -o Holdexar.exe .
```

Production packaging is owned by `scripts/build_release.py`, which supplies application identity metadata and assembles the complete versioned payload and independent UpdateHost. The command above only builds the entry executable.

The launcher delegates interrupted or accepted update data transactions to the existing `updater/UpdateHost.exe --recover-only --target <installation-root>` path. It validates the host location, returns failure if recovery fails, and reloads the state after recovery. It does not restore update snapshots itself or depend on the versioned application for protection.

`--server` waits for the versioned program and returns its exit status. Other launches detach the program. `--update-protocol` reports the existing update protocol without starting the application.

Use `scripts/check_launcher_contract.py` for isolated executable, filesystem, data and recovery checks. Unit tests and protocol checks do not establish GUI startup performance or full-package upgrade acceptance.
