//! Native zstd decoding: frames decoded in a row through the thread's reused decoder come out
//! right, and decoding allocates its big buffers once, not once per frame or call.
//!
//! A counting global allocator (this test binary only) records the allocations of 64 KiB or more
//! that the thread under test makes.
//!
//! The frames of `tests/fixtures/zstd` are `zstd -19` of `raw(seed, len)` below (a 64 KiB block
//! of pseudo-random bytes, repeated), made with zstd 1.5.7:
//!   w8m.zst         raw(1, 9 MiB)   window 8 MiB, content size in the header
//!   w3m.zst         raw(2, 3 MiB)   window 3 MiB, content size in the header
//!   w100k.zst       raw(3, 100 KiB) window 100 KiB, content size in the header
//!   w8m-nosize.zst  raw(4, 9 MiB)   window 8 MiB, no content size (compressed from a pipe)

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::sync::Mutex;

use armless::zstd::{self, Ended};
use ruzstd::encoding::{compress_to_vec, CompressionLevel};

const BIG: usize = 64 << 10;

static LARGE: Mutex<Vec<usize>> = Mutex::new(Vec::new());

thread_local! {
    static COUNTING: Cell<bool> = const { Cell::new(false) };
}

struct Counting;

fn note(size: usize) {
    if size >= BIG && COUNTING.try_with(Cell::get).unwrap_or(false) {
        // The lock may allocate the first time: not counted
        COUNTING.with(|c| c.set(false));
        LARGE.lock().unwrap().push(size);
        COUNTING.with(|c| c.set(true));
    }
}

// SAFETY: every call is passed to `System` unchanged.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        note(l.size());
        unsafe { System.alloc(l) }
    }
    unsafe fn alloc_zeroed(&self, l: Layout) -> *mut u8 {
        note(l.size());
        unsafe { System.alloc_zeroed(l) }
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        unsafe { System.dealloc(p, l) }
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        note(n);
        unsafe { System.realloc(p, l, n) }
    }
}

#[global_allocator]
static ALLOC: Counting = Counting;

/// The sizes of the large allocations `f` makes on this thread.
fn large_allocs<T>(f: impl FnOnce() -> T) -> (T, Vec<usize>) {
    LARGE.lock().unwrap().clear();
    COUNTING.with(|c| c.set(true));
    let r = f();
    COUNTING.with(|c| c.set(false));
    (r, std::mem::take(&mut *LARGE.lock().unwrap()))
}

/// `len` bytes: a block of 64 KiB of pseudo-random bytes from `seed`, repeated.
fn raw(seed: u64, len: usize) -> Vec<u8> {
    let mut x = seed.wrapping_mul(0x9e37_79b9_7f4a_7c15) | 1;
    let mut block = Vec::with_capacity(64 << 10);
    while block.len() < 64 << 10 {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        block.extend_from_slice(&x.to_le_bytes());
    }
    block.iter().copied().cycle().take(len).collect()
}

/// A frame to decode, and what it decodes to.
struct Case {
    name: &'static str,
    frame: Vec<u8>,
    raw: Vec<u8>,
}

fn fixture(name: &'static str, file: &str, seed: u64, len: usize) -> Case {
    let path = format!("{}/tests/fixtures/zstd/{file}", env!("CARGO_MANIFEST_DIR"));
    Case {
        name,
        frame: std::fs::read(path).unwrap(),
        raw: raw(seed, len),
    }
}

/// Frames with windows of 8 MiB (twice, once without a content size), 3 MiB and 100 KiB, and one
/// from ruzstd's encoder (a small window and no content size).
fn cases() -> Vec<Case> {
    let encoded = raw(5, 600 << 10);
    vec![
        fixture("8 MiB window", "w8m.zst", 1, 9 << 20),
        fixture("3 MiB window", "w3m.zst", 2, 3 << 20),
        fixture("100 KiB window", "w100k.zst", 3, 100 << 10),
        fixture("8 MiB window, no size", "w8m-nosize.zst", 4, 9 << 20),
        Case {
            name: "ruzstd encoder",
            frame: compress_to_vec(&encoded[..], CompressionLevel::Fastest),
            raw: encoded,
        },
    ]
}

fn kib(a: &[usize]) -> Vec<usize> {
    a.iter().map(|n| n >> 10).collect()
}

#[test]
fn frames_in_a_row_decode_through_the_reused_decoder() {
    let cases = cases();
    let all: Vec<u8> = cases.iter().flat_map(|c| c.frame.clone()).collect();
    let everything: Vec<u8> = cases.iter().flat_map(|c| c.raw.clone()).collect();

    // One call, all the frames
    assert!(zstd::decompress(&all).as_deref() == Some(&everything[..]));

    // Call after call, one frame each, in an order that makes the window shrink and grow
    for i in [0, 1, 2, 3, 4, 2, 1, 0, 4, 3, 2, 0, 1] {
        let c = &cases[i];
        assert!(
            zstd::decompress(&c.frame).as_deref() == Some(&c.raw[..]),
            "{}",
            c.name
        );
    }

    // In pieces, from the same decoder
    let mut got = Vec::new();
    let ended = zstd::decompress_each(&all, &mut |p| {
        got.extend_from_slice(p);
        true
    });
    assert_eq!(ended, Ended::Done);
    assert!(got == everything);

    // A truncated frame, and a bad one, fail and do not spoil the next decode
    let cut = &cases[0].frame[..cases[0].frame.len() / 2];
    assert_eq!(zstd::decompress(cut), None);
    assert_eq!(zstd::decompress_each(cut, &mut |_| true), Ended::Corrupt);
    assert_eq!(zstd::decompress(b"\x28\xb5\x2f\xfd garbage"), None);
    assert_eq!(zstd::decompress(b"not zstd at all"), None);
    assert!(zstd::decompress(&all).as_deref() == Some(&everything[..]));

    // A sink that stops, then a full decode
    let mut n = 0;
    let ended = zstd::decompress_each(&all, &mut |_| {
        n += 1;
        n < 3
    });
    assert_eq!(ended, Ended::Stopped);
    let c = &cases[1];
    assert!(zstd::decompress(&c.frame).as_deref() == Some(&c.raw[..]));

    // A decompression from within a sink gets a decoder of its own
    let c = &cases[2];
    let mut inner = None;
    let ended = zstd::decompress_each(&all, &mut |_| {
        inner = zstd::decompress(&c.frame);
        false
    });
    assert_eq!(ended, Ended::Stopped);
    assert!(inner.as_deref() == Some(&c.raw[..]));
    assert!(zstd::decompress(&all).as_deref() == Some(&everything[..]));
}

#[test]
fn decoding_allocates_its_big_buffers_once() {
    let cases = cases();
    let all: Vec<u8> = cases.iter().flat_map(|c| c.frame.clone()).collect();
    let total = cases.iter().map(|c| c.raw.len()).sum::<usize>();

    // The first call of the thread sets the decoder up for the frame it meets first: the window
    // (8 MiB and a block or two) in one go, the block and literals scratch growing to a block each
    let c = &cases[0];
    let (out, first) = large_allocs(|| zstd::decompress(&c.frame).unwrap());
    println!("first call, {}: {:?} KiB", c.name, kib(&first));
    assert!(out == c.raw);
    assert!(first.len() <= 6, "{:?}", kib(&first));

    // The next calls, whatever the window (none is larger), allocate the output at most
    for i in [0, 1, 2, 3, 4, 0, 3] {
        let c = &cases[i];
        let (out, a) = large_allocs(|| zstd::decompress(&c.frame).unwrap());
        println!("call {i}, {}: {:?} KiB", c.name, kib(&a));
        assert!(out == c.raw);
        // What is allocated is the output (all of it in one go if the header says how big it is),
        // never a window; the block and literals scratch only ever grows to a block each
        if i != 3 && i != 4 {
            let rest: Vec<usize> = a.iter().copied().filter(|&n| n != c.raw.len()).collect();
            assert_eq!(a.len() - rest.len(), 1, "{}: {:?}", c.name, kib(&a));
            assert!(
                rest.len() <= 2 && rest.iter().all(|&n| n <= 130 << 10),
                "{}: {rest:?}",
                c.name
            );
        } else {
            // The output grows by doubling
            assert!(
                a.len() <= 14 && a.iter().all(|&n| n <= 2 * c.raw.len()),
                "{}: {:?}",
                c.name,
                kib(&a)
            );
        }
    }

    // All the frames in one call: the output of the first, then what the rest of it makes
    let (out, a) = large_allocs(|| zstd::decompress(&all).unwrap());
    println!("all frames in one call: {:?} KiB", kib(&a));
    assert_eq!(out.len(), total);

    // In pieces the output is never allocated, so nothing is
    let (ended, a) = large_allocs(|| zstd::decompress_each(&all, &mut |_| true));
    println!("all frames in pieces: {:?} KiB", kib(&a));
    assert_eq!(ended, Ended::Done);
    assert!(a.is_empty(), "{:?}", kib(&a));
}
