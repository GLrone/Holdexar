#![windows_subsystem = "windows"]

use std::env;
use std::ffi::c_void;
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::process::{self, Command};

#[link(name = "user32")]
unsafe extern "system" {
    #[link_name = "MessageBoxW"]
    fn message_box(owner: *mut c_void, text: *const u16, title: *const u16, kind: u32) -> i32;
}

fn show_fatal(message: &str) {
    eprintln!("{message}");
    if env::var("HOLDEXAR_NO_DIALOG").as_deref() == Ok("1")
        || env::var("CI").as_deref() == Ok("1")
    {
        return;
    }
    let text: Vec<u16> = std::ffi::OsStr::new(message).encode_wide().chain([0]).collect();
    let title: Vec<u16> = "Holdexar 启动失败".encode_utf16().chain([0]).collect();
    unsafe { message_box(std::ptr::null_mut(), text.as_ptr(), title.as_ptr(), 0x10); }
}

fn display_path(path: PathBuf) -> PathBuf {
    // Windows canonicalize() returns \\?\-prefixed device paths; a drive path
    // with '?' breaks downstream file: URI handling in the protection core,
    // so strip the prefix (UNC device paths stay untouched).
    let text = path.as_os_str().to_string_lossy();
    match text.strip_prefix(r"\\?\") {
        Some(rest) if rest.as_bytes().get(1) == Some(&b':') => PathBuf::from(rest),
        _ => path,
    }
}

fn helper_path(root: &Path) -> io::Result<PathBuf> {
    let expected = root.join(".launcher").join("SafetyHost.exe");
    let resolved = display_path(expected.canonicalize().map_err(|error| {
        io::Error::new(error.kind(), format!("启动保护组件缺失，请重新解压完整安装包：{error}"))
    })?);
    if resolved != expected {
        return Err(io::Error::other("启动保护组件路径被重定向，已停止启动"));
    }
    if !resolved.is_file() {
        return Err(io::Error::other("启动保护组件不是文件"));
    }
    Ok(resolved)
}

fn launch() -> io::Result<i32> {
    let executable = display_path(env::current_exe()?.canonicalize()?);
    let root = executable.parent().ok_or_else(|| io::Error::other("无法获取安装目录"))?;
    let helper = helper_path(root)?;
    let status = Command::new(helper)
        .args(env::args_os().skip(1))
        .env("HOLDEXAR_INSTALL_ROOT", root)
        .status()?;
    status.code().ok_or_else(|| io::Error::other("启动保护组件异常退出"))
}

fn main() {
    let code = match launch() {
        Ok(code) => code,
        Err(error) => {
            show_fatal(&error.to_string());
            1
        }
    };
    process::exit(code);
}
