//! Guest code that runs compiled by the native JIT (ARMLESS_JIT=native, `Cpu::enable_native_jit`)
//! as it does interpreted, and the code the program replaces (stored over, or unmapped and mapped
//! again at the same address, where the frame is reused) is not run again.

mod prog;

use armless::vm::Vm;
use prog::*;

/// aarch64 instructions the programs are made of
const SUBS_X1_1: u32 = 0xf100_0421; // subs x1, x1, #1
const BNE_BACK_2: u32 = 0x54ff_ffc1; // b.ne .-8
const AND_X0_FFF: u32 = 0x9240_2c00; // and x0, x0, #0xfff
const BLR_X20: u32 = 0xd63f_0280; // blr x20
const RET: u32 = 0xd65f_03c0;
const MOV_X0_7: u32 = 0xd280_00e0; // mov x0, #7
const MOV_X0_9: u32 = 0xd280_0120; // mov x0, #9
const STR_W9_X20: u32 = 0xb900_0289; // str w9, [x20]
const STR_W9_X10: u32 = 0xb900_0149; // str w9, [x10]
const STR_W9_X10_4: u32 = 0xb900_0549; // str w9, [x10, #4]
/// dc cvau, x20; dsb ish; ic ivau, x20; dsb ish; isb: what a program makes its new code visible with
const SYNC_CODE_X20: [u32; 5] = [0xd50b_7b34, 0xd503_3b9f, 0xd50b_7534, 0xd503_3b9f, 0xd503_3fdf];

const PROT_RWX: u64 = 7;
/// MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED
const MAP_FIXED_ANON: u64 = 0x32;
const MMAP: u64 = 222;
const MUNMAP: u64 = 215;
const CODE_AT: u64 = 0x1000_0000;
/// Passes of the loops: enough for the compiler (it runs on a thread of its own) to be done and
/// the loop to run compiled for a while, few enough for the interpreter to do them in a second
const PASSES: u64 = 2_000_000;

/// A machine whose CPUs compile code run 16 times, or 0 times (the interpreter): the instruction
/// counts of programs this short would never reach the default
fn machine_jit(jit: bool) -> Vm<armless::platform::NativePlatform> {
    let mut vm = machine();
    if jit {
        for cpu in vm.cpus.iter_mut() {
            assert!(cpu.enable_native_jit(), "no native JIT on this host");
            cpu.jit.threshold = 16;
        }
    }
    vm
}

/// (functions compiled, blocks that ran compiled) of all the CPUs
fn compiled(vm: &Vm<armless::platform::NativePlatform>) -> (u64, u64) {
    vm.cpus.iter().fold((0, 0), |(c, r), cpu| {
        (c + cpu.native_jit_stats().0, r + cpu.jit.block_runs)
    })
}

/// Runs `p` on `vm`, and says it exited without a failed check
fn run_ok(vm: &mut Vm<armless::platform::NativePlatform>, p: Prog) {
    let mut proc = start(vm, p);
    let (_, status) = run(&mut proc, vm);
    ok(&status);
}

/// x0 += `step` `PASSES` times, kept to its low 12 bits
fn hot_loop(step: u32) -> Prog {
    let mut p = Prog::default();
    p.li(0, 0);
    p.li(1, PASSES);
    // add x0, x0, #step
    p.code.extend([0x9100_0000 | step << 10, SUBS_X1_1, BNE_BACK_2, AND_X0_FFF]);
    p.expect(((step as u64 * PASSES) & 0xfff) as i64);
    p
}

#[test]
fn a_hot_loop_gives_the_result_the_interpreter_gives() {
    let mut vm = machine_jit(false);
    run_ok(&mut vm, hot_loop(3));
    assert_eq!(compiled(&vm), (0, 0));

    let mut vm = machine_jit(true);
    run_ok(&mut vm, hot_loop(3));
    let (functions, runs) = compiled(&vm);
    assert!(functions > 0 && runs > 0, "nothing ran compiled: {functions} functions, {runs} runs");
    // --memstats reports it, summed over the CPUs
    let counter = |name: &str| {
        armless::memstats::counters().iter().find(|(n, _)| *n == name).unwrap().1
    };
    assert!(counter("jit_compiled") >= functions && counter("jit_code_bytes") > 0);
}

/// `PASSES` calls of the function at x20, each of which sets x0
fn call_loop(p: &mut Prog) {
    p.li(1, PASSES);
    p.code.extend([BLR_X20, SUBS_X1_1, BNE_BACK_2]);
}

#[test]
fn code_the_program_stores_over_is_not_run_again() {
    let program = || {
        let mut p = Prog::default();
        // The function is in the data, which is as executable as the code: it returns 7
        p.put(0x800, &[MOV_X0_7.to_le_bytes(), RET.to_le_bytes()].concat());
        p.li(20, DATA + 0x800);
        call_loop(&mut p);
        p.expect(7);
        // It returns 9 from now on
        p.li(9, MOV_X0_9 as u64);
        p.code.push(STR_W9_X20);
        p.code.extend(SYNC_CODE_X20);
        call_loop(&mut p);
        p.expect(9);
        p
    };
    run_ok(&mut machine_jit(false), program());
    let mut vm = machine_jit(true);
    run_ok(&mut vm, program());
    assert!(compiled(&vm).1 > 0, "nothing ran compiled");
}

#[test]
fn code_in_a_page_that_is_unmapped_and_mapped_again_is_not_run_again() {
    let program = || {
        let mut p = Prog::default();
        // A page at CODE_AT with `mov x0, #value; ret` in it, called until it is hot
        let function = |p: &mut Prog, value: u32| {
            p.sys(MMAP, &[I(CODE_AT), I(4096), I(PROT_RWX), I(MAP_FIXED_ANON), I(0xffff_ffff), I(0)]);
            p.li(10, CODE_AT);
            p.li(9, value as u64);
            p.code.push(STR_W9_X10);
            p.li(9, RET as u64);
            p.code.push(STR_W9_X10_4);
            p.li(20, CODE_AT);
            p.code.extend(SYNC_CODE_X20);
            call_loop(p);
        };
        function(&mut p, MOV_X0_7);
        p.expect(7);
        // The frame it was in is free now, and the next page is made of it
        p.sys(MUNMAP, &[I(CODE_AT), I(4096)]).expect(0);
        function(&mut p, MOV_X0_9);
        p.expect(9);
        // And a page replaced by a mapping over it, with no munmap
        function(&mut p, MOV_X0_7);
        p.expect(7);
        p
    };
    run_ok(&mut machine_jit(false), program());
    let mut vm = machine_jit(true);
    run_ok(&mut vm, program());
    assert!(compiled(&vm).1 > 0, "nothing ran compiled");
}

/// A JIT machine whose filesystem has /first and /second
fn machine_with_progs(first: &[u8], second: &[u8]) -> Vm<armless::platform::NativePlatform> {
    let config = armless::vm::Config {
        memory_size: 128 << 20,
        cpus: test_cpus(),
        devices: armless::dev::DeviceConfig {
            console: false,
            fs_tag: Some("host9p".into()),
            ..Default::default()
        },
    };
    let mut vm = armless::runner::new_vm(config).unwrap().0;
    for cpu in vm.cpus.iter_mut() {
        assert!(cpu.enable_native_jit(), "no native JIT on this host");
        cpu.jit.threshold = 16;
    }
    let json = format!(
        r#"{{"version":3,"fsroot":[["first",{},0,33261,0,0,"first-blob"],["second",{},0,33261,0,0,"second-blob"]]}}"#,
        first.len(),
        second.len()
    );
    vm.board.load_filesystem(json.as_bytes()).unwrap();
    vm.board.provide_raw_blob("first-blob", first.to_vec()).unwrap();
    vm.board.provide_raw_blob("second-blob", second.to_vec()).unwrap();
    vm
}

#[test]
fn code_of_a_program_that_execs_another_is_not_run_by_it() {
    // Both are loops at the same addresses, which differ in what they add: the second's check
    // fails if the first's compiled loop runs for it
    let mut first = Prog::default();
    first.put(0x200, b"/second\0");
    // the first runs its loop hot, then execs the second
    first.li(1, PASSES);
    first.code.extend([0x9100_0c00, SUBS_X1_1, BNE_BACK_2]);
    first.sys(221, &[D(0x200), I(0), I(0)]);
    first.expect(0); // only reached if the exec fails
    let mut vm = machine_with_progs(&first.elf(), &hot_loop(5).elf());
    let board = vm.board.clone();
    let mut proc =
        armless_user::Process::exec(&mut vm.cpus[0], board, &["/first".into()], &[], "/").unwrap();
    let (_, status) = run(&mut proc, &mut vm);
    ok(&status);
    assert!(compiled(&vm).1 > 0, "nothing ran compiled");
}

#[test]
fn a_program_started_on_a_machine_that_compiled_another_does_not_run_its_code() {
    // The same addresses, and the frames the first one freed: the second's loop adds another step
    let mut vm = machine_jit(true);
    run_ok(&mut vm, hot_loop(3));
    let first = compiled(&vm).1;
    assert!(first > 0, "nothing ran compiled");
    run_ok(&mut vm, hot_loop(5));
    assert!(compiled(&vm).1 > first, "the second did not run compiled");
}
