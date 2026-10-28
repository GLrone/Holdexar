#![windows_subsystem = "windows"]
mod migration;
mod platform;
mod sqlite;
use platform::{resolved, same_path};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::ffi::OsString;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};
use std::{env, fs, io, process};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const DB: &str = "holdexar.db";
fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn unique() -> String {
    format!(
        "{}-{}",
        process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    )
}
fn path_string(path: &Path) -> Result<&str> {
    path.to_str().ok_or_else(|| "路径包含非Unicode字符".into())
}
fn read_json(path: &Path) -> Result<Option<Value>> {
    match fs::read(path) {
        Ok(data) => Ok(Some(serde_json::from_slice(&data)?)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}
fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    let parent = path.parent().ok_or("缺少父目录")?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        "{}.{}.tmp",
        path.file_name().unwrap().to_string_lossy(),
        unique()
    ));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec_pretty(value)?)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)?;
        Ok(())
    })();
    let _ = fs::remove_file(temporary);
    result
}
fn managed_name(name: &str) -> bool {
    !name.is_empty()
        && name.trim() == name
        && !name.ends_with('.')
        && !name.contains(['/', '\\', ':'])
        && (name.chars().next().is_some_and(|c| c.is_numeric()) || name == "legacy_version")
}
fn version_dir(root: &Path, version: &str) -> Result<PathBuf> {
    if !managed_name(version) {
        return Err("版本目录名称无效".into());
    }
    let versions = root.join("versions");
    let directory = versions.join(version);
    if !same_path(&resolved(&versions)?, &versions)
        || !same_path(&resolved(&directory)?, &directory)
    {
        return Err("版本目录超出受管安装位置".into());
    }
    Ok(directory)
}
fn version_valid(root: &Path, version: &str) -> bool {
    (|| -> Result<bool> {
        let directory = version_dir(root, version)?;
        let exe = directory.join("Holdexar.exe");
        let internal = directory.join("_internal");
        if !directory.is_dir()
            || !exe.is_file()
            || !same_path(&resolved(&exe)?, &exe)
            || !same_path(&resolved(&internal)?, &internal)
        {
            return Ok(false);
        }
        Ok(!internal.exists()
            || internal.is_dir() && fs::read_dir(internal)?.next().transpose()?.is_some())
    })()
    .unwrap_or(false)
}
fn current(root: &Path) -> Result<Option<Value>> {
    let value = read_json(&root.join("state/current.json"))?;
    if value.as_ref().is_some_and(|value| {
        !value.is_object()
            || value
                .get("version")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
    }) {
        return Err("程序指向记录无法读取，已停止选择程序".into());
    }
    Ok(value)
}
struct Compatibility {
    data: PathBuf,
    schema: i64,
    versions: BTreeSet<String>,
}
fn compatibility(state: Option<&Value>) -> Result<Option<Compatibility>> {
    let Some(value) = state.and_then(|s| s.get("dataCompatibility")) else {
        return Ok(None);
    };
    let data = value
        .get("dataDir")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or("数据相容记录无效")?;
    let data = Path::new(data);
    if !data.is_absolute() {
        return Err("数据相容目录不是绝对路径".into());
    }
    let schema = value
        .get("schemaVersion")
        .and_then(Value::as_i64)
        .filter(|v| *v >= 0)
        .ok_or("数据结构版本无效")?;
    let versions = value
        .get("versions")
        .and_then(Value::as_array)
        .filter(|v| !v.is_empty())
        .ok_or("相容版本清单无效")?;
    let mut allowed = BTreeSet::new();
    for value in versions {
        allowed.insert(
            value
                .as_str()
                .filter(|v| managed_name(v))
                .ok_or("相容版本名称无效")?
                .to_owned(),
        );
    }
    Ok(Some(Compatibility {
        data: resolved(data)?,
        schema,
        versions: allowed,
    }))
}
fn write_current(root: &Path, version: &str, old: Option<&Value>) -> Result<()> {
    let mut value = json!({"version":version,"installedAt":now(),"lastKnownGood":null});
    if let Some(compat) = old.and_then(|o| o.get("dataCompatibility")) {
        value["dataCompatibility"] = compat.clone();
    }
    atomic_json(&root.join("state/current.json"), &value)
}
fn find_version(
    root: &Path,
    state: Option<&Value>,
    compat: Option<&Compatibility>,
) -> Result<String> {
    if let Some(c) = compat {
        if sqlite::schema_version(&c.data.join(DB))? != c.schema {
            return Err("数据结构已变化，拒绝选择未经相容验收的程序".into());
        }
    }
    let usable = |v: &str| version_valid(root, v) && compat.is_none_or(|c| c.versions.contains(v));
    if let Some(state) = state {
        for key in ["version", "lastKnownGood", "previousVersion"] {
            if let Some(v) = state.get(key).and_then(Value::as_str).filter(|v| usable(v)) {
                if key != "version" {
                    write_current(root, v, Some(state))?;
                }
                return Ok(v.to_owned());
            }
        }
    }
    let mut installed = Vec::new();
    for entry in fs::read_dir(root.join("versions"))? {
        let entry = entry?;
        if let Some(v) = entry.file_name().to_str().filter(|v| usable(v)) {
            installed.push((entry.metadata()?.modified()?, v.to_owned()));
        }
    }
    let best = installed
        .into_iter()
        .max_by_key(|(time, _)| *time)
        .ok_or("没有可运行的有效版本")?
        .1;
    write_current(root, &best, state)?;
    Ok(best)
}
fn recovery_needed(tx: Option<&Value>) -> bool {
    tx.is_some_and(|tx| {
        tx["dataRollbackReleased"] == true
            || tx["dataAccepted"] == true
            || tx["dataPrepared"] == true
            || tx["dataDir"].as_str().is_some_and(|dir| !dir.is_empty())
    })
}
fn recovery_helper(root: &Path) -> Result<PathBuf> {
    let expected = root.join(".launcher").join("SafetyHost.exe");
    if !same_path(&resolved(&expected)?, &expected) || !expected.is_file() {
        return Err("更新恢复保护组件缺失或被重定向，数据已保留".into());
    }
    Ok(expected)
}
fn launch_recovery(root: &Path, args: &[OsString]) -> Result<i32> {
    Command::new(recovery_helper(root)?)
        .args(args)
        .env("HOLDEXAR_INSTALL_ROOT", root)
        .status()?
        .code()
        .ok_or_else(|| "更新恢复组件异常退出".into())
}
fn cleanup_old(directory: &Path) {
    if let Ok(entries) = fs::read_dir(directory) {
        for entry in entries.flatten() {
            if entry
                .file_name()
                .to_str()
                .is_some_and(|n| n.contains(".old_"))
                && entry.path().is_file()
            {
                let _ = fs::remove_file(entry.path());
            }
        }
    }
}
fn launch() -> Result<i32> {
    let args: Vec<OsString> = env::args_os().skip(1).collect();
    if args.iter().any(|arg| arg == "--update-protocol") {
        let payload = serde_json::to_vec(&json!({"updateDataProtocol":1}))?;
        if let Some(path) = env::var_os("HOLDEXAR_UPDATE_PROTOCOL_RESULT") {
            fs::write(path, payload)?;
        } else {
            println!("{}", String::from_utf8(payload)?);
        }
        return Ok(0);
    }
    let executable = resolved(&env::current_exe()?)?;
    let root = executable.parent().ok_or("无法获取安装目录")?;
    let lock = platform::UpdateMutex::acquire()?;
    let tx = read_json(&root.join("state/transaction.json"))?;
    if recovery_needed(tx.as_ref()) {
        drop(lock);
        return launch_recovery(root, &args);
    }
    drop(lock);
    let state = current(root)?;
    let compat = compatibility(state.as_ref())?;
    for dir in [
        "state",
        "data",
        "logs",
        "versions",
        "updater",
        "update-staging",
    ] {
        fs::create_dir_all(root.join(dir))?;
    }
    cleanup_old(&root.join("updater"));
    cleanup_old(root);
    if compat.is_none()
        && tx
            .as_ref()
            .and_then(|t| t["dataDir"].as_str())
            .is_none_or(str::is_empty)
    {
        migration::migrate(root)?;
    }
    let version = find_version(root, state.as_ref(), compat.as_ref())?;
    let data = compat
        .as_ref()
        .map(|c| c.data.clone())
        .or_else(|| {
            tx.as_ref()
                .and_then(|t| t["dataDir"].as_str())
                .map(PathBuf::from)
        })
        .unwrap_or_else(|| root.join("data"));
    let data = resolved(&data)?;
    let exe = version_dir(root, &version)?.join("Holdexar.exe");
    let mut child = Command::new(&exe);
    child.args(&args).current_dir(exe.parent().unwrap());
    for key in [
        "UPDATE_ATTEMPT_ID",
        "UPDATE_DATA_TOKEN",
        "LAUNCHER_PID",
        "DEV_URL",
        "UPDATE_RESTART",
        "PREV_PID",
    ] {
        child.env_remove(format!("HOLDEXAR_{key}"));
    }
    child
        .env("HOLDEXAR_INSTALL_ROOT", root)
        .env("HOLDEXAR_DATA_DIR", &data)
        .env("WEBVIEW2_USER_DATA_FOLDER", data.join("cache/webview"));
    if args.iter().any(|a| a == "--server") {
        return child
            .status()?
            .code()
            .ok_or_else(|| "主程序异常退出".into());
    }
    child.creation_flags(0x8 | 0x200).spawn()?;
    Ok(0)
}
fn main() {
    let code = match launch() {
        Ok(code) => code,
        Err(error) => {
            platform::fatal(&error.to_string());
            1
        }
    };
    process::exit(code);
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recovery_helper_uses_native_components() {
        let root = env::temp_dir().join(format!("holdexar-rust-helper-{}", unique()));
        fs::create_dir_all(root.join(".launcher")).unwrap();
        let expected = root.join(".launcher").join("SafetyHost.exe");
        fs::write(&expected, []).unwrap();
        assert_eq!(
            recovery_helper(&resolved(&root).unwrap()).unwrap(),
            resolved(&expected).unwrap()
        );
        fs::remove_file(expected).unwrap();
        fs::remove_dir(root.join(".launcher")).unwrap();
        fs::remove_dir(root).unwrap();
    }
    #[test]
    fn names() {
        for n in ["0.3.8", "1-test", "legacy_version"] {
            assert!(managed_name(n));
        }
        for n in [
            "", "../0.3.8", r"E:\evil", "data", "0.3.8.", " 0.3.8", "0.3.8 ",
        ] {
            assert!(!managed_name(n));
        }
    }
    #[test]
    fn invalid_compat() {
        for schema in [json!(true), json!(-1), json!("32")] {
            let v = json!({"dataCompatibility":{"dataDir":r"E:\data","schemaVersion":schema,"versions":["0.3.8"]}});
            assert!(compatibility(Some(&v)).is_err());
        }
    }
    #[test]
    fn recovery() {
        assert!(recovery_needed(Some(&json!({"dataPrepared":true}))));
        assert!(recovery_needed(Some(
            &json!({"dataAccepted":true,"stage":"committed"})
        )));
        assert!(!recovery_needed(Some(&json!({"stage":"committed"}))));
    }
}
