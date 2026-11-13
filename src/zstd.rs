//! zstd decompression: 9p blobs (`.bin.zst`), compressed snapshots and
//! zstd-compressed kernels.
//!
//! In wasm this is the C decoder (`lib/zstd/zstddeclib.c`, linked in by
//! `build-wasm.sh`), which is much faster there; natively it is `ruzstd`.

#[cfg(target_arch = "wasm32")]
mod imp {
    use std::alloc;

    // SAFETY: the C decoder linked in by build-wasm.sh, with these signatures. Making a stream and
    // telling an error code from a size take no pointers and have no preconditions.
    unsafe extern "C" {
        safe fn ZSTD_createDStream() -> u32;
        fn ZSTD_freeDStream(ctx: u32) -> i32;
        fn ZSTD_decompressStream_simpleArgs(
            ctx: u32,
            dst: *mut u8,
            dst_capacity: u32,
            dst_pos: *mut u32,
            src: *const u8,
            src_size: u32,
            src_pos: *mut u32,
        ) -> i32;
        safe fn ZSTD_isError(err: i32) -> bool;
    }

    const MALLOC_ALIGN: usize = 16;

    /// malloc for the zstd library (`#define malloc v86_malloc`).
    ///
    /// # Safety
    /// Called by C code only.
    #[no_mangle]
    pub unsafe extern "C" fn v86_malloc(size: u32) -> u32 {
        let Ok(layout) = alloc::Layout::from_size_align(size as usize + MALLOC_ALIGN, MALLOC_ALIGN)
        else {
            return 0;
        };
        // SAFETY: the layout is never zero-sized (MALLOC_ALIGN bytes of header at least).
        let addr = unsafe { alloc::alloc(layout) };
        if addr.is_null() {
            return 0; // as malloc: zstd checks
        }
        // SAFETY: the header, at the start of what was just allocated, 16-byte aligned.
        unsafe { *(addr as *mut u32) = size };
        addr as u32 + MALLOC_ALIGN as u32
    }

    /// # Safety
    /// `addr` must come from `v86_malloc`.
    #[no_mangle]
    pub unsafe extern "C" fn v86_free(addr: u32) {
        if addr == 0 {
            return;
        }
        let base = addr - MALLOC_ALIGN as u32;
        // SAFETY: `addr` came from `v86_malloc` (the caller's promise), so its header is before it.
        let size = unsafe { *(base as *mut u32) };
        let layout = alloc::Layout::from_size_align(size as usize + MALLOC_ALIGN, MALLOC_ALIGN)
            .expect("the layout v86_malloc made");
        // SAFETY: allocated by `v86_malloc` with this layout.
        unsafe { alloc::dealloc(base as *mut u8, layout) }
    }

    /// Streaming decoder: compressed bytes in, decompressed chunks out.
    pub struct Stream {
        ctx: u32,
        buf: Vec<u8>,
    }

    impl Default for Stream {
        fn default() -> Self {
            Self::new()
        }
    }

    impl Stream {
        pub fn new() -> Stream {
            Stream {
                // 0 if it could not allocate, which `feed` checks.
                ctx: ZSTD_createDStream(),
                buf: vec![0; 1 << 17],
            }
        }

        /// Decompress `input`, passing output to `sink`. False on corrupt data.
        pub fn feed(&mut self, input: &[u8], sink: &mut dyn FnMut(&[u8])) -> bool {
            if self.ctx == 0 {
                return false;
            }
            let mut src_pos = 0u32;
            loop {
                let mut dst_pos = 0u32;
                // SAFETY: a live stream; the output buffer and the input are ours, with their
                // lengths, and the positions are this frame's own variables.
                let r = unsafe {
                    ZSTD_decompressStream_simpleArgs(
                        self.ctx,
                        self.buf.as_mut_ptr(),
                        self.buf.len() as u32,
                        &mut dst_pos,
                        input.as_ptr(),
                        input.len() as u32,
                        &mut src_pos,
                    )
                };
                if ZSTD_isError(r) {
                    return false;
                }
                if dst_pos > 0 {
                    sink(&self.buf[..dst_pos as usize]);
                }
                // Done with this input once it is consumed and the output
                // buffer was not filled (nothing more buffered inside).
                if src_pos as usize >= input.len() && (dst_pos as usize) < self.buf.len() {
                    return true;
                }
            }
        }
    }

    impl Drop for Stream {
        fn drop(&mut self) {
            // SAFETY: this stream's own context, freed once (zstd ignores 0).
            unsafe {
                ZSTD_freeDStream(self.ctx);
            }
        }
    }

    pub fn decompress(data: &[u8]) -> Option<Vec<u8>> {
        let mut out = Vec::new();
        let mut s = Stream::new();
        s.feed(data, &mut |b| out.extend_from_slice(b))
            .then_some(out)
    }
}

#[cfg(not(target_arch = "wasm32"))]
mod imp {
    use ruzstd::decoding::{FrameDecoder, StreamingDecoder};
    use std::io::Read;

    /// Bytes decompressed before each call of the sink of `decompress_each`, at most.
    const EACH: usize = 128 << 10;

    /// The largest frame content size (from the frame header, which the data may lie about) that
    /// `decompress` reserves the output for in one go.
    const PRESIZE_MAX: u64 = 1 << 30;

    /// What a call decodes with, made for the call and dropped at its end, so that nothing of it stays
    /// allocated between calls: the window and the block and literals scratch of ruzstd's
    /// `FrameDecoder` are freed with it (before the heap is trimmed), and one decoder serves all the
    /// frames of a call, which allocates them once.
    struct Scratch {
        dec: FrameDecoder,
        /// `dec` has been through `init` once: its window is allocated at the size of a frame, in
        /// one go (a new decoder's grows by doubling, copying, while it decodes)
        primed: bool,
        /// The pieces of `decompress_each` (empty for `decompress`)
        piece: Vec<u8>,
    }

    /// `f` with a decoder, which is dropped before this returns.
    fn with_scratch<T>(f: impl FnOnce(&mut Scratch) -> T) -> T {
        let mut s = Scratch {
            dec: FrameDecoder::new(),
            primed: false,
            piece: Vec::new(),
        };
        f(&mut s)
    }

    /// The decoder of the frame at the start of `rest`, which it reads the header of.
    fn open<'a, 'b>(
        dec: &'a mut FrameDecoder,
        primed: &mut bool,
        rest: &'a mut &'b [u8],
    ) -> Option<StreamingDecoder<&'a mut &'b [u8], &'a mut FrameDecoder>> {
        if !*primed {
            // The first `init` of a decoder makes its state, the next ones (`reset`) reserve the
            // window up front
            let mut header: &[u8] = rest;
            dec.init(&mut header).ok()?;
            *primed = true;
        }
        StreamingDecoder::new_with_decoder(rest, dec).ok()
    }

    /// Gives glibc's free memory back to the system: what a decode freed (the output's growth, the
    /// scratch of the decoder, dropped when the call ends) stays resident in the heap otherwise, as glibc raises
    /// its mmap threshold after the first large block is freed and serves the next ones from the heap.
    /// Called once per `decompress` and `decompress_each`, after the frames are decoded.
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    fn trim_heap() {
        // SAFETY: `malloc_trim` takes no pointers, and is safe to call at any time from any thread.
        unsafe {
            libc::malloc_trim(0);
        }
    }

    /// Nothing to give back where the allocator is not glibc's.
    #[cfg(not(all(target_os = "linux", target_env = "gnu")))]
    fn trim_heap() {}

    /// Every frame of `data`, decompressed.
    pub fn decompress(data: &[u8]) -> Option<Vec<u8>> {
        use crate::memstats::{
            Timed, ZSTD_CALLS, ZSTD_IN_BYTES, ZSTD_NS, ZSTD_OUT_BYTES, ZSTD_OUT_CAPACITY,
        };
        use std::sync::atomic::Ordering::Relaxed;
        let _time = Timed::start(&ZSTD_NS);
        let out = with_scratch(|s| {
            let mut out = Vec::new();
            let mut rest = data;
            while !rest.is_empty() {
                let mut d = open(&mut s.dec, &mut s.primed, &mut rest)?;
                // The size the frame says it has (0 if it does not say): the output is allocated at
                // that size, once (for the first frame; the next ones grow it as `reserve` does)
                let size = d.decoder.content_size();
                if (1..=PRESIZE_MAX).contains(&size) {
                    if out.is_empty() {
                        out.reserve_exact(size as usize);
                    } else {
                        out.reserve(size as usize);
                    }
                }
                d.read_to_end(&mut out).ok()?;
            }
            Some(out)
        });
        trim_heap();
        let out = out?;
        // What the caller gets: its size, and what was allocated for it (read_to_end's growth leaves slack)
        ZSTD_CALLS.fetch_add(1, Relaxed);
        ZSTD_IN_BYTES.fetch_add(data.len() as u64, Relaxed);
        ZSTD_OUT_BYTES.fetch_add(out.len() as u64, Relaxed);
        ZSTD_OUT_CAPACITY.fetch_add(out.capacity() as u64, Relaxed);
        Some(out)
    }

    /// How `decompress_each` ended.
    #[derive(Debug, PartialEq)]
    pub enum Ended {
        Done,
        /// `data` is not valid zstd (what `sink` was given before that is of no use)
        Corrupt,
        /// `sink` returned false
        Stopped,
    }

    /// Every frame of `data`, decompressed, given to `sink` in pieces of at most `EACH` bytes: the
    /// decompressed contents are never all in memory (as `decompress` has them).
    pub fn decompress_each(data: &[u8], sink: &mut dyn FnMut(&[u8]) -> bool) -> Ended {
        let ended = with_scratch(|s| {
            s.piece.resize(EACH, 0);
            let mut rest = data;
            while !rest.is_empty() {
                let Some(mut d) = open(&mut s.dec, &mut s.primed, &mut rest) else {
                    return Ended::Corrupt;
                };
                loop {
                    match d.read(&mut s.piece) {
                        Ok(0) => break,
                        Ok(n) if sink(&s.piece[..n]) => {},
                        Ok(_) => return Ended::Stopped,
                        Err(_) => return Ended::Corrupt,
                    }
                }
            }
            Ended::Done
        });
        trim_heap();
        ended
    }

    /// Native stand-in for the wasm streaming decoder: each `feed` must hold
    /// whole frames (only the wasm exports, type-checked natively, use it).
    #[derive(Default)]
    pub struct Stream;

    impl Stream {
        pub fn new() -> Stream {
            Stream
        }
        pub fn feed(&mut self, input: &[u8], sink: &mut dyn FnMut(&[u8])) -> bool {
            match decompress(input) {
                Some(d) => {
                    sink(&d);
                    true
                },
                None => false,
            }
        }
    }

    /// A reader of the decompressed contents of a zstd stream.
    pub fn reader<'a, R: Read + 'a>(r: R) -> std::io::Result<Box<dyn Read + 'a>> {
        let d = ruzstd::decoding::StreamingDecoder::new(r)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e.to_string()))?;
        Ok(Box::new(d))
    }
}

pub use imp::*;

/// Whether `data` starts with the zstd frame magic.
pub fn is_zstd(data: &[u8]) -> bool {
    data.len() >= 4 && data[..4] == [0x28, 0xb5, 0x2f, 0xfd]
}
