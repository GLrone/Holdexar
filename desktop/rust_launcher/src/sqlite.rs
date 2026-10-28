use crate::Result;
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::path::Path;
use std::time::{Duration, Instant};

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LoadLibraryExW(name: *const u16, file: *mut c_void, flags: u32) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const c_char) -> *mut c_void;
}
type Open = unsafe extern "C" fn(*const c_char, *mut *mut c_void, c_int, *const c_char) -> c_int;
type Close = unsafe extern "C" fn(*mut c_void) -> c_int;
type Error = unsafe extern "C" fn(*mut c_void) -> *const c_char;
type Prepare = unsafe extern "C" fn(
    *mut c_void,
    *const c_char,
    c_int,
    *mut *mut c_void,
    *mut *const c_char,
) -> c_int;
type Step = unsafe extern "C" fn(*mut c_void) -> c_int;
type Text = unsafe extern "C" fn(*mut c_void, c_int) -> *const u8;
type Finalize = unsafe extern "C" fn(*mut c_void) -> c_int;
type BackupInit =
    unsafe extern "C" fn(*mut c_void, *const c_char, *mut c_void, *const c_char) -> *mut c_void;
type BackupStep = unsafe extern "C" fn(*mut c_void, c_int) -> c_int;
type BackupFinish = unsafe extern "C" fn(*mut c_void) -> c_int;
struct Api {
    open: Open,
    close: Close,
    error: Error,
    prepare: Prepare,
    step: Step,
    text: Text,
    finalize: Finalize,
    backup_init: BackupInit,
    backup_step: BackupStep,
    backup_finish: BackupFinish,
}
impl Api {
    fn load() -> Result<Self> {
        // Only load the Windows platform SQLite, never a DLL from the installation directory.
        let name: Vec<u16> = "winsqlite3.dll".encode_utf16().chain([0]).collect();
        let module = unsafe { LoadLibraryExW(name.as_ptr(), std::ptr::null_mut(), 0x800) };
        if module.is_null() {
            return Err(std::io::Error::last_os_error().into());
        }
        macro_rules! proc {
            ($name:literal,$kind:ty) => {{
                let address =
                    unsafe { GetProcAddress(module, concat!($name, "\0").as_ptr().cast()) };
                if address.is_null() {
                    return Err(concat!("系统SQLite缺少API：", $name).into());
                }
                unsafe { std::mem::transmute::<*mut c_void, $kind>(address) }
            }};
        }
        Ok(Self {
            open: proc!("sqlite3_open_v2", Open),
            close: proc!("sqlite3_close", Close),
            error: proc!("sqlite3_errmsg", Error),
            prepare: proc!("sqlite3_prepare_v2", Prepare),
            step: proc!("sqlite3_step", Step),
            text: proc!("sqlite3_column_text", Text),
            finalize: proc!("sqlite3_finalize", Finalize),
            backup_init: proc!("sqlite3_backup_init", BackupInit),
            backup_step: proc!("sqlite3_backup_step", BackupStep),
            backup_finish: proc!("sqlite3_backup_finish", BackupFinish),
        })
    }
}
struct Database<'a> {
    api: &'a Api,
    handle: *mut c_void,
}
impl<'a> Database<'a> {
    fn open(api: &'a Api, path: &Path, flags: i32) -> Result<Self> {
        let filename = CString::new(path.to_str().ok_or("数据库路径包含非Unicode字符")?)?;
        let mut handle = std::ptr::null_mut();
        let code = unsafe { (api.open)(filename.as_ptr(), &mut handle, flags, std::ptr::null()) };
        let db = Self { api, handle };
        if code != 0 {
            return Err(db.error().into());
        }
        Ok(db)
    }
    fn error(&self) -> String {
        unsafe {
            CStr::from_ptr((self.api.error)(self.handle))
                .to_string_lossy()
                .into_owned()
        }
    }
    fn query(&self, sql: &str) -> Result<String> {
        let sql = CString::new(sql)?;
        let mut statement = std::ptr::null_mut();
        if unsafe {
            (self.api.prepare)(
                self.handle,
                sql.as_ptr(),
                -1,
                &mut statement,
                std::ptr::null_mut(),
            )
        } != 0
        {
            return Err(self.error().into());
        }
        let result = if unsafe { (self.api.step)(statement) } == 100 {
            let text = unsafe { (self.api.text)(statement, 0) };
            if text.is_null() {
                Err("SQLite结果为空".into())
            } else {
                Ok(unsafe { CStr::from_ptr(text.cast()) }.to_str()?.to_owned())
            }
        } else {
            Err(self.error().into())
        };
        unsafe {
            (self.api.finalize)(statement);
        }
        result
    }
}
impl Drop for Database<'_> {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe {
                (self.api.close)(self.handle);
            }
        }
    }
}
pub fn schema_version(path: &Path) -> Result<i64> {
    let api = Api::load()?;
    Database::open(&api, path, 1)?
        .query("PRAGMA user_version")?
        .parse()
        .map_err(Into::into)
}
pub fn quick_check(path: &Path) -> Result<bool> {
    let api = Api::load()?;
    Ok(Database::open(&api, path, 1)?.query("PRAGMA quick_check")? == "ok")
}
pub fn backup(source: &Path, target: &Path) -> Result<()> {
    let api = Api::load()?;
    let source = Database::open(&api, source, 1)?;
    let target = Database::open(&api, target, 2 | 4)?;
    let handle = unsafe {
        (api.backup_init)(
            target.handle,
            c"main".as_ptr(),
            source.handle,
            c"main".as_ptr(),
        )
    };
    if handle.is_null() {
        return Err(target.error().into());
    }
    let deadline = Instant::now() + Duration::from_secs(30);
    let code = loop {
        let code = unsafe { (api.backup_step)(handle, -1) };
        if !matches!(code, 5 | 6) || Instant::now() >= deadline {
            break code;
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    let finish = unsafe { (api.backup_finish)(handle) };
    if code != 101 || finish != 0 {
        return Err(target.error().into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn platform_backup() {
        let directory =
            std::env::temp_dir().join(format!("holdexar-rust-sqlite-{}", crate::unique()));
        std::fs::create_dir(&directory).unwrap();
        let source = directory.join("source.db");
        let target = directory.join("copy.db");
        let api = Api::load().unwrap();
        let db = Database::open(&api, &source, 2 | 4).unwrap();
        let sql = CString::new("PRAGMA user_version=32").unwrap();
        let mut statement = std::ptr::null_mut();
        assert_eq!(
            unsafe {
                (api.prepare)(
                    db.handle,
                    sql.as_ptr(),
                    -1,
                    &mut statement,
                    std::ptr::null_mut(),
                )
            },
            0
        );
        assert_eq!(unsafe { (api.step)(statement) }, 101);
        unsafe {
            (api.finalize)(statement);
        }
        drop(db);
        assert_eq!(schema_version(&source).unwrap(), 32);
        backup(&source, &target).unwrap();
        assert_eq!(schema_version(&target).unwrap(), 32);
        assert!(quick_check(&target).unwrap());
        std::fs::remove_file(source).unwrap();
        std::fs::remove_file(target).unwrap();
        std::fs::remove_dir(directory).unwrap();
    }
}
