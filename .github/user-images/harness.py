#!/usr/bin/env python3
"""The user-mode image harness of .github/workflows/user-images.yml: supa-wasm's seven gate images on armless's `armless-exec`.

  harness.py cell   --image NAME --fs DIR --armless-exec BIN --out DIR [--ready-timeout S] [--exec-arg ARG]...
  harness.py report --in DIR --out DIR [--source TEXT]

The images, their launch and the checks are supa-wasm's own (experiments/usermode/gates/{m2b,m3b,m4a} at um/int): m3b's SERVE table (the
requests, their exact status, content type and body), m2b's `check`, m4a's `expect_go`, m3b's hook flow (POST /ready, /run, the requests, POST /terminate
within the 15 s drain limit) and m4a's ready event (the first answer of the app, after the connect; for the hooks, after POST /run) and its "the process
must not have exited while serving" check. The launch plan and the report parsing and classification are m4a/armless_user.py (copied unchanged).

A cell is one image, two starts of it, both probed from the host with the same checks:
  untraced  armless-exec --fs DIR --cwd .. -m RAM --net --publish 8080:8080 [--publish 61009:61009 for the hook runtimes] --timeout T --env .. ARGV
            (ARGV: the image's Entrypoint+Cmd as the kernel would run it: a script is expanded to its `#!` interpreter, which armless does not do).
            Gives ready, served, the failed check (expected against actual) and the peak PSS: /proc/<pid>/smaps_rollup of armless-exec and its
            descendants summed every 200 ms from the spawn to the stop.
  traced    the same with ARMLESS_EXEC_TRACE=1, always, served or not. The trace is read from the pipe as it is written (it is far too large to keep)
            and scanned: every call that returned ENOSYS or EOPNOTSUPP is listed per call with its count, the record of the first, and sample
            arguments (the trace prints the first three); EINVAL, ENOPROTOOPT, EPROTONOSUPPORT, ESOCKTNOSUPPORT, EPFNOSUPPORT and EAFNOSUPPORT results
            are listed apart (an EINVAL is often not about support at all). On a failure the same scan classifies the first failure with armless_user.classify.
  logs      the guest's own output of both starts is searched for ENOSYS, "Function not implemented", EOPNOTSUPP, "Operation not supported", "not implemented",
            "not supported" and "unsupported": each distinct line is listed with its count.
`served` means every check passed: all of them, in order, the first mismatch is `failed check` with what it expected and what it got.
The host reaches the guest as 127.0.0.1:<port> through armless-exec's own --net/--publish. `--exec-arg` adds armless-exec options.
Exit status: 0 whatever the image does (its outcome is the result); 1 when the harness itself failed (the launch plan, a busy port, no armless-exec).
"""
import argparse, collections, glob, gzip, http.client, importlib.util, json, os, re, shutil, socket, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


au = load("armless_user", "m4a/armless_user.py")
SYSCALL_NAMES = load("syscalls", "syscalls.py").NAMES

IMAGES = ["go", "node", "nginx", "python", "shell", "deno", "node-runtime"]
HOOK_PORT = 61009
HOOK = "/aws/lambda-microvms/runtime/v1"
DRAIN_MS = 15000
CHECK_BUDGET_S = 240  # what armless-exec's own --timeout adds to the ready budget: the requests (3 x 30 s) and the terminate hook (60 s)
SAMPLE_S = 0.2  # the PSS sampler's period
DHAT_GRACE_S = 900  # what a dhat build gets to write its profile after SIGTERM (it symbolizes the backtraces then; the plain 10 s would kill it mid-write)

# the catalog's entrypoints (m3b LAUNCHER) and the hook deadlines build.sh raises (m3b LAUNCHER_ENV)
LAUNCHER = {
    "node-runtime": ["node", "/opt/workers/launcher.mjs", "--", "node", "/opt/workers/serve.mjs"],
    "deno": ["deno", "run", "-A", "/opt/workers/launcher.mjs", "--", "deno", "run", "-A", "--config", "/app/deno.json", "/opt/workers/serve-deno.ts"],
}
LAUNCHER_ENV = ["WORKERS_RUN_DEADLINE_MS=280000", "WORKERS_LISTEN_TIMEOUT_MS=280000"]


class Fail(Exception):
    """A check that did not pass: its name, what it expected, what it got."""
    def __init__(self, check, expected, actual):
        super().__init__(f"{check}: expected {expected}, got {actual}")
        self.check, self.expected, self.actual = check, expected, actual


def http_get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.getheader("content-type") or "", r.read().decode("utf-8", "replace")


def expect_go(port):
    """m4a's expect_go (gates/m1's checks of the Go image that need no database): /now is the guest's clock in ms, /rand 32 hex digits, different on two requests."""
    s, _t, b = http_get(port, "/now")
    if s != 200 or not b.strip().isdigit() or abs(int(b) - time.time() * 1000) > 24 * 3600 * 1000:
        raise Fail("GET /now", "200 and the guest's clock in ms (within a day of the host's)", f"{s} {b[:80]!r}")
    r = []
    for _ in range(2):
        s, _t, b = http_get(port, "/rand")
        if s != 200 or not re.fullmatch(r"[0-9a-f]{32}\n?", b):
            raise Fail("GET /rand", "200 and 32 hex digits", f"{s} {b[:80]!r}")
        r.append(b)
    if r[0] == r[1]:
        raise Fail("GET /rand (twice)", "two different answers", f"both {r[0]!r}")


# image -> how it starts (the image's own start unless `entrypoint`), the path whose first answer is ready, and what a request must answer
# (m3b SERVE; go and node are m4a's CASES). `port` is the guest's port the checks use, published on the host's same number.
CASES = {
    "go": {"port": 8080, "load": "/now", "requests": [], "expect": expect_go},
    "node": {"port": 8080, "load": "/", "requests": [("/", 200, "text/plain", "bench-node ok\n")]},
    "shell": {"port": 8080, "load": "/", "requests": [("/", 200, "text/", "bench-shell ok\n")]},
    "nginx": {"port": 8080, "load": "/", "requests": [("/", 200, "text/html", "bench-nginx ok\n"), ("/missing", 404, "text/html", None)]},
    "python": {"port": 8080, "load": "/", "requests": [("/", 200, "text/plain", "bench-python ok\n")]},
    "deno": {
        "port": 8080, "load": "/foo", "hook": True, "entrypoint": LAUNCHER["deno"], "env": ["GREETING=hi-deno", "DENO_NO_UPDATE_CHECK=1"] + LAUNCHER_ENV,
        "requests": [
            ("/foo", 200, "application/json", {"worker": "hello-deno", "path": "/foo", "greeting": "hi-deno"}),
            ("/bar/baz?x=1", 200, "application/json", {"worker": "hello-deno", "path": "/bar/baz", "greeting": "hi-deno"}),
        ],
    },
    "node-runtime": {
        "port": 8080, "load": "/foo", "hook": True, "entrypoint": LAUNCHER["node-runtime"], "env": ["GREETING=hi-node"] + LAUNCHER_ENV,
        "requests": [
            ("/foo", 200, "application/json", {"worker": "hello-node", "path": "/foo", "greeting": "hi-node"}),
            ("/bar/baz?x=1", 200, "application/json", {"worker": "hello-node", "path": "/bar/baz", "greeting": "hi-node"}),
        ],
    },
}


# ---- the probes

def check(port, case):
    """m2b's check: the case's requests, each on its own connection; Fail on the first mismatch."""
    for path, status, ctype, want in case["requests"]:
        name = f"GET {path}"
        expected = f"{status} {ctype}*" + ("" if want is None else f" {want!r}")
        try:
            r_status, got_type, body = http_get(port, path)
        except (OSError, http.client.HTTPException) as e:
            raise Fail(name, expected, f"{type(e).__name__}: {e}")
        ok = r_status == status and got_type.startswith(ctype)
        if ok and want is not None:
            try:
                ok = (json.loads(body) if isinstance(want, dict) else body) == want
            except ValueError:
                ok = False
        if not ok:
            raise Fail(name, expected, f"{r_status} {got_type!r} {body[:200]!r}")


def post(port, path, timeout):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request("POST", path, body=b"")
    r = c.getresponse()
    return r.status, r.read().decode("utf-8", "replace")


def port_busy(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def connect_wait(p, port, deadline, budget, what):
    """m3b's wait_connect: until a connect to 127.0.0.1:port succeeds. The process ending first, or the budget passing with it alive, is a Fail."""
    check_name = f"ready: connect 127.0.0.1:{port} ({what})"
    while True:
        if p.poll() is not None:
            raise Fail(check_name, "a listener", f"armless-exec exited with status {p.returncode} first")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise Fail(check_name, "a listener", f"nothing listening after {budget:g} s, armless-exec still running")
            time.sleep(0.1)


def first_answer(p, port, path, deadline, budget):
    """m4a's ready event: the first answer of the app to GET `path` (m4a's first_response wants a 200; any answer is taken here and the exact
    check that follows says what was wrong with it, so a wrong status is reported as that and not as a timeout)."""
    name = f"ready: first answer to GET {path}"
    last = "no connection"
    while True:
        if p.poll() is not None:
            raise Fail(name, "an HTTP answer", f"armless-exec exited with status {p.returncode} first ({last})")
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path)
            c.getresponse().read()
            return
        except (OSError, http.client.HTTPException) as e:
            last = f"{type(e).__name__}: {e}"
        if time.monotonic() > deadline:
            raise Fail(name, "an HTTP answer", f"none after {budget:g} s, armless-exec still running ({last})")
        time.sleep(0.01)


def exercise(p, case, budget, t0):
    """Ready, then the checks, on the process `p`: {ready, served, ready_s, listen_s, failed_check, expected, actual, failure, ...}."""
    res = {"ready": False, "served": False, "ready_s": None, "listen_s": None, "failed_check": None, "expected": None, "actual": None, "failure": None}
    deadline = t0 + budget
    hook, port = case.get("hook"), case["port"]
    step = "start"
    try:
        if hook:
            connect_wait(p, HOOK_PORT, deadline, budget, "the launcher's hook port")
            res["listen_s"] = round(time.monotonic() - t0, 1)
            for path, want in (("/ready", "ready\n"), ("/run", "running\n")):
                step = f"POST {path}"
                try:
                    status, body = post(HOOK_PORT, HOOK + path, max(1.0, deadline - time.monotonic()))
                except (OSError, http.client.HTTPException) as e:
                    raise Fail(step, f"200 {want!r}", f"{type(e).__name__}: {e}")
                if (status, body) != (200, want):
                    raise Fail(step, f"200 {want!r}", f"{status} {body[:200]!r}")
        else:
            connect_wait(p, port, deadline, budget, "listening")
            res["listen_s"] = round(time.monotonic() - t0, 1)
        first_answer(p, port, case["load"], deadline, budget)
        res["ready"], res["ready_s"] = True, round(time.monotonic() - t0, 1)
        step = "checks"
        if case["requests"]:
            check(port, case)
        if case.get("expect"):
            case["expect"](port)
        if p.poll() is not None:  # m4a: an exit during the checks is a failure of the start
            raise Fail("process alive while serving", "armless-exec running", f"exited with status {p.returncode}")
        if hook:
            step = "POST /terminate"
            t1 = time.monotonic()
            try:
                status, body = post(HOOK_PORT, HOOK + "/terminate", 60)
            except (OSError, http.client.HTTPException) as e:
                raise Fail(step, "200 'terminating\\n'", f"{type(e).__name__}: {e}")
            res["terminate_ms"] = round((time.monotonic() - t1) * 1000)
            if (status, body) != (200, "terminating\n"):
                raise Fail(step, "200 'terminating\\n'", f"{status} {body[:200]!r}")
            if res["terminate_ms"] > DRAIN_MS:
                raise Fail("POST /terminate within the drain limit", f"an answer within {DRAIN_MS} ms (the app exits on SIGTERM)", f"{res['terminate_ms']} ms")
        res["served"] = True
    except Fail as e:
        res.update(failed_check=e.check, expected=e.expected, actual=e.actual, failure=str(e))
    except (OSError, http.client.HTTPException) as e:
        res.update(failed_check=step, expected="no transport error", actual=f"{type(e).__name__}: {e}", failure=f"{step}: {type(e).__name__}: {e}")
    if hook and res["served"]:
        try:  # not a check (m4a only looks at it on T1): did the guest end after the terminate? then armless-exec has printed its report
            p.wait(5)
        except subprocess.TimeoutExpired:
            pass
        res["exited_after_terminate"] = p.poll() is not None
    return res


# ---- peak PSS: armless-exec and its descendants, /proc/<pid>/smaps_rollup

def tree_pids(root):
    kids = collections.defaultdict(list)
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as f:
                    s = f.read()
                kids[int(s[s.rindex(")") + 2:].split()[1])].append(int(d))
            except (OSError, ValueError, IndexError):
                continue
    out, stack = [root], [root]
    while stack:
        for c in kids.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def pss_kib(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


class PssSampler(threading.Thread):
    """The summed PSS of the process tree every SAMPLE_S from the spawn until `halt`: samples are (seconds since t0, KiB)."""
    def __init__(self, pid, t0):
        super().__init__(daemon=True)
        self.pid, self.t0, self.samples, self.done = pid, t0, [], threading.Event()

    def run(self):
        while True:
            try:
                total = sum(pss_kib(x) for x in tree_pids(self.pid))
            except OSError:
                total = 0
            if total:
                self.samples.append((time.monotonic() - self.t0, total))
            if self.done.wait(SAMPLE_S):
                return

    def halt(self):
        self.done.set()
        self.join(10)

    def peak_mib(self):
        return round(max((k for _t, k in self.samples), default=0) / 1024, 1) or None

    def at_mib(self, t):
        """The first sample at or after `t` seconds."""
        return next((round(k / 1024, 1) for s, k in self.samples if s >= t), None)


# ---- the trace, scanned as it is written

ERRNO_UNSUPPORTED = {38: "ENOSYS", 95: "EOPNOTSUPP"}
ERRNO_OTHER = {22: "EINVAL", 92: "ENOPROTOOPT", 93: "EPROTONOSUPPORT", 94: "ESOCKTNOSUPPORT", 96: "EPFNOSUPPORT", 97: "EAFNOSUPPORT"}
# a call whose ENOSYS the guest's libc expects (it probes and falls back): still listed, and marked
PROBES = {293: "rseq (glibc registers it and goes on without)", 435: "clone3 (glibc falls back to clone)"}
TRACE_LINE = re.compile(rb"^\[(\d+)\] sys (\d+)\((0x[0-9a-f]+), (0x[0-9a-f]+), (0x[0-9a-f]+)\) = (.*)$")
NOISE_CAP = 8 << 20
HEAD_LINES = 20000
RING = 400


class TraceScanner:
    """Fed the lines of armless-exec's stderr as they arrive: the trace is counted (calls, results that mean `unsupported`, the first blocker for
    armless_user.classify) and everything that is not a trace record (armless-exec's messages, its final report) goes to `noise_path`."""

    def __init__(self, noise_path):
        self.n = 0
        self.calls = collections.Counter()
        self.hits = {}  # (nr, errno) -> {count, first, args}
        self.first_blocker = None
        self.after = 0
        self.first_enomem, self.enomem_n = None, 0
        self.exits = []
        self.ring = collections.deque(maxlen=RING)
        self.head = []
        self.noise = open(noise_path, "wb")
        self.noise_bytes = 0
        self.in_report = False

    def feed(self, raw):
        m = TRACE_LINE.match(raw.rstrip(b"\n"))
        if not m:
            if raw.startswith(b"--- armless-exec report"):
                self.in_report = True
            if self.in_report or self.noise_bytes < NOISE_CAP:
                self.noise.write(raw)
                self.noise_bytes += len(raw)
            return
        self.n += 1
        n = self.n
        tid, nr, a0, a1, a2, ret = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4), m.group(5), m.group(6)
        self.calls[nr] += 1
        self.ring.append(raw)
        if n <= HEAD_LINES:
            self.head.append(raw)
        if self.first_blocker is not None:
            self.after += 1
        if ret[:2] == b"0x" and len(ret) == 18 and ret[2:3] == b"f":
            errno = (1 << 64) - int(ret, 16)
            if errno in ERRNO_UNSUPPORTED or errno in ERRNO_OTHER:
                h = self.hits.get((nr, errno))
                if h is None:
                    h = self.hits[(nr, errno)] = {"count": 0, "first": n, "tid": tid, "args": []}
                h["count"] += 1
                args = (a0.decode(), a1.decode(), a2.decode())
                if len(h["args"]) < 3 and args not in h["args"]:
                    h["args"].append(args)
                if errno == 38 and self.first_blocker is None:
                    a0v = int(a0, 16)
                    if nr in au.NET or (nr in au.PROC and not (nr == 220 and a0v & au.CLONE_VM and a0v & au.CLONE_THREAD)):
                        self.first_blocker = {"record": n, "tid": tid, "nr": nr, "name": au.NAMES.get(nr, str(nr)), "a0": hex(a0v)}
            elif errno == 12:
                self.enomem_n += 1
                if self.first_enomem is None:
                    self.first_enomem = {"record": n, "nr": nr, "name": au.NAMES.get(nr, str(nr))}
        elif ret[:4] == b"exit" or ret[:11] == b"thread exit":
            if ret[:4] == b"exit":
                self.exits.append((n, tid, nr, ret.decode()))
                del self.exits[:-3]

    def close(self):
        self.noise.close()

    @staticmethod
    def name(nr):
        return SYSCALL_NAMES.get(nr, str(nr))

    def entries(self, errnos):
        out = []
        for (nr, errno), h in self.hits.items():
            if errno in errnos:
                out.append({"nr": nr, "name": self.name(nr), "errno": errno, "errno_name": errnos[errno], "count": h["count"], "first_record": h["first"],
                            "args": [list(a) for a in h["args"]], "note": PROBES.get(nr) if errno == 38 else None})
        return sorted(out, key=lambda e: (-e["count"], e["nr"]))

    def summary(self):
        """The shape of armless_user.parse_trace's, for armless_user.classify."""
        enosys = collections.Counter()
        first = {}
        for (nr, errno), h in self.hits.items():
            if errno == 38:
                enosys[au.NAMES.get(nr, str(nr))] += h["count"]
                first[au.NAMES.get(nr, str(nr))] = h["first"]
        tail = []
        for raw in list(self.ring)[-12:]:
            m = TRACE_LINE.match(raw.rstrip(b"\n"))
            if m:
                nr = int(m.group(2))
                tail.append(f"[{int(m.group(1))}] {au.NAMES.get(nr, nr)}({int(m.group(3), 16):#x}) = {m.group(6).decode()}")
        return {"records": self.n, "calls": {self.name(k): v for k, v in self.calls.most_common(12)},
                "enosys": dict(enosys.most_common()), "first_enosys": dict(sorted(first.items(), key=lambda kv: kv[1])),
                "first_blocker": self.first_blocker, "calls_after_blocker": self.after, "first_enomem": self.first_enomem, "enomem_count": self.enomem_n,
                "exits": self.exits, "tail": tail}


# ---- the guest's own messages

LOG_PATTERN = re.compile(r"ENOSYS|EOPNOTSUPP|Function not implemented|Operation not supported|not implemented|not supported|unsupported", re.I)


def scan_log(path, source, limit=64 << 20):
    """Every distinct line of the file that says something is not implemented or supported, with its count."""
    counts = collections.Counter()
    try:
        with open(path, "rb") as f:
            text = f.read(limit).decode("utf-8", "replace")
    except OSError:
        return []
    text = text.split("--- armless-exec report", 1)[0]  # the report's own lines list opens and call tables, not messages
    for line in text.splitlines():
        if LOG_PATTERN.search(line):
            counts[line.strip()[:300]] += 1
    return [{"source": source, "line": l, "count": c} for l, c in counts.most_common(40)]


# ---- one run of the image

def start(a, plan, case, tag, traced, timeout):
    """armless-exec on the plan, with its network and the publish flags of the case: (process, files, command, stdout path, stderr path).
    Untraced, stderr is a file; traced it is a pipe (the trace is scanned as it is written) and <out>/au-<tag>.stderr is made from its non-trace lines."""
    out_p, err_p = (os.path.join(a.out, f"au-{tag}.{x}") for x in ("stdout", "stderr"))
    cmd = [os.path.abspath(a.armless_exec), "--fs", a.fs, "--cwd", plan["cwd"], "-m", str(au.RAM_MB), "--timeout", str(int(timeout)), "--net",
           "--publish", f"{case['port']}:{case['port']}"]
    if case.get("hook"):
        cmd += ["--publish", f"{HOOK_PORT}:{HOOK_PORT}"]
    cmd += list(a.exec_arg)
    for kv in plan["env"]:
        cmd += ["--env", kv]
    cmd += plan["run_argv"]
    env = dict(os.environ)
    if traced:
        env["ARMLESS_EXEC_TRACE"] = "1"
    if a.dhat:  # a dhat build (--features dhat-heap): the profile of the untraced start only; set and empty, the traced start does not run the profiler
        env["ARMLESS_DHAT_OUT"] = "" if traced else os.path.join(a.out, "dhat-heap.json")
    fo = open(out_p, "wb")
    fe = subprocess.PIPE if traced else open(err_p, "wb")
    p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe)
    return p, [fo] + ([] if traced else [fe]), cmd, out_p, err_p


def stop(p, files, grace=10):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(grace)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    for f in files:
        f.close()


def tail_lines(path, n=40, chunk=1 << 20):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - chunk))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def run_once(a, plan, case, tag, traced, budget):
    t0 = time.monotonic()
    p, files, cmd, out_p, err_p = start(a, plan, case, tag, traced, budget + CHECK_BUDGET_S)
    print(f"[{tag}] {' '.join(cmd)}", flush=True)
    scanner = pump = sampler = None
    if traced:
        scanner = TraceScanner(err_p)

        def drain():
            for raw in p.stderr:
                scanner.feed(raw)
        pump = threading.Thread(target=drain, daemon=True)
        pump.start()
    else:
        sampler = PssSampler(p.pid, t0)
        sampler.start()
    try:
        res = exercise(p, case, budget, t0)
    finally:
        # the process may have ended by itself with its report: give it a moment to flush before stopping it
        time.sleep(0.5)
        exited = p.poll() is not None
        stop(p, files, DHAT_GRACE_S if a.dhat and not traced else 10)
        if sampler:
            sampler.halt()
        if pump:
            pump.join(60)
            scanner.close()
    res.update({"tag": tag, "cmd": " ".join(cmd), "exited_on_its_own": exited, "exit": p.returncode, "wall_s": round(time.monotonic() - t0, 1), "stdout": out_p, "stderr": err_p})
    if sampler:
        res["peak_pss_mib"] = sampler.peak_mib()
        res["pss_ready_mib"] = sampler.at_mib(res["ready_s"] + 0.15) if res["ready_s"] is not None else None
        res["pss_samples"] = len(sampler.samples)
    if scanner:
        res["scanner"] = scanner
        with gzip.open(os.path.join(a.out, f"au-{tag}.trace-head.gz"), "wb") as g:
            g.writelines(scanner.head)
        with open(os.path.join(a.out, f"au-{tag}.trace-tail.log"), "wb") as f:
            f.writelines(scanner.ring)
    rep = au.parse_report("\n".join(tail_lines(err_p, 400, 1 << 20)))
    res["report"] = rep
    print(f"[{tag}] ready={res['ready']} served={res['served']} exit={p.returncode} report-status={(rep or {}).get('status')} failure={res['failure']}"
          + (f" peak-pss={res['peak_pss_mib']} MiB" if sampler else f" trace-records={scanner.n}"), flush=True)
    return res


def squeeze(path):
    au.squeeze(path)


def unsupported_block(scanner):
    """The traced run's list: every call that returned ENOSYS or EOPNOTSUPP, and (apart) the errnos that can mean the same."""
    calls = scanner.entries(ERRNO_UNSUPPORTED)
    return {"total": sum(c["count"] for c in calls), "calls": calls, "other": scanner.entries(ERRNO_OTHER), "trace_records": scanner.n}


def cmd_cell(a):
    os.makedirs(a.out, exist_ok=True)
    name, case = a.image, CASES[a.image]
    result = {"image": name, "ready": False, "served": False, "failed_check": None, "expected": None, "actual": None, "first_failure": None, "kind": None,
              "harness_error": None, "ram_mb": au.RAM_MB, "hook": bool(case.get("hook"))}

    def finish(rc=0):
        with open(os.path.join(a.out, "result.json"), "w") as f:
            json.dump(result, f, indent=1)
        print(f"{name}: ready={result['ready']} served={result['served']} failed check: {result['failed_check']}; first failure: {result['first_failure']}", flush=True)
        return rc

    try:
        tree = au.read_tree(a.fs)
        with open(os.path.join(a.fs, "config.json")) as f:
            cfg = json.load(f)
        if "entrypoint" in case:  # m3b's package_for: the case's Entrypoint, no Cmd
            cfg["Entrypoint"], cfg["Cmd"] = case["entrypoint"], None
        argv, env, cwd, exe, shebang, ids = au.launch_plan(cfg, tree, list(case.get("env", [])), [])
    except (OSError, RuntimeError, ValueError) as e:
        result["harness_error"] = f"launch plan: {e}"
        return finish(1)
    run_argv = au.expand_shebang(argv, exe, shebang) if shebang else argv
    plan = {"argv": argv, "run_argv": run_argv, "env": env, "cwd": cwd, "exec": exe, "shebang": shebang, "image_user": cfg.get("User") or "", "ids": ids}
    result["plan"] = plan
    print(f"{name}: {json.dumps(plan)}", flush=True)
    if not os.access(a.armless_exec, os.X_OK):
        result["harness_error"] = f"{a.armless_exec} is not executable"
        return finish(1)
    for port in {case["port"], HOOK_PORT}:
        if port_busy(port):
            result["harness_error"] = f"127.0.0.1:{port} is already in use on this host: a connect there would not say anything about the guest"
            return finish(1)

    # 1. untraced: ready, the checks, the memory
    run = run_once(a, plan, case, name, False, a.ready_timeout)
    for k in ("ready", "served", "ready_s", "listen_s", "failed_check", "expected", "actual", "terminate_ms", "exited_after_terminate", "peak_pss_mib", "pss_ready_mib", "pss_samples"):
        if k in run:
            result[k] = run[k]
    result.update(failure=run["failure"], exit=run["exit"], status=(run["report"] or {}).get("status"), cmd=run["cmd"], wall_s=run["wall_s"],
                  stderr_tail=tail_lines(run["stderr"]), stdout_tail=tail_lines(run["stdout"], 20))
    logs = scan_log(run["stdout"], "guest output") + scan_log(run["stderr"], "armless-exec stderr")
    squeeze(run["stderr"])
    if a.dhat:  # the profile's top allocation sites at its peak, in the artifact next to dhat-heap.json
        dh = os.path.join(a.out, "dhat-heap.json")
        if os.path.exists(dh):
            top = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "dhat_top.py"), dh], capture_output=True, text=True)
            with open(os.path.join(a.out, "dhat-top.txt"), "w") as f:
                f.write(top.stdout + top.stderr)
            result["dhat"] = f"dhat-top.txt ({os.path.getsize(dh)} bytes of dhat-heap.json)" if top.returncode == 0 else f"dhat_top.py exit {top.returncode}"
        else:
            result["dhat"] = "no dhat-heap.json written (a binary without --features dhat-heap, or killed before the write)"
        print(f"{name}: dhat: {result['dhat']}", flush=True)

    # 2. traced, always: every call armless answered ENOSYS or EOPNOTSUPP, and the first failure's classification
    tr = run_once(a, plan, case, name + "-trace", True, a.ready_timeout)
    sc = tr["scanner"]
    result["unsupported"] = unsupported_block(sc)
    result["traced_served"] = tr["served"]
    result["traced_failure"] = tr["failure"]
    result["traced_failed_check"] = tr["failed_check"]
    result["traced_status"] = (tr["report"] or {}).get("status")
    if tr["report"] and tr["report"].get("unknown_syscalls"):
        result["report_unknown_syscalls"] = tr["report"]["unknown_syscalls"]
    logs += scan_log(tr["stdout"], "guest output (traced start)") + scan_log(tr["stderr"], "armless-exec stderr (traced start)")
    result["log_messages"] = logs
    result["trace"] = summary = sc.summary() if sc.n else None
    result["traced_stderr_tail"] = tail_lines(tr["stderr"])
    result["traced_stdout_tail"] = tail_lines(tr["stdout"], 20)

    if run["served"]:
        result["kind"] = "served"
        result["first_failure"] = None
        if not tr["served"]:  # m3b: a start that fails only under the trace is a failing schedule, not a footnote
            result["first_failure"] = f"the traced start failed (the untraced one passed): {tr['failure']}"
        squeeze(tr["stderr"])
        return finish()

    # the failure: the traced run's own trace and report say which call or instruction ended it
    rep = tr["report"]
    fault = au.fault_info(rep, a.fs, run_argv[0] if shebang else exe) if rep else None
    if tr["served"]:
        kind, verdict = "unclassified", f"the traced start served; the untraced one failed: {run['failure']}"
    elif summary is not None and rep is not None:
        kind, verdict = au.classify({"report": rep, "exit": tr["exit"], "stderr_tail": ""}, summary, fault)
    elif (m_ := re.findall(r"^armless-exec: (.+)$", open(tr["stderr"], errors="replace").read(), re.M)):
        # the runner refused the program before running a single instruction (the ELF loader, the filesystem)
        kind, verdict = "loader", f"armless-exec refused to start the program: {m_[-1]} (exit {tr['exit']}, before the first instruction)"
    elif summary is not None:
        kind, verdict = "unclassified", (f"armless-exec did not reach its report in the traced run (exit {tr['exit']})"
                                         + ("" if tr["failure"] == run["failure"] else f"; the traced start: {tr['failure']}"))
    else:
        kind, verdict = "unclassified", "the traced run left no trace"
    result.update(kind=kind, verdict=verdict, fault=fault)
    result["first_failure"] = verdict if kind in ("blocked", "loader") else f"{run['failure']}; {verdict}"
    squeeze(tr["stderr"])
    return finish()


# ---- the table

def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def calls_cell(r):
    u = r.get("unsupported")
    if u is None:
        return "?"
    if not u["calls"]:
        return "0"
    parts = [f"{c['name']}({c['errno_name']}) x{c['count']}" for c in u["calls"][:6]]
    if len(u["calls"]) > 6:
        parts.append(f"+{len(u['calls']) - 6} more")
    return f"{u['total']}: " + ", ".join(parts)


def failed_cell(r):
    if r.get("failed_check"):
        return r["failed_check"]
    if r.get("traced_failed_check"):
        return f"(traced start) {r['traced_failed_check']}"
    return "-"


def cmd_report(a):
    rows, details = [], []
    for name in IMAGES:
        mine = glob.glob(os.path.join(a.inp, f"cell-{name}", "result.json"))
        if not mine:
            rows.append({"image": name, "ready": None, "served": None, "first_failure": "no result: the cell job failed before the harness wrote one (see the run)"})
            continue
        with open(mine[0]) as f:
            r = json.load(f)
        if r.get("harness_error"):
            r["first_failure"] = f"HARNESS ERROR: {r['harness_error']}"
        rows.append(r)
        details.append(r)
    yn = lambda v: "?" if v is None else ("yes" if v else "no")
    num = lambda v: "-" if v is None else v
    ram = next((r["ram_mb"] for r in details if r.get("ram_mb")), au.RAM_MB)
    out = ["## armless user mode: supa-wasm gate images", "", (a.source or "").strip(), "",
           f"guest RAM `-m {ram}` MiB; `--net --publish 8080:8080` (and `--publish {HOOK_PORT}:{HOOK_PORT}` for the hook runtimes deno and node-runtime).", "",
           "| image | ready (s) | served (all checks) | failed check | ENOSYS/EOPNOTSUPP calls | peak PSS MiB | first failure |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        ff = r["first_failure"] or "-"
        out.append(f"| {r['image']} | {num(r.get('ready_s'))} | {yn(r['served'])} | {esc(failed_cell(r))} | {esc(calls_cell(r))} | {num(r.get('peak_pss_mib'))} | {esc(ff)} |")
    out += ["", "ready: the first answer of the app (the hook runtimes: after POST /ready and /run), from the spawn. served: every check passed (the exact requests, "
            "the alive check, and for the hook runtimes POST /terminate within the 15 s drain limit). peak PSS: armless-exec and its descendants, /proc smaps_rollup every "
            f"{SAMPLE_S * 1000:.0f} ms over the whole untraced start. Calls: the traced start's trace, all calls that returned ENOSYS or EOPNOTSUPP (listed per image below).", ""]
    for r in details:
        out += [f"### {r['image']}", ""]
        if r.get("failed_check"):
            out.append(f"failed check `{esc(r['failed_check'])}`: expected {esc(r.get('expected'))}; got {esc(r.get('actual'))}")
        if r.get("status"):
            out.append(f"untraced start: status `{r['status']}`, exit {r.get('exit')}, {r.get('wall_s')} s; listening at {r.get('listen_s')} s, ready at {r.get('ready_s')} s"
                       f"; peak PSS {r.get('peak_pss_mib')} MiB, at ready {r.get('pss_ready_mib')} MiB; harness: {esc(r.get('failure') or 'none')}")
        if r.get("terminate_ms") is not None:
            out.append(f"POST /terminate answered in {r['terminate_ms']} ms; armless-exec ended after it: {r.get('exited_after_terminate')}")
        if r.get("traced_status") is not None:
            out.append(f"traced start: status `{r['traced_status']}`, served: {r.get('traced_served')}; harness: {esc(r.get('traced_failure') or 'none')}")
        t = r.get("trace") or {}
        if t:
            out.append(f"trace: {t.get('records')} calls; most frequent: {esc(t.get('calls'))}")
        if r.get("fault"):
            out.append(f"fault: {r['fault']}")
        u = r.get("unsupported")
        if u is not None:
            out += ["", f"**Calls that returned ENOSYS or EOPNOTSUPP** (traced start, {u['trace_records']} calls scanned): " + (f"{u['total']} in {len(u['calls'])} kinds" if u["calls"] else "none"), ""]
            if u["calls"]:
                out += ["| call | nr | result | count | first at call | sample arguments (a0, a1, a2) | note |", "|---|---|---|---|---|---|---|"]
                for c in u["calls"]:
                    out.append(f"| {c['name']} | {c['nr']} | {c['errno_name']} | {c['count']} | {c['first_record']} | {esc('; '.join('(' + ', '.join(x) + ')' for x in c['args']))} | {esc(c.get('note') or '')} |")
            if u["other"]:
                out += ["", "Other results of the same family (informational: an EINVAL is often about the arguments, not about support):", "",
                        "| call | nr | result | count | first at call | sample arguments (a0, a1, a2) |", "|---|---|---|---|---|---|"]
                for c in u["other"]:
                    out.append(f"| {c['name']} | {c['nr']} | {c['errno_name']} | {c['count']} | {c['first_record']} | {esc('; '.join('(' + ', '.join(x) + ')' for x in c['args']))} |")
        if r.get("report_unknown_syscalls") not in (None, "{}"):
            out.append(f"\narmless-exec's own report, unknown_syscalls: `{esc(r['report_unknown_syscalls'])}`")
        logs = r.get("log_messages") or []
        out += ["", "**ENOSYS / not-supported messages in the image's own output:** " + ("none" if not logs else f"{len(logs)} distinct line(s)")]
        if logs:
            out += ["", "| source | count | line |", "|---|---|---|"] + [f"| {esc(l['source'])} | {l['count']} | `{esc(l['line'])}` |" for l in logs]
        for title, key in (("last 40 lines of stderr (untraced)", "stderr_tail"), ("last lines of the guest's output", "stdout_tail")):
            lines = r.get(key) or []
            out += ["", f"<details><summary>{title}</summary>", "", "```", *[l[:300] for l in lines], "```", "", "</details>"]
        if not r.get("served"):
            out += ["", "<details><summary>last 40 lines of stderr (traced)</summary>", "", "```", *[l[:300] for l in (r.get("traced_stderr_tail") or [])], "```", "", "</details>"]
        out.append("")
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "summary.md"), "w") as f:
        f.write("\n".join(out) + "\n")
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump({"source": a.source, "rows": rows}, f, indent=1)
    print("\n".join(out))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cell")
    c.add_argument("--image", required=True, choices=IMAGES)
    c.add_argument("--fs", required=True)
    c.add_argument("--armless-exec", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--ready-timeout", type=float, default=300)
    c.add_argument("--exec-arg", action="append", default=[])
    c.add_argument("--dhat", action="store_true", help="the binary was built with --features dhat-heap: profile the untraced start (ARMLESS_DHAT_OUT in --out)")
    r = sub.add_parser("report")
    r.add_argument("--in", dest="inp", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--source", default="")
    a = ap.parse_args()
    sys.exit(cmd_cell(a) if a.cmd == "cell" else cmd_report(a))


if __name__ == "__main__":
    main()
