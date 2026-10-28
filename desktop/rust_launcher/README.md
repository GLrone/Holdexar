# Windows Rust launcher experiment

The normal launch path selects a version, checks `current.json` data compatibility against SQLite (including WAL), migrates legacy data using a read-only online backup, and starts the versioned program. It uses the existing `current.json`, `transaction.json`, and `migration.json` records. The default production build continues to use the Python launcher.

An update transaction with `dataRollbackReleased`, `dataAccepted`, `dataPrepared`, or a nonempty `dataDir` requires the existing independent `.launcher/SafetyHost.exe` Python launcher. The Rust entry checks its canonical location and forwards the original arguments, installation root and exit code. Missing or redirected recovery components stop startup. Recovery is deliberately kept in the existing protection implementation instead of copying snapshot restoration into Rust. This means accepted historical updates also use the shared recovery path, just as the Python launcher rechecks them on every launch.

`--server` waits for and returns the version program's exit code. Other launches detach the version process. `--update-protocol` only reports the existing protocol JSON and does not start the application.

The only crate dependency is `serde_json`. SQLite comes from Windows' `winsqlite3.dll`, loaded from System32; no SQLite DLL or C compiler is required. The used APIs require Windows 10 version 1511 or newer. Building requires a Windows GNU Rust toolchain; its bundled `rust-lld` can link without an external GCC installation:

```powershell
$sysroot = rustc --print sysroot
$env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = Join-Path $sysroot 'lib/rustlib/x86_64-pc-windows-gnu/bin/rust-lld.exe'
$env:RUSTFLAGS = '-C linker-flavor=ld.lld -C link-self-contained=yes'
cargo test --locked --manifest-path desktop/rust_launcher/Cargo.toml
cargo build --release --locked --manifest-path desktop/rust_launcher/Cargo.toml
```

A complete experiment package contains the Rust entry, the same versioned payload and UpdateHost as the Python comparison, and `.launcher/SafetyHost.exe`. Include this helper in package size and recovery measurements. Normal-path and recovery-path measurements must be reported separately.

Unit tests are not a substitute for the shared packaged contract matrix and Windows GUI measurement. See `desktop/native_launcher/packaged_contract.py` for real executable and data fault vectors.

Platform references: [Microsoft SQLite API availability](https://learn.microsoft.com/en-us/uwp/win32-and-com/win32-extension-apis), [SQLite online backup](https://www.sqlite.org/backup.html), [read-only WAL requirements](https://www.sqlite.org/wal.html#read_only_databases).
