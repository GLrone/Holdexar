use crate::Result;
use std::ffi::{OsStr, OsString, c_void};
use std::io;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateMutexW(attributes: *mut c_void, owner: i32, name: *const u16) -> *mut c_void;
    fn WaitForSingleObject(handle: *mut c_void, timeout: u32) -> u32;
    fn ReleaseMutex(handle: *mut c_void) -> i32;
    fn CloseHandle(handle: *mut c_void) -> i32;
    fn MoveFileW(source: *const u16, target: *const u16) -> i32;
    fn CompareStringOrdinal(
        first: *const u16,
        first_len: i32,
        second: *const u16,
        second_len: i32,
        ignore_case: i32,
    ) -> i32;
}
#[link(name = "user32")]
unsafe extern "system" {
    fn MessageBoxW(owner: *mut c_void, text: *const u16, title: *const u16, kind: u32) -> i32;
}
fn wide(text: &OsStr) -> Vec<u16> {
    text.encode_wide().chain([0]).collect()
}
pub struct UpdateMutex(*mut c_void);
impl UpdateMutex {
    pub fn acquire() -> Result<Self> {
        let handle = unsafe {
            CreateMutexW(
                std::ptr::null_mut(),
                0,
                wide(OsStr::new("Holdexar-update-swap")).as_ptr(),
            )
        };
        if handle.is_null() {
            return Err(io::Error::last_os_error().into());
        }
        if !matches!(unsafe { WaitForSingleObject(handle, 0) }, 0 | 0x80) {
            unsafe {
                CloseHandle(handle);
            }
            return Err("已有更新正在进行或无法核对更新权限".into());
        }
        Ok(Self(handle))
    }
}
impl Drop for UpdateMutex {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.0);
            CloseHandle(self.0);
        }
    }
}
pub fn fatal(message: &str) {
    eprintln!("{message}");
    if std::env::var("HOLDEXAR_NO_DIALOG").as_deref() == Ok("1")
        || std::env::var("CI").as_deref() == Ok("1")
        || std::env::args_os().any(|a| a == "--server")
    {
        return;
    }
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            wide(OsStr::new(message)).as_ptr(),
            wide(OsStr::new("Holdexar 启动失败")).as_ptr(),
            0x10,
        );
    }
}
pub fn display_path(path: PathBuf) -> PathBuf {
    let units: Vec<u16> = path.as_os_str().encode_wide().collect();
    let prefix: Vec<u16> = r"\\?\".encode_utf16().collect();
    if units.starts_with(&prefix) {
        if units.get(5) == Some(&(b':' as u16)) {
            return OsString::from_wide(&units[4..]).into();
        }
        let unc: Vec<u16> = r"UNC\".encode_utf16().collect();
        if units[4..].starts_with(&unc) {
            let mut normal: Vec<u16> = r"\\".encode_utf16().collect();
            normal.extend_from_slice(&units[8..]);
            return OsString::from_wide(&normal).into();
        }
    }
    path
}
pub fn resolved(path: &Path) -> io::Result<PathBuf> {
    match path.canonicalize() {
        Ok(path) => Ok(display_path(path)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let absolute = std::path::absolute(path)?;
            let parent = absolute.parent().ok_or(error)?;
            Ok(resolved(parent)?.join(
                absolute
                    .file_name()
                    .ok_or_else(|| io::Error::other("无效路径"))?,
            ))
        }
        Err(error) => Err(error),
    }
}
pub fn same_path(first: &Path, second: &Path) -> bool {
    let first: Vec<u16> = first.as_os_str().encode_wide().collect();
    let second: Vec<u16> = second.as_os_str().encode_wide().collect();
    unsafe {
        CompareStringOrdinal(
            first.as_ptr(),
            first.len() as i32,
            second.as_ptr(),
            second.len() as i32,
            1,
        ) == 2
    }
}
pub fn install_missing(source: &Path, target: &Path) -> io::Result<()> {
    // MoveFileW refuses to replace an existing target on every supported Windows filesystem.
    if unsafe {
        MoveFileW(
            wide(source.as_os_str()).as_ptr(),
            wide(target.as_os_str()).as_ptr(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_device_paths() {
        assert_eq!(
            display_path(PathBuf::from(r"\\?\E:\中文 安装\app")),
            PathBuf::from(r"E:\中文 安装\app")
        );
        assert_eq!(
            display_path(PathBuf::from(r"\\?\UNC\server\share\中文")),
            PathBuf::from(r"\\server\share\中文")
        );
    }
    #[test]
    fn path_case() {
        assert!(same_path(
            Path::new(r"E:\中文\Foo"),
            Path::new(r"e:\中文\foo")
        ));
        assert!(!same_path(
            Path::new(r"E:\app\0.3.7"),
            Path::new(r"E:\app\0.3.8")
        ));
    }
}
