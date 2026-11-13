//! Host memory counters for `armless-exec --memstats`, kept where the state changes (relaxed
//! atomics: a store or an add each), and the process-level calls that go with them.

use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

/// Decompressed blobs the file system holds: bytes now, the most it held, and the blobs.
pub static FS_CACHE_BYTES: AtomicU64 = AtomicU64::new(0);
pub static FS_CACHE_PEAK: AtomicU64 = AtomicU64::new(0);
pub static FS_CACHE_ENTRIES: AtomicU64 = AtomicU64::new(0);
/// Bytes that entered the cache since the start, blobs that did, blobs evicted.
pub static FS_BYTES_IN: AtomicU64 = AtomicU64::new(0);
pub static FS_FETCHES: AtomicU64 = AtomicU64::new(0);
pub static FS_EVICTIONS: AtomicU64 = AtomicU64::new(0);
/// Inodes the file system holds.
pub static FS_INODES: AtomicU64 = AtomicU64::new(0);
/// Slots of decoded pages allocated, and the bytes of their ops.
pub static PREDECODE_SLOTS: AtomicU64 = AtomicU64::new(0);
pub static PREDECODE_BYTES: AtomicU64 = AtomicU64::new(0);
/// Guest frames in use.
pub static FRAMES_LIVE: AtomicU64 = AtomicU64::new(0);
/// File pages filled into a fresh frame, and the bytes copied into them.
pub static POPULATE_FAULTS: AtomicU64 = AtomicU64::new(0);
pub static POPULATE_BYTES: AtomicU64 = AtomicU64::new(0);

/// The counters, named, as one list (glibc's `mallinfo2` fields come last, 0 elsewhere).
pub fn counters() -> [(&'static str, u64); 16] {
    let m = mallinfo();
    [
        ("fs_cache_bytes", FS_CACHE_BYTES.load(Relaxed)),
        ("fs_cache_peak", FS_CACHE_PEAK.load(Relaxed)),
        ("fs_cache_entries", FS_CACHE_ENTRIES.load(Relaxed)),
        ("fs_bytes_in", FS_BYTES_IN.load(Relaxed)),
        ("fs_fetches", FS_FETCHES.load(Relaxed)),
        ("fs_evictions", FS_EVICTIONS.load(Relaxed)),
        ("fs_inodes", FS_INODES.load(Relaxed)),
        ("predecode_slots", PREDECODE_SLOTS.load(Relaxed)),
        ("predecode_bytes", PREDECODE_BYTES.load(Relaxed)),
        ("frames_live", FRAMES_LIVE.load(Relaxed)),
        ("populate_faults", POPULATE_FAULTS.load(Relaxed)),
        ("populate_bytes", POPULATE_BYTES.load(Relaxed)),
        ("malloc_arena", m[0]),
        ("malloc_uordblks", m[1]),
        ("malloc_fordblks", m[2]),
        ("malloc_hblkhd", m[3]),
    ]
}

/// glibc's arena, uordblks, fordblks and hblkhd.
#[cfg(all(target_os = "linux", target_env = "gnu"))]
#[allow(unsafe_code)]
fn mallinfo() -> [u64; 4] {
    // SAFETY: only reads the allocator's own totals.
    let m = unsafe { libc::mallinfo2() };
    [
        m.arena as u64,
        m.uordblks as u64,
        m.fordblks as u64,
        m.hblkhd as u64,
    ]
}

#[cfg(not(all(target_os = "linux", target_env = "gnu")))]
fn mallinfo() -> [u64; 4] {
    [0; 4]
}

/// Hands memory the allocator holds free back to the system: malloc keeps the pages of what is
/// freed, and counts them as the process's, until asked.
#[allow(unsafe_code)]
pub fn return_freed_memory() {
    #[cfg(target_os = "macos")]
    {
        unsafe extern "C" {
            fn malloc_zone_pressure_relief(zone: *mut std::ffi::c_void, goal: usize) -> usize;
        }
        // SAFETY: a null zone means every zone; a zero goal, as much as it can.
        unsafe { malloc_zone_pressure_relief(std::ptr::null_mut(), 0) };
    }
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    // SAFETY: only returns free memory to the system.
    unsafe {
        libc::malloc_trim(0);
    }
}

/// Blocks SIGTERM and SIGINT in the calling thread, and so in the threads it makes afterwards.
#[cfg(unix)]
#[allow(unsafe_code)]
pub fn block_termination() {
    // SAFETY: a signal set made here, blocked for this thread.
    unsafe {
        let mut set = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGTERM);
        libc::sigaddset(&mut set, libc::SIGINT);
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
    }
}

/// Waits for SIGTERM or SIGINT (blocked by `block_termination` in every thread) and returns it.
#[cfg(unix)]
#[allow(unsafe_code)]
pub fn wait_termination() -> i32 {
    // SAFETY: waits on the set `block_termination` blocked.
    unsafe {
        let mut set = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGTERM);
        libc::sigaddset(&mut set, libc::SIGINT);
        let mut sig = 0;
        libc::sigwait(&set, &mut sig);
        sig
    }
}

/// Ends the process now, without running anything at exit.
#[cfg(unix)]
#[allow(unsafe_code)]
pub fn exit_now(code: i32) -> ! {
    // SAFETY: ends the process.
    unsafe { libc::_exit(code) }
}
