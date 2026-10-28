use crate::platform::{install_missing, same_path};
use crate::{DB, Result, atomic_json, now, path_string, read_json, resolved, sqlite, unique};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::fs::OpenOptions;
use std::path::{Path, PathBuf};
use std::{env, fs, io};
const AUX: [&str; 4] = ["config", "seed", "backups", "logs"];
fn candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    for key in ["LOCALAPPDATA", "APPDATA"] {
        if let Some(base) = env::var_os(key).filter(|v| !v.is_empty()) {
            out.push(PathBuf::from(base).join("holdexar"));
        }
    }
    if let Some(base) = env::var_os("USERPROFILE")
        .filter(|v| !v.is_empty())
        .or_else(|| env::var_os("HOME"))
    {
        out.push(PathBuf::from(base).join(".holdexar"));
    }
    out
}
fn strings(value: &Value, key: &str) -> BTreeSet<String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(|v| {
            v.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}
fn transient(name: &OsStr) -> bool {
    name.to_str()
        .is_some_and(|n| [".lock", ".tmp", ".part"].iter().any(|s| n.ends_with(s)))
}
fn copy_missing(source: &Path, target: &Path) -> Result<()> {
    if !same_path(&resolved(source)?, source) || !same_path(&resolved(target)?, target) {
        return Err("迁移附件不能通过链接指向其他位置".into());
    }
    let secret = source
        .file_name()
        .and_then(OsStr::to_str)
        .is_some_and(|n| n.to_ascii_lowercase().starts_with("secret."));
    if target.exists() {
        if !target.is_file() {
            return Err("迁移附件目标不是普通文件".into());
        }
        if secret && fs::read(source)? != fs::read(target)? {
            return Err("目标已有不同的密钥文件".into());
        }
        return Ok(());
    }
    let temp = target.with_file_name(format!(
        "{}.{}.tmp",
        target.file_name().unwrap().to_string_lossy(),
        unique()
    ));
    let result = (|| -> Result<()> {
        let mut input = fs::File::open(source)?;
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)?;
        io::copy(&mut input, &mut output)?;
        output.sync_all()?;
        drop(output);
        match install_missing(&temp, target) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                if !target.is_file() || (secret && fs::read(source)? != fs::read(target)?) {
                    return Err("并发迁移目标冲突".into());
                }
                Ok(())
            }
            Err(error) => Err(error.into()),
        }
    })();
    let _ = fs::remove_file(temp);
    result
}
fn copy_tree(source: &Path, target: &Path) -> Result<()> {
    if !same_path(&resolved(source)?, source) || !same_path(&resolved(target)?, target) {
        return Err("迁移附件目录不能重定向".into());
    }
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        if transient(&entry.file_name()) {
            continue;
        }
        let destination = target.join(entry.file_name());
        if entry.path().is_dir() {
            copy_tree(&entry.path(), &destination)?;
        } else {
            copy_missing(&entry.path(), &destination)?;
        }
    }
    Ok(())
}
fn finish(source: &Path, target: &Path, mut meta: Value) -> Result<()> {
    let mut required_dirs = strings(&meta, "requiredDirs");
    let mut keys = strings(&meta, "requiredKeys");
    let mut copied = strings(&meta, "copiedSubdirs");
    for sub in AUX {
        if sub != "seed" && source.join(sub).is_dir() {
            required_dirs.insert(sub.to_owned());
        }
    }
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        if entry.path().is_file() && !transient(&entry.file_name()) {
            if let Some(name) = entry
                .file_name()
                .to_str()
                .filter(|n| n.to_ascii_lowercase().starts_with("secret."))
            {
                keys.insert(name.to_owned());
            }
        }
    }
    let mut warnings = Vec::new();
    for sub in AUX {
        if !source.join(sub).is_dir() {
            if required_dirs.contains(sub) {
                warnings.push(format!("原始辅助目录 {sub} 暂不可用"));
            }
            continue;
        }
        match copy_tree(&source.join(sub), &target.join(sub)) {
            Ok(()) => {
                copied.insert(sub.to_owned());
            }
            Err(error) if sub != "seed" => {
                warnings.push(format!("辅助目录 {sub} 未完整复制：{error}"))
            }
            Err(_) => {}
        }
    }
    for name in &keys {
        if let Err(error) = copy_missing(&source.join(name), &target.join(name)) {
            warnings.push(format!("密钥文件 {name} 未完整复制：{error}"));
        }
    }
    meta["sourceDir"] = json!(path_string(source)?);
    meta["targetDir"] = json!(path_string(target)?);
    meta["database"] = json!(DB);
    meta["copiedSubdirs"] = json!(copied);
    meta["requiredDirs"] = json!(required_dirs);
    meta["requiredKeys"] = json!(keys);
    meta["warnings"] = json!(warnings);
    meta["status"] = json!(if warnings.is_empty() {
        "completed"
    } else {
        "incomplete"
    });
    meta["completed"] = json!(warnings.is_empty());
    atomic_json(&target.join("migration.json"), &meta)?;
    fs::write(
        target.join("MIGRATION_FROM_APPDATA.txt"),
        format!(
            "Holdexar 数据迁移说明\n来源：{}\n目标：{}\n数据库已生成安全副本；原始数据目录保持完整，程序不会自动删除。\n{}\n",
            source.display(),
            target.display(),
            warnings.join("\n")
        ),
    )?;
    if !warnings.is_empty() {
        return Err("旧数据迁移未完成，保留来源供下次接续".into());
    }
    Ok(())
}
pub fn migrate(root: &Path) -> Result<()> {
    let target = resolved(&root.join("data"))?;
    let meta = read_json(&target.join("migration.json"))
        .ok()
        .flatten()
        .filter(Value::is_object)
        .unwrap_or(json!({}));
    let pending = meta["status"] == "incomplete"
        && meta["targetDir"] == path_string(&target)?
        && meta["database"] == DB;
    let mut source = candidates().into_iter().find(|c| c.join(DB).is_file());
    if source.is_none() && pending {
        source = meta["sourceDir"]
            .as_str()
            .map(PathBuf::from)
            .filter(|p| p.is_absolute());
    }
    let Some(source) = source else { return Ok(()) };
    let source = resolved(&source)?;
    if source == target {
        return Ok(());
    }
    if target.join(DB).exists() {
        if !source.join(DB).is_file() && !pending {
            return Ok(());
        }
        if !source.join(DB).is_file() {
            return Err("原迁移来源暂不可用，保留未完成记录".into());
        }
        let reused = ["completed", "incomplete"]
            .iter()
            .any(|s| meta["status"] == *s)
            && meta["sourceDir"] == path_string(&source)?
            && meta["targetDir"] == path_string(&target)?
            && meta["database"] == DB
            && sqlite::quick_check(&target.join(DB))?;
        if !reused {
            return Err("两处存在独立数据，均已保留，已停止启动以避免覆盖".into());
        }
        let missing = fs::read_dir(&source)?
            .filter_map(std::result::Result::ok)
            .any(|e| {
                e.path().is_file()
                    && e.file_name()
                        .to_str()
                        .is_some_and(|n| n.to_ascii_lowercase().starts_with("secret."))
                    && !target.join(e.file_name()).is_file()
            });
        if pending || meta["completed"] != true || missing {
            finish(&source, &target, meta)?;
        }
        return Ok(());
    }
    if !source.join(DB).is_file() {
        return Err("未找到原始数据库".into());
    }
    let temp = target.join(format!("{DB}.{}.migrating.tmp", unique()));
    let snapshot = (|| -> Result<()> {
        sqlite::backup(&source.join(DB), &temp)?;
        if !sqlite::quick_check(&temp)? {
            return Err("迁移数据库完整性校验失败".into());
        }
        OpenOptions::new()
            .read(true)
            .write(true)
            .open(&temp)?
            .sync_all()?;
        install_missing(&temp, &target.join(DB))?;
        Ok(())
    })();
    let _ = fs::remove_file(temp);
    snapshot?;
    finish(&source, &target, json!({"migratedAt":now()}))
}
