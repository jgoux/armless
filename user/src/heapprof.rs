//! `--features dhat-heap`: armless-exec profiles its heap with dhat and writes dhat-heap.json, whose
//! peak (t-gmax) allocation sites say what holds the host heap. The file is ARMLESS_DHAT_OUT
//! (default ./dhat-heap.json; set but empty, no profile: the allocator stays, the profiler does
//! not run). Written when `stop` is called: main's exit and a signal's (memstats.rs).
//! Without the feature none of this exists.

#[cfg(feature = "dhat-heap")]
mod on {
    use std::sync::Mutex;

    #[global_allocator]
    static ALLOC: dhat::Alloc = dhat::Alloc;

    static PROFILER: Mutex<Option<dhat::Profiler>> = Mutex::new(None);

    pub fn start() {
        let file = std::env::var_os("ARMLESS_DHAT_OUT").unwrap_or_else(|| "dhat-heap.json".into());
        if file.is_empty() {
            return;
        }
        *PROFILER.lock().unwrap() = Some(
            dhat::Profiler::builder()
                .file_name(file)
                .trim_backtraces(Some(20))
                .build(),
        );
    }

    /// Writes the profile, once. A second caller waits until the file is written.
    pub fn stop() {
        let mut p = PROFILER.lock().unwrap_or_else(|e| e.into_inner());
        drop(p.take());
    }
}

#[cfg(feature = "dhat-heap")]
pub use on::{start, stop};

/// Whether this build has the profiler.
pub const ENABLED: bool = cfg!(feature = "dhat-heap");

#[cfg(not(feature = "dhat-heap"))]
pub fn start() {}

#[cfg(not(feature = "dhat-heap"))]
pub fn stop() {}
