"""A'u: armless 0.11's user mode (`armless-exec`, native, built from the pinned commit) on the image of a M4a cell.

What it is not: a serving engine. armless-exec answers a guest's system calls from the host and its source returns ENOSYS for every socket call
(sys.rs: `198..=212 | 242 | 243 | 269`), a clone without CLONE_VM|CLONE_THREAD, clone3, execve and execveat. None of the seven images can serve
an HTTP request on it, so this engine produces EVIDENCE, not ready, latency, throughput or memory numbers:

  launch    the image's start exactly as T0 derives it (crates/driver/src/image.rs `plan`: the package's config.json, with the case's Entrypoint
            and environment as gates/m3b prepares them; Docker's environment order and HOME; the working directory entered through the image's
            symlinks and required to exist; a program without a slash found in PATH as a regular executable file); a script entrypoint is
            expanded as the kernel does (armless has no shebang support), since the run would otherwise stop at the first byte
  run       armless-exec --fs (the same flattened tree root.erofs is made from, armless_fs.py), ARMLESS_EXEC_TRACE=1: every system call with its
            result (hexadecimal: ENOSYS is 0xffffffffffffffda), the final report (status, instruction count, calls, opens)
  verdict   the first unsupported call the process met (a socket call, a clone without CLONE_VM|CLONE_THREAD, execve/execveat), what the process
            did after it and how it ended. An outcome that is not tied to such a call in this run's own trace (it never met one, it faulted,
            a call failed with ENOMEM first, armless-exec did not start) is UNCLASSIFIED and fails the job: it is a harness or fs problem or
            something this file does not know, never a "limitation"
  controls  the same image with its Entrypoint replaced by a short program that ends (and Cmd cleared), on armless-exec and on T0 (same package copy,
            same environment): pass or fail only. They say armless-exec can run that runtime on that filesystem; they validate nothing of the entrypoint
"""
import collections, json, os, re, shutil, subprocess, sys, time

# aarch64 system call numbers
NET = {198: "socket", 199: "socketpair", 200: "bind", 201: "listen", 202: "accept", 203: "connect", 204: "getsockname", 205: "getpeername",
       206: "sendto", 207: "recvfrom", 208: "setsockopt", 209: "getsockopt", 210: "shutdown", 211: "sendmsg", 212: "recvmsg",
       242: "accept4", 243: "recvmmsg", 269: "sendmmsg"}
PROC = {220: "clone", 221: "execve", 281: "execveat"}
PROBE = {435: "clone3"}  # glibc probes clone3 and falls back to clone: an ENOSYS here is not a blocker by itself
NAMES = {**NET, **PROC, **PROBE, 93: "exit", 94: "exit_group", 260: "wait4", 56: "openat", 222: "mmap", 215: "munmap", 226: "mprotect"}
ENOSYS, ENOMEM = 0xffffffffffffffda, 0xfffffffffffffff4
LINE = re.compile(r"^\[(\d+)\] sys (\d+)\((0x[0-9a-f]+), (0x[0-9a-f]+), (0x[0-9a-f]+)\) = (.*)$")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PIE_BASE = 0x10_0000
RAM_MB = 3072  # armless-exec accepts 16 MiB to 3.5 GiB (VA = PA, a sparse file)
CLONE_VM, CLONE_THREAD = 0x100, 0x10000

# image -> a program that ends and prints 42 (nginx: its version), run with the image's runtime on both engines
CONTROLS = {
    "node": (["node", "-e", "console.log(6*7)"], "42"),
    "node-runtime": (["node", "-e", "console.log(6*7)"], "42"),
    "python": (["python", "-c", "print(6*7)"], "42"),
    "shell": (["/bin/sh", "-c", "echo $((6*7))"], "42"),
    "nginx": (["nginx", "-v"], "nginx version"),
    "deno": (["deno", "eval", "console.log(6*7)"], "42"),
}


# ---- the image, as T0 reads it

def read_tree(fs_dir):
    with open(os.path.join(fs_dir, "tree.json")) as f:
        return json.load(f)


def canonical(tree, path):
    """image.rs `canonical`: the path with every symlink followed (the last one too), `.` and `..` resolved; None when something is missing or not a directory."""
    paths = tree["paths"]
    parts = lambda p: [c for c in reversed(p.split("/")) if c]
    todo, done, hops = parts(path), [], 0
    while todo:
        c = todo.pop()
        if done and paths.get("/" + "/".join(done), [None])[0] != "dir":
            return None
        if c == ".":
            continue
        if c == "..":
            if done:
                done.pop()
            continue
        done.append(c)
        here = "/" + "/".join(done)
        node = paths.get(here)
        if node is None and here != "/":
            return None
        if node[0] == "sym":
            hops += 1
            if hops > 40:
                return None
            done.pop()
            if node[2].startswith("/"):
                done.clear()
            todo.extend(parts(node[2]))
    return "/" + "/".join(done)


def is_program(tree, path):
    c = canonical(tree, path)
    node = tree["paths"].get(c) if c else None
    return bool(node) and node[0] == "reg" and node[1] & 0o111 != 0


def resolve_user(tree, spec):
    """image.rs `resolve_user`, as far as the launch needs: (uid, gid, home) of `user[:group]` from /etc/passwd ('' is root)."""
    rows = [l.split(":") for l in tree["passwd"].splitlines() if l and not l.startswith("#") and len(l.split(":")) >= 7]
    user = spec.split(":")[0] if spec else ""
    if not user:
        row = next((r for r in rows if r[2] == "0"), None)
        return 0, 0, (row[5] if row else "/")
    row = next((r for r in rows if r[0] == user or r[2] == user), None)
    if row is None:
        return (int(user), 0, "/") if user.isdigit() else (None, None, None)
    return int(row[2]), int(row[3]), row[5] or "/"


def launch_plan(cfg, tree, env_extra, args):
    """image.rs `plan` for A'u: (argv, env, cwd, exec path, the script's `#!` line or None). Raises RuntimeError where T0 would fail."""
    argv = list(cfg.get("Entrypoint") or []) + (list(args) if args else list(cfg.get("Cmd") or []))
    if not argv:
        raise RuntimeError("the image has no Entrypoint or Cmd and no arguments were given")
    uid, gid, home = resolve_user(tree, cfg.get("User") or "")
    if uid is None:
        raise RuntimeError(f"user {cfg.get('User')!r} is not in the image's /etc/passwd")
    env = [["PATH", DEFAULT_PATH], ["HOSTNAME", "localhost"]]
    name = lambda kv: kv.split("=", 1)[0]
    image_env = [kv for kv in (cfg.get("Env") or []) if not any(name(u) == name(kv) for u in env_extra)]
    for kv in list(env_extra) + image_env:
        k, _, v = kv.partition("=")
        e = next((e for e in env if e[0] == k), None)
        if e:
            e[1] = v
        else:
            env.append([k, v])
    if not any(k == "HOME" for k, _ in env):
        env.append(["HOME", home])
    path = next(v for k, v in env if k == "PATH")
    cwd = cfg.get("WorkingDir") or "/"
    cwd_c = canonical(tree, cwd)
    if cwd_c is None or (cwd_c != "/" and tree["paths"][cwd_c][0] != "dir"):
        raise RuntimeError(f"the working directory {cwd} is not in the image")
    if "/" in argv[0]:
        exe = argv[0]
    else:
        cands = [(f"{cwd_c}/{argv[0]}" if d == "" else f"{d.rstrip('/')}/{argv[0]}" if d.startswith("/") else f"{cwd_c}/{d.rstrip('/')}/{argv[0]}") for d in path.split(":")]
        exe = next((c for c in cands if is_program(tree, c)), None)
        if exe is None:
            raise RuntimeError(f"{argv[0]}: not found in PATH={path}")
    real = canonical(tree, exe)
    shebang = tree["shebangs"].get(real) if real else None
    if real is None:
        raise RuntimeError(f"{exe}: not in the image")
    return argv, [f"{k}={v}" for k, v in env], cwd_c, exe, shebang, {"uid": uid, "gid": gid}


def expand_shebang(argv, exe, shebang):
    """What the kernel does with `#!interp [arg]`: interp, arg, the script's path, then the rest of argv."""
    interp = shebang[2:].strip().split(None, 1)
    return interp + [exe] + argv[1:]


# ---- the run

def parse_trace(path):
    """One pass over the trace: counts per call, ENOSYS per call, the first ENOSYS of each call (record index), the first blocker, the first ENOMEM and
    the last records. Records are numbered from 1."""
    calls, enosys, first_enosys = collections.Counter(), collections.Counter(), {}
    ring = collections.deque(maxlen=40)
    n = 0
    first_blocker, first_enomem, after, enomem_n = None, None, 0, 0
    exits = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            m = LINE.match(line.rstrip("\n"))
            if not m:
                continue
            n += 1
            tid, nr, a0, ret = int(m.group(1)), int(m.group(2)), int(m.group(3), 16), m.group(6)
            calls[nr] += 1
            r = int(ret, 16) if ret.startswith("0x") else None
            ring.append((n, tid, nr, a0, ret))
            if r == ENOSYS:
                enosys[nr] += 1
                first_enosys.setdefault(nr, n)
                blocker = nr in NET or (nr in PROC and not (nr == 220 and a0 & CLONE_VM and a0 & CLONE_THREAD))
                if blocker and first_blocker is None:
                    first_blocker = {"record": n, "tid": tid, "nr": nr, "name": NAMES.get(nr, str(nr)), "a0": hex(a0)}
            if r == ENOMEM:
                enomem_n += 1
            if r == ENOMEM and first_enomem is None:
                first_enomem = {"record": n, "nr": nr, "name": NAMES.get(nr, str(nr))}
            if first_blocker is not None and n > first_blocker["record"]:
                after += 1
            if ret.startswith("exit"):
                exits.append((n, tid, nr, ret))
    return {"records": n, "calls": {NAMES.get(k, str(k)): v for k, v in calls.most_common(12)},
            "enosys": {NAMES.get(k, str(k)): v for k, v in sorted(enosys.items(), key=lambda kv: -kv[1])},
            "first_enosys": {NAMES.get(k, str(k)): v for k, v in sorted(first_enosys.items(), key=lambda kv: kv[1])},
            "first_blocker": first_blocker, "calls_after_blocker": after, "first_enomem": first_enomem, "enomem_count": enomem_n, "exits": exits[-3:],
            "tail": [f"[{t}] {NAMES.get(nr, nr)}({a0:#x}) = {ret}" for (_n, t, nr, a0, ret) in list(ring)[-12:]]}


def parse_report(err):
    """The key: value lines after `--- armless-exec report` (None when armless-exec did not get that far)."""
    if "--- armless-exec report" not in err:
        return None
    rep = {}
    for line in err.split("--- armless-exec report", 1)[1].splitlines():
        k, sep, v = line.partition(": ")
        if sep:
            rep[k.strip()] = v.strip()
    rep["timed_out"] = "armless-exec: timed out" in err
    return rep


def tail(path, n=600):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def run_exec(a, fs_dir, argv, env, cwd, tag, timeout, traced=True):
    """One armless-exec run: (process facts, trace summary or None). Files: <out>/au-<tag>.{stdout,stderr,trace}."""
    out_p, err_p = (os.path.join(a.out, f"au-{tag}.{x}") for x in ("stdout", "stderr"))
    cmd = [os.path.abspath(a.armless_exec), "--fs", fs_dir, "--cwd", cwd, "-m", str(RAM_MB), "--timeout", str(int(timeout))]
    for kv in env:
        cmd += ["--env", kv]
    cmd += argv
    e = dict(os.environ)
    if traced:
        e["ARMLESS_EXEC_TRACE"] = "1"
    t0 = time.perf_counter()
    with open(out_p, "wb") as fo, open(err_p, "wb") as fe:
        p = subprocess.Popen(cmd, env=e, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe)
        try:
            code = p.wait(timeout + 120)  # armless-exec ends itself at --timeout (and scans the guest's RAM first)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            code = "killed by the harness"
    facts = {"cmd": " ".join(cmd), "exit": code, "wall_s": round(time.perf_counter() - t0, 1), "stdout_tail": tail(out_p), "stdout": out_p, "stderr": err_p}
    err = open(err_p, errors="replace").read() if os.path.getsize(err_p) < 2_000_000_000 else ""
    rep = parse_report(err)
    facts["report"] = rep
    if rep is None:
        facts["stderr_tail"] = tail(err_p)
    summary = parse_trace(err_p) if traced else None
    return facts, summary


# Arm crypto-extension instructions armless-exec does not implement (user/src/crypto.rs has AES, SHA1, SHA256 and CRC32 only; AT_HWCAP 0x1fb has no SM3,
# SM4, SHA512 or SHA3 bit): (mask, value, name); a fault is tied to a missing instruction only when its word decodes as one of these.
UNSUPPORTED_INSNS = [
    (0xffe08000, 0xce400000, "SM3SS1"), (0xffe0cc00, 0xce408000, "SM3TT1A"), (0xffe0cc00, 0xce408400, "SM3TT1B"),
    (0xffe0cc00, 0xce408800, "SM3TT2A"), (0xffe0cc00, 0xce408c00, "SM3TT2B"), (0xffe0fc00, 0xce60c000, "SM3PARTW1"), (0xffe0fc00, 0xce60c400, "SM3PARTW2"),
    (0xfffffc00, 0xcec08400, "SM4E"), (0xffe0fc00, 0xce60c800, "SM4EKEY"),
    (0xffe0fc00, 0xce608000, "SHA512H"), (0xffe0fc00, 0xce608400, "SHA512H2"), (0xfffffc00, 0xcec08000, "SHA512SU0"), (0xffe0fc00, 0xce608800, "SHA512SU1"),
    (0xffe08000, 0xce000000, "EOR3"), (0xffe08000, 0xce200000, "BCAX"), (0xffe0fc00, 0xce608c00, "RAX1"), (0xffe00000, 0xce800000, "XAR"),
]


def decode_unsupported(word):
    """The name of the crypto-extension instruction `word` ("0x........") encodes when armless-exec lacks it, else None."""
    try:
        w = int(word, 16)
    except (TypeError, ValueError):
        return None
    return next((n for mask, val, n in UNSUPPORTED_INSNS if w & mask == val), None)


def classify(facts, summary, fault=None):
    """(kind, text): kind is 'blocked' (the outcome is tied, by this run's own trace and report, to a call or an instruction armless-exec lacks) or
    'unclassified'. Meeting a blocker is not enough: the run must also end the way that blocker explains (exit status non-zero, or the fault at the
    missing instruction), with no ENOMEM anywhere in the trace and no timeout."""
    rep = facts["report"]
    if rep is None:
        return "unclassified", f"armless-exec did not reach its report (exit {facts['exit']}): {facts.get('stderr_tail', '')[-300:]!r}"
    status = rep.get("status", "")
    b = summary["first_blocker"]
    if summary["enomem_count"]:
        return "unclassified", f"{summary['enomem_count']} call(s) failed with ENOMEM (first {summary['first_enomem']}): the {RAM_MB} MiB of RAM may be what limits it"
    insn = decode_unsupported(fault["word"]) if fault and fault["ec"] == "0x0" and fault["word"] else None
    if b is None and insn:
        # no unsupported call at all: armless raised an undefined-instruction exception (EC 0x0) at a crypto instruction it does not implement
        return "blocked", (f"cpu: undefined-instruction exception (EC 0x0) at pc {fault['pc']} of {fault['exe']}, instruction word {fault['word']} ({insn}, "
                           f"not implemented); {summary['records']} calls before it, no unsupported call; exit {facts['exit']}")
    if b is None:
        return "unclassified", (f"no unsupported call in {summary['records']} calls; status {status}, exit {facts['exit']}; ENOSYS: {summary['enosys'] or 'none'}"
                                + (f"; fault word {fault['word']} is not a known unimplemented instruction" if fault else ""))
    if rep["timed_out"]:
        return "unclassified", f"after the first blocker ({b['name']} at record {b['record']}) the run timed out"
    m = re.fullmatch(r"Exited\((\d+)\)", status)
    if not m or int(m.group(1)) == 0:
        return "unclassified", f"after the first blocker ({b['name']} at record {b['record']}) the process did not exit non-zero: {status}"
    kind = "network" if b["nr"] in NET else "process creation/exec"
    return "blocked", (f"{kind}: {b['name']} ENOSYS at call {b['record']} of {summary['records']} (tid {b['tid']}); {summary['calls_after_blocker']} calls after it, then {status}"
                       f" (exit {facts['exit']})")


# ---- the controls

def control_t0(a, name, case, pkg_dir, env_extra, argv, want):
    """The control on T0: um-t0 run --image of the package copy whose Entrypoint is the control (Cmd cleared)."""
    cmd = [os.path.abspath(a.t0), "run", "--image", os.path.abspath(pkg_dir)]
    for kv in env_extra:
        cmd += ["--env", kv]
    out_p, err_p = (os.path.join(a.out, f"au-control-t0-{name}.{x}") for x in ("stdout", "stderr"))
    with open(out_p, "wb") as fo, open(err_p, "wb") as fe:
        try:
            code = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, timeout=a.timeout).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    text = open(out_p, errors="replace").read() + open(err_p, errors="replace").read()
    return {"ok": code == 0 and want in text, "exit": code, "output": text[-300:]}


def fault_word(fs_dir, path, pc):
    """The instruction word at `pc` of the static executable `path` of the armless filesystem (read from its blobs, the way armless-exec
    loads it), or None. A position-independent executable is loaded at PIE_BASE (user/src/lib.rs: 0x10_0000), a static one at 0; a pc in the
    dynamic loader or elsewhere is None."""
    import struct
    try:
        node = json.load(open(os.path.join(fs_dir, "fs.json")))["fsroot"]
        for part in [p for p in path.split("/") if p]:
            node = next(c for c in node if c[0] == part)[6]
        chunks = [[node, None]] if isinstance(node, str) else node
        data = b""
        for blob, _n in chunks:
            data += subprocess.run(["zstd", "-dc", os.path.join(fs_dir, "blobs", blob)], check=True, capture_output=True).stdout
        if data[:4] != b"\x7fELF" or struct.unpack_from("<H", data, 16)[0] not in (2, 3):
            return None
        base = PIE_BASE if struct.unpack_from("<H", data, 16)[0] == 3 else 0
        phoff, phentsize, phnum = struct.unpack_from("<Q", data, 32)[0], struct.unpack_from("<H", data, 54)[0], struct.unpack_from("<H", data, 56)[0]
        for i in range(phnum):
            typ, _fl, off, vaddr, _pa, filesz, _ms, _al = struct.unpack_from("<IIQQQQQQ", data, phoff + i * phentsize)
            if typ == 1 and base + vaddr <= pc < base + vaddr + filesz:
                return struct.unpack_from("<I", data, off + pc - base - vaddr)[0]
    except (OSError, StopIteration, KeyError, ValueError, struct.error, subprocess.CalledProcessError):
        return None
    return None


def fault_info(rep, fs_dir, exe):
    """{pc, exe, word, ec} of a guest fault in armless-exec's report (Fault("exception class 0x.. (esr ..) at pc 0x.., far ..")), or None."""
    m = re.search(r"exception class (0x[0-9a-f]+) .* at pc (0x[0-9a-f]+)", (rep or {}).get("status", ""))
    if not m:
        return None
    w = fault_word(fs_dir, exe, int(m.group(2), 16))
    return {"ec": m.group(1), "pc": m.group(2), "exe": exe, "word": None if w is None else f"0x{w:08x}"}


def controls(a, name, case, m3b, fs_dir, tree):
    if name not in CONTROLS:
        return {"note": "no control: the image's program (a static Go server) has no short terminating mode"}
    argv0, want = CONTROLS[name]
    ccase = {"pkg": case["pkg"], "entrypoint": argv0}
    pkg = m3b.package_for(a, "ctl-" + name, ccase)
    try:
        with open(os.path.join(pkg, "config.json")) as f:
            cfg = json.load(f)
        env_extra = list(case.get("env", []))
        argv, env, cwd, exe, shebang, _ids = launch_plan(cfg, tree, env_extra, [])
        run_argv = expand_shebang(argv, exe, shebang) if shebang else argv
        facts, summary = run_exec(a, fs_dir, run_argv, env, cwd, f"control-{name}", 120)
        rep = facts["report"] or {}
        ok = rep.get("status") == "Exited(0)" and want in facts["stdout_tail"]
        res = {"argv": argv0, "want": want, "armless_user": {"ok": ok, "status": rep.get("status"), "exit": facts["exit"], "output": facts["stdout_tail"][-200:]}}
        fl = fault_info(rep, fs_dir, exe)  # the instruction at the pc, read from the executable: what armless's report alone does not give
        if fl:
            res["armless_user"]["fault"] = fl
        if not ok and summary is not None:
            res["armless_user"].update(first_enosys=summary["first_enosys"], first_blocker=summary["first_blocker"], tail=summary["tail"][-6:])
            # the same causal rule as the main run: a failed control is an armless limitation only when its own trace and report say so
            res["armless_user"]["kind"], res["armless_user"]["verdict"] = classify(facts, summary, fl)
        res["t0"] = control_t0(a, name, case, pkg, env_extra, argv0, want)
        return res
    finally:  # the copy's root.erofs is a symlink the artifact upload would follow (hundreds of MB): not kept
        shutil.rmtree(pkg, ignore_errors=True)


# ---- the cell

def cell(a, name, case, m3b):
    """(ok, failure, metrics, text, evidence): ok when the outcome is classified (and the controls behave); the cell fails the job otherwise."""
    fs_dir = os.path.join(a.pkgs, case["pkg"], "armless")
    if not os.path.exists(os.path.join(fs_dir, "fs.json")):
        return False, f"no armless filesystem at {fs_dir} (the package job makes it)", {}, "-", {}
    tree = read_tree(fs_dir)
    pkg = m3b.package_for(a, name, case)
    with open(os.path.join(pkg, "config.json")) as f:
        cfg = json.load(f)
    try:
        argv, env, cwd, exe, shebang, ids = launch_plan(cfg, tree, list(case.get("env", [])), list(case.get("args", [])))
    except RuntimeError as e:
        return False, f"launch plan (T0 would refuse it too): {e}", {}, "-", {}
    run_argv = expand_shebang(argv, exe, shebang) if shebang else argv
    plan = {"argv": argv, "run_argv": run_argv, "env": env, "cwd": cwd, "exec": exe, "shebang": shebang, "image_user": cfg.get("User") or "", "ids": ids,
            "note": "armless user mode has no uid option: getuid is 0 whatever the image's User says"}
    print(f"armless-user {name}: {json.dumps(plan)}", flush=True)
    facts, summary = run_exec(a, fs_dir, run_argv, env, cwd, f"{name}", 120)
    fault = fault_info(facts["report"], fs_dir, run_argv[0] if shebang else exe)
    kind, text = classify(facts, summary, fault) if summary is not None else ("unclassified", "no trace")
    rep = facts["report"] or {}
    ev = {"plan": plan, "run": {k: v for k, v in facts.items() if k != "report"}, "report": rep, "fault": fault, "trace": summary, "kind": kind, "verdict": text}
    print(f"armless-user {name}: {kind}: {text}", flush=True)
    if summary:
        print(f"armless-user {name}: calls {summary['records']}, ENOSYS {summary['enosys'] or 'none'}, first ENOSYS at {summary['first_enosys']}; last calls: {'; '.join(summary['tail'][-4:])}", flush=True)
        print(f"armless-user {name}: stdout tail {facts['stdout_tail'][-300:]!r}", flush=True)
    failure = None if kind == "blocked" else f"unclassified outcome: {text}"
    try:
        ev["controls"] = controls(a, name, case, m3b, fs_dir, tree)
        print(f"armless-user {name}: controls {json.dumps(ev['controls'])[:900]}", flush=True)
    except RuntimeError as e:
        ev["controls"] = {"error": str(e)}
        failure = failure or f"control launch: {e}"
    ctl = ev["controls"].get("armless_user")
    if ctl is not None and not ctl["ok"]:
        # a control that fails on armless must be tied to an unsupported call as well; the table shows it either way
        if ctl.get("kind") != "blocked":
            failure = failure or f"control failed on armless-exec and is not tied to an unsupported call or instruction: {ctl.get('verdict') or ctl}"
    ctl0 = ev["controls"].get("t0")
    if ctl0 is not None and not ctl0["ok"]:
        failure = failure or f"control failed on T0 (the invocation is wrong, not armless): {ctl0}"
    with open(os.path.join(a.out, f"au-{name}-evidence.json"), "w") as f:
        json.dump(ev, f, indent=1)
    c = ev["controls"]
    if "armless_user" in c:
        fl = c["armless_user"].get("fault")
        c_txt = (f"control: armless-exec {'pass' if c['armless_user']['ok'] else 'FAIL'}"
                 + (f" ({c['armless_user']['status']}: instruction {fl['word']} at pc {fl['pc']} of {fl['exe']})" if fl else "")
                 + f", T0 {'pass' if c['t0']['ok'] else 'FAIL'}")
    else:
        c_txt = "control: " + c.get("note", c.get("error", "not run"))
    for tag in (name, f"control-{name}"):
        squeeze(os.path.join(a.out, f"au-{tag}.stderr"))
    return failure is None, failure, {}, f"{text}; {c_txt}", ev


def squeeze(path):
    import gzip, shutil
    try:
        if os.path.getsize(path) > 1_000_000:
            with open(path, "rb") as f, gzip.open(path + ".gz", "wb") as g:
                shutil.copyfileobj(f, g)
            os.remove(path)
    except OSError:
        pass
