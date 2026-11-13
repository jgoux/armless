//! `--memstats`: where the process's peak PSS goes. A thread samples /proc/self/smaps (Linux) by
//! mapping, with the machine's counters (`armless::memstats`), every 100 ms; `ready` and the
//! sample with the highest total are printed to stderr as `memstats ready {json}` and
//! `memstats peak {json}` (at exit, and when SIGTERM or SIGINT ends the process).
//! `--trim` returns the heap's free memory to the system at the points main names and every second.

use std::fmt::Write;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use armless::memstats::{counters, exit_now, return_freed_memory, wait_termination, COUNTERS};
use armless::vm::Board;
use armless::Ram;

use crate::heapprof;

const SAMPLE_EVERY: Duration = Duration::from_millis(100);
const TRIM_EVERY: Duration = Duration::from_secs(1);
/// `Fs::stats` walks every inode under the device lock, so the sampler asks for it this often.
const FS_STATS_EVERY: Duration = Duration::from_secs(1);

static TRIM: AtomicBool = AtomicBool::new(false);
static DONE: AtomicBool = AtomicBool::new(false);
static RAM_START: AtomicU64 = AtomicU64::new(0);
static RAM_LEN: AtomicU64 = AtomicU64::new(0);
static STATE: Mutex<Option<State>> = Mutex::new(None);
static BOARD: Mutex<Option<Arc<Board>>> = Mutex::new(None);

/// PSS in KiB, by group of mappings.
#[derive(Clone, Copy, Default)]
struct Pss {
    total: u64,
    ram: u64,
    heap: u64,
    jit: u64,
    text: u64,
    other: u64,
}

#[derive(Clone, Copy)]
struct Sample {
    t_ms: u64,
    pss: Option<Pss>,
    counters: [(&'static str, u64); COUNTERS],
}

struct State {
    start: Instant,
    peak: Option<Sample>,
    max: [u64; COUNTERS],
}

/// Starts the threads: the sampler (`memstats`) and the one that prints the peak when a signal
/// ends the process. Called before any other thread exists, so every one blocks the signals.
pub fn init(memstats: bool, trim: bool, start: Instant) {
    TRIM.store(trim, Relaxed);
    if !memstats && !trim && !heapprof::ENABLED {
        return;
    }
    if memstats || heapprof::ENABLED {
        armless::memstats::block_termination();
        if memstats {
            *STATE.lock().unwrap() = Some(State {
                start,
                peak: None,
                max: [0; COUNTERS],
            });
        }
        std::thread::spawn(|| {
            let sig = wait_termination();
            finish();
            // The heap profile is written now, as at a normal exit (it waits for one that is being written)
            heapprof::stop();
            exit_now(128 + sig);
        });
    }
    if !memstats && !trim {
        return;
    }
    std::thread::spawn(move || {
        let mut trimmed = Instant::now();
        let mut fs_stats = Instant::now();
        loop {
            std::thread::sleep(SAMPLE_EVERY);
            if memstats {
                if fs_stats.elapsed() >= FS_STATS_EVERY {
                    refresh_fs_stats();
                    fs_stats = Instant::now();
                }
                sample();
            }
            if trim && trimmed.elapsed() >= TRIM_EVERY {
                return_freed_memory();
                trimmed = Instant::now();
            }
        }
    });
}

/// The machine, whose file system `refresh_fs_stats` reads.
pub fn set_board(board: &Arc<Board>) {
    *BOARD.lock().unwrap() = Some(board.clone());
}

/// Sleeps, and counts the sleep and its time (what it took, not what was asked for).
pub fn sleep(d: Duration, count: &AtomicU64, ns: &AtomicU64) {
    let t = Instant::now();
    std::thread::sleep(d);
    count.fetch_add(1, Relaxed);
    ns.fetch_add(t.elapsed().as_nanos() as u64, Relaxed);
}

/// What the guest wrote into the file system (buffers, bytes, allocated capacity), into the counters.
fn refresh_fs_stats() {
    let Some(board) = BOARD.lock().unwrap().clone() else {
        return;
    };
    if let Some(s) = board.with_fs(|fs| fs.stats()) {
        use armless::memstats::{FS_WRITTEN_BUFFERS, FS_WRITTEN_BYTES, FS_WRITTEN_CAPACITY};
        FS_WRITTEN_BUFFERS.store(s.written_buffers as u64, Relaxed);
        FS_WRITTEN_BYTES.store(s.written_bytes as u64, Relaxed);
        FS_WRITTEN_CAPACITY.store(s.written_capacity as u64, Relaxed);
    }
}

/// The guest RAM's mapping, which `ram` is the PSS of.
pub fn set_ram(ram: &Ram) {
    RAM_START.store(ram.as_ptr() as u64, Relaxed);
    RAM_LEN.store(ram.len() as u64, Relaxed);
}

/// Gives free heap memory back to the system, if `--trim`.
pub fn trim() {
    if TRIM.load(Relaxed) {
        return_freed_memory();
    }
}

pub fn ready() {
    refresh_fs_stats();
    if let Some(s) = sample() {
        eprintln!("memstats ready {}", json(&s, None));
    }
}

/// Prints the peak line, once.
pub fn finish() {
    if STATE.lock().unwrap().is_none() || DONE.swap(true, Relaxed) {
        return;
    }
    refresh_fs_stats();
    sample();
    let st = STATE.lock().unwrap();
    if let Some(State {
        peak: Some(p), max, ..
    }) = st.as_ref()
    {
        eprintln!("memstats peak {}", json(p, Some(max)));
    }
}

/// Takes a sample, and keeps it if its total is the highest so far.
fn sample() -> Option<Sample> {
    let mut st = STATE.lock().unwrap();
    let st = st.as_mut()?;
    let s = Sample {
        t_ms: st.start.elapsed().as_millis() as u64,
        pss: smaps(),
        counters: counters(),
    };
    for (m, (_, v)) in st.max.iter_mut().zip(&s.counters) {
        *m = (*m).max(*v);
    }
    let total = |s: &Sample| s.pss.map_or(0, |p| p.total);
    if st.peak.as_ref().is_none_or(|p| total(&s) >= total(p)) {
        st.peak = Some(s);
    }
    Some(s)
}

/// PSS by group: the guest RAM, the heap (and the other anonymous mappings), the JIT's memfd,
/// the binary and its libraries, the rest.
fn smaps() -> Option<Pss> {
    let text = std::fs::read_to_string("/proc/self/smaps").ok()?;
    Some(group_pss(
        &text,
        RAM_START.load(Relaxed),
        RAM_LEN.load(Relaxed),
    ))
}

fn group_pss(text: &str, ram_start: u64, ram_len: u64) -> Pss {
    let mut pss = Pss::default();
    let mut group = 0;
    for line in text.lines() {
        let mut it = line.split_whitespace();
        let first = it.next().unwrap_or("");
        if let Some((a, b)) = first.split_once('-') {
            if let (Ok(a), Ok(_)) = (u64::from_str_radix(a, 16), u64::from_str_radix(b, 16)) {
                let path = it.nth(4).unwrap_or("");
                group = if (ram_start..ram_start + ram_len).contains(&a) {
                    0
                } else if path.contains("memfd:armless-jit") {
                    2
                } else if path.is_empty() || path == "[heap]" || path.starts_with("[anon") {
                    1
                } else if path.starts_with('/') {
                    3
                } else {
                    4
                };
                continue;
            }
        }
        if first == "Pss:" {
            let kb: u64 = it.next().and_then(|v| v.parse().ok()).unwrap_or(0);
            pss.total += kb;
            *[
                &mut pss.ram,
                &mut pss.heap,
                &mut pss.jit,
                &mut pss.text,
                &mut pss.other,
            ][group] += kb;
        }
    }
    pss
}

fn json(s: &Sample, max: Option<&[u64; COUNTERS]>) -> String {
    let mut o = format!("{{\"t_ms\":{}", s.t_ms);
    if let Some(p) = s.pss {
        let _ = write!(
            o,
            ",\"pss_kb\":{{\"total\":{},\"ram\":{},\"heap\":{},\"jit\":{},\"text\":{},\"other\":{}}}",
            p.total, p.ram, p.heap, p.jit, p.text, p.other
        );
    }
    o.push_str(",\"counters\":{");
    for (i, (k, v)) in s.counters.iter().enumerate() {
        let _ = write!(o, "{}\"{k}\":{v}", if i > 0 { "," } else { "" });
    }
    o.push('}');
    if let Some(max) = max {
        o.push_str(",\"max\":{");
        for (i, ((k, _), v)) in s.counters.iter().zip(max).enumerate() {
            let _ = write!(o, "{}\"{k}\":{v}", if i > 0 { "," } else { "" });
        }
        o.push('}');
    }
    o.push('}');
    o
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn smaps_are_grouped_by_what_maps_them() {
        let map = |range: &str, path: &str, kb: u64| {
            format!(
                "{range} rw-p 00000000 00:00 0 {path}\nSize: 8 kB\nRss: 8 kB\nPss: {kb} kB\nPss_Anon: 1 kB\nVmFlags: rd wr\n"
            )
        };
        let text = [
            map("55d0-55e0", "/usr/bin/armless-exec", 1),
            map("55e0-55f0", "[heap]", 2),
            map("7000-7100", "/tmp/armless-exec-1.ram (deleted)", 4),
            map("7f00-7f10", "/memfd:armless-jit (deleted)", 8),
            map("7f10-7f20", "", 16),
            map("7ff0-8000", "[stack]", 32),
        ]
        .concat();
        let p = group_pss(&text, 0x7000, 0x100);
        assert_eq!((p.ram, p.heap, p.jit, p.text, p.other), (4, 18, 8, 1, 32));
        assert_eq!(p.total, 63);
    }
}
