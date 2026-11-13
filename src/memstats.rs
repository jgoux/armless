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

// Start-up time. Nanoseconds are cumulative, summed over the threads that spend them (not wall time).
/// zstd blobs decompressed (natively: `zstd::decompress`), their compressed bytes, the time, and
/// the bytes and allocated capacity of the Vec it returns (which the file system keeps as it is).
pub static ZSTD_CALLS: AtomicU64 = AtomicU64::new(0);
pub static ZSTD_IN_BYTES: AtomicU64 = AtomicU64::new(0);
pub static ZSTD_NS: AtomicU64 = AtomicU64::new(0);
pub static ZSTD_OUT_BYTES: AtomicU64 = AtomicU64::new(0);
pub static ZSTD_OUT_CAPACITY: AtomicU64 = AtomicU64::new(0);
/// Blobs that came raw (not zstd) into `Board::provide_blob`, with the same two sizes of their Vec.
pub static RAW_BLOBS: AtomicU64 = AtomicU64::new(0);
pub static RAW_BYTES: AtomicU64 = AtomicU64::new(0);
pub static RAW_CAPACITY: AtomicU64 = AtomicU64::new(0);
/// `Board::provide_blob`: time to get the device lock, and time it is held (decompression and
/// the file system's insert are inside it).
pub static DEV_LOCK_WAIT_NS: AtomicU64 = AtomicU64::new(0);
pub static DEV_LOCK_HOLD_NS: AtomicU64 = AtomicU64::new(0);
/// The native host's `armless-host` thread: blobs it fetched (read from the directory), their
/// bytes (as stored: compressed), and the time of the reads.
pub static HOST_FETCHES: AtomicU64 = AtomicU64::new(0);
pub static HOST_FETCH_BYTES: AtomicU64 = AtomicU64::new(0);
pub static HOST_FETCH_NS: AtomicU64 = AtomicU64::new(0);
/// armless-exec's exec loop: the 5 ms sleeps while the program's blobs came, and their time.
pub static EXEC_RETRIES: AtomicU64 = AtomicU64::new(0);
pub static EXEC_RETRY_NS: AtomicU64 = AtomicU64::new(0);
/// armless-exec's run loop: the 1 ms sleeps while every thread waited on blobs, and their time.
pub static BLOB_WAIT_SLEEPS: AtomicU64 = AtomicU64::new(0);
pub static BLOB_WAIT_SLEEP_NS: AtomicU64 = AtomicU64::new(0);
/// What the guest wrote into the file system: buffers (Content::Mem and Mem chunks), their bytes
/// and their allocated capacity. Read by the sampler (`Fs::stats`), about once a second.
pub static FS_WRITTEN_BUFFERS: AtomicU64 = AtomicU64::new(0);
pub static FS_WRITTEN_BYTES: AtomicU64 = AtomicU64::new(0);
pub static FS_WRITTEN_CAPACITY: AtomicU64 = AtomicU64::new(0);

/// Adds the time between its making and its drop to a counter (nothing on wasm32: no clock).
pub struct Timed {
    #[cfg(not(target_arch = "wasm32"))]
    since: std::time::Instant,
    #[cfg(not(target_arch = "wasm32"))]
    ns: &'static AtomicU64,
}

impl Timed {
    #[cfg(not(target_arch = "wasm32"))]
    pub fn start(ns: &'static AtomicU64) -> Timed {
        Timed {
            since: std::time::Instant::now(),
            ns,
        }
    }

    #[cfg(target_arch = "wasm32")]
    pub fn start(_ns: &'static AtomicU64) -> Timed {
        Timed {}
    }
}

#[cfg(not(target_arch = "wasm32"))]
impl Drop for Timed {
    fn drop(&mut self) {
        self.ns
            .fetch_add(self.since.elapsed().as_nanos() as u64, Relaxed);
    }
}

/// The number of counters `counters` lists.
pub const COUNTERS: usize = 36;

/// The counters, named, as one list (glibc's `mallinfo2` fields come last, 0 elsewhere).
pub fn counters() -> [(&'static str, u64); COUNTERS] {
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
        ("zstd_calls", ZSTD_CALLS.load(Relaxed)),
        ("zstd_in_bytes", ZSTD_IN_BYTES.load(Relaxed)),
        ("zstd_ns", ZSTD_NS.load(Relaxed)),
        ("zstd_out_bytes", ZSTD_OUT_BYTES.load(Relaxed)),
        ("zstd_out_capacity", ZSTD_OUT_CAPACITY.load(Relaxed)),
        ("raw_blobs", RAW_BLOBS.load(Relaxed)),
        ("raw_bytes", RAW_BYTES.load(Relaxed)),
        ("raw_capacity", RAW_CAPACITY.load(Relaxed)),
        ("dev_lock_wait_ns", DEV_LOCK_WAIT_NS.load(Relaxed)),
        ("dev_lock_hold_ns", DEV_LOCK_HOLD_NS.load(Relaxed)),
        ("host_fetches", HOST_FETCHES.load(Relaxed)),
        ("host_fetch_bytes", HOST_FETCH_BYTES.load(Relaxed)),
        ("host_fetch_ns", HOST_FETCH_NS.load(Relaxed)),
        ("exec_retries", EXEC_RETRIES.load(Relaxed)),
        ("exec_retry_ns", EXEC_RETRY_NS.load(Relaxed)),
        ("blob_wait_sleeps", BLOB_WAIT_SLEEPS.load(Relaxed)),
        ("blob_wait_sleep_ns", BLOB_WAIT_SLEEP_NS.load(Relaxed)),
        ("fs_written_buffers", FS_WRITTEN_BUFFERS.load(Relaxed)),
        ("fs_written_bytes", FS_WRITTEN_BYTES.load(Relaxed)),
        ("fs_written_capacity", FS_WRITTEN_CAPACITY.load(Relaxed)),
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
