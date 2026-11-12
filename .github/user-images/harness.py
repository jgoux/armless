#!/usr/bin/env python3
"""The user-mode image harness of .github/workflows/user-images.yml: supa-wasm's seven gate images on armless's `armless-exec`.

  harness.py cell   --image NAME --fs DIR --armless-exec BIN --out DIR [--ready-timeout S] [--exec-arg ARG]...
  harness.py report --in DIR --out DIR [--source TEXT]

The images, their launch and the checks are supa-wasm's own (experiments/usermode/gates/{m2a,m2b,m3b,m4a} at um/int: the requests below are m3b's
SERVE table, m2b's `check`, m4a's `expect_go`, m3b's hook flow); the launch plan, the trace and report parsing and the classification are
m4a/armless_user.py (copied unchanged). What differs from supa-wasm's armless-user row: that row never tried to serve (armless 0.11 had no sockets) and
classified the first unsupported call; this one starts the image, probes it from the host and applies the serve checks, so a ref that adds sockets
and processes shows how far each image gets, and still classifies the first failure from a traced run.

`cell` is one image: `--fs` is the directory armless_fs.py made (fs.json, blobs/, tree.json, config.json).
  start   armless-exec --fs DIR --cwd .. -m 3072 --timeout T --env .. ARGV (ARGV: the image's Entrypoint+Cmd as the kernel would run it: a script
          is expanded to its `#!` interpreter, which armless does not do), stdin closed, output to files
  ready   non-hook images: a connect to 127.0.0.1:8080 on the host succeeds; the hook runtimes (deno, node-runtime): the launcher answers POST /ready
          and POST /run (hook port 61009), which starts the app and answers once it listens
  served  the image's exact requests are answered as m3b/m2b expect (go: /now and /rand), and for the hook runtimes POST /terminate ends it
  failure the process ending first, or the budget passing with it alive, is the first failure; the image is then started again with ARMLESS_EXEC_TRACE=1
          (the same probes, a shorter budget, a cap on the trace's size) and that run's trace and report classify it with armless_user.classify: the first
          unsupported call (socket, a clone that is not a thread, execve) or the instruction armless lacks, with the last 40 lines of its stderr
The host reaches the guest only as 127.0.0.1:<the guest's port>: what a ref has to do for ready to become true. `--exec-arg` adds armless-exec options.
Exit status: 0 whatever the image does (its outcome is the result); 1 when the harness itself failed (the launch plan, a busy port, no armless-exec).
"""
import argparse, glob, http.client, importlib.util, json, os, re, shutil, socket, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


au = load("armless_user", "m4a/armless_user.py")

IMAGES = ["go", "node", "nginx", "python", "shell", "deno", "node-runtime"]
HOOK_PORT = 61009
HOOK = "/aws/lambda-microvms/runtime/v1"
DRAIN_MS = 15000
TRACE_CAP = 512 << 20  # the traced run is killed when its stderr (the trace) passes this
TRACE_BUDGET_S = 120
CHECK_BUDGET_S = 240  # what armless-exec's own --timeout adds to the ready budget: the requests (3 x 30 s) and the terminate hook (60 s)

# the catalog's entrypoints (m3b LAUNCHER) and the hook deadlines build.sh raises (m3b LAUNCHER_ENV)
LAUNCHER = {
    "node-runtime": ["node", "/opt/workers/launcher.mjs", "--", "node", "/opt/workers/serve.mjs"],
    "deno": ["deno", "run", "-A", "/opt/workers/launcher.mjs", "--", "deno", "run", "-A", "--config", "/app/deno.json", "/opt/workers/serve-deno.ts"],
}
LAUNCHER_ENV = ["WORKERS_RUN_DEADLINE_MS=280000", "WORKERS_LISTEN_TIMEOUT_MS=280000"]


def expect_go(port):
    """m4a's expect_go (gates/m1's checks of the Go image that need no database): /now is the guest's clock in ms, /rand 32 hex digits, different on two requests."""
    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read().decode()
    s, b = get("/now")
    if s != 200 or not b.strip().isdigit() or abs(int(b) - time.time() * 1000) > 24 * 3600 * 1000:
        raise RuntimeError(f"GET /now: {s} {b[:80]!r}")
    r = []
    for _ in range(2):
        s, b = get("/rand")
        if s != 200 or not re.fullmatch(r"[0-9a-f]{32}\n?", b):
            raise RuntimeError(f"GET /rand: {s} {b[:80]!r}")
        r.append(b)
    if r[0] == r[1]:
        raise RuntimeError(f"GET /rand: two requests gave {r[0]!r}")


# image -> how it starts (the image's own start unless `entrypoint`) and what a request must answer (m3b SERVE; go and node are m4a's CASES)
CASES = {
    "go": {"port": 8080, "requests": [], "expect": expect_go},
    "node": {"port": 8080, "requests": [("/", 200, "text/plain", "bench-node ok\n")]},
    "shell": {"port": 8080, "requests": [("/", 200, "text/", "bench-shell ok\n")]},
    "nginx": {"port": 8080, "requests": [("/", 200, "text/html", "bench-nginx ok\n"), ("/missing", 404, "text/html", None)]},
    "python": {"port": 8080, "requests": [("/", 200, "text/plain", "bench-python ok\n")]},
    "deno": {
        "port": 8080, "hook": True, "entrypoint": LAUNCHER["deno"], "env": ["GREETING=hi-deno", "DENO_NO_UPDATE_CHECK=1"] + LAUNCHER_ENV,
        "requests": [
            ("/foo", 200, "application/json", {"worker": "hello-deno", "path": "/foo", "greeting": "hi-deno"}),
            ("/bar/baz?x=1", 200, "application/json", {"worker": "hello-deno", "path": "/bar/baz", "greeting": "hi-deno"}),
        ],
    },
    "node-runtime": {
        "port": 8080, "hook": True, "entrypoint": LAUNCHER["node-runtime"], "env": ["GREETING=hi-node"] + LAUNCHER_ENV,
        "requests": [
            ("/foo", 200, "application/json", {"worker": "hello-node", "path": "/foo", "greeting": "hi-node"}),
            ("/bar/baz?x=1", 200, "application/json", {"worker": "hello-node", "path": "/bar/baz", "greeting": "hi-node"}),
        ],
    },
}


class Fail(Exception):
    pass


# ---- the probes

def check(port, case):
    """m2b's check: the case's requests, each on its own connection; RuntimeError on a mismatch."""
    for path, status, ctype, want in case["requests"]:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        got_type = r.getheader("content-type") or ""
        ok = r.status == status and got_type.startswith(ctype)
        if ok and want is not None:
            try:
                ok = (json.loads(body) if isinstance(want, dict) else body) == want
            except ValueError:
                ok = False
        if not ok:
            raise RuntimeError(f"GET {path}: {r.status} {got_type!r} {body[:200]!r}, want {status} {ctype!r} {want!r}")


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
    """m3b's wait_connect: until a connect to 127.0.0.1:port succeeds. The process ending first, or the budget passing with it alive, raises Fail."""
    while True:
        if p.poll() is not None:
            raise Fail(f"the process exited with status {p.returncode} before {what}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise Fail(f"nothing listening on 127.0.0.1:{port} for {what} after {budget:g} s, and armless-exec still running")
            time.sleep(0.1)


def exercise(p, case, budget):
    """Ready, then the checks, on the process `p`: {ready, served, ready_s, failure}."""
    res = {"ready": False, "served": False, "ready_s": None, "failure": None}
    t0 = time.monotonic()
    deadline = t0 + budget
    hook = case.get("hook")
    try:
        if hook:
            connect_wait(p, HOOK_PORT, deadline, budget, "the launcher's hook port")
            for path, want in (("/ready", "ready\n"), ("/run", "running\n")):
                status, body = post(HOOK_PORT, HOOK + path, max(1.0, deadline - time.monotonic()))
                if (status, body) != (200, want):
                    raise Fail(f"POST {path}: {status} {body[:200]!r}, want 200 {want!r}")
        else:
            connect_wait(p, case["port"], deadline, budget, "listening")
        res["ready"], res["ready_s"] = True, round(time.monotonic() - t0, 1)
        check(case["port"], case)
        if case.get("expect"):
            case["expect"](case["port"])
        if hook:
            t1 = time.monotonic()
            status, body = post(HOOK_PORT, HOOK + "/terminate", 60)
            if (status, body) != (200, "terminating\n"):
                raise Fail(f"POST /terminate: {status} {body[:200]!r}")
            if (time.monotonic() - t1) * 1000 > DRAIN_MS:
                raise Fail(f"POST /terminate took {(time.monotonic() - t1) * 1000:.0f} ms: the app did not exit on SIGTERM")
        res["served"] = True
    except (Fail, RuntimeError, OSError, http.client.HTTPException) as e:
        res["failure"] = str(e) or type(e).__name__
    return res


# ---- one run of the image

def start(a, plan, tag, traced, timeout):
    """armless-exec on the plan: (process, files, command). Output goes to <out>/au-<tag>.{stdout,stderr}."""
    out_p, err_p = (os.path.join(a.out, f"au-{tag}.{x}") for x in ("stdout", "stderr"))
    cmd = [os.path.abspath(a.armless_exec), "--fs", a.fs, "--cwd", plan["cwd"], "-m", str(au.RAM_MB), "--timeout", str(int(timeout))] + list(a.exec_arg)
    for kv in plan["env"]:
        cmd += ["--env", kv]
    cmd += plan["run_argv"]
    env = dict(os.environ)
    if traced:
        env["ARMLESS_EXEC_TRACE"] = "1"
    fo, fe = open(out_p, "wb"), open(err_p, "wb")
    p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe)
    if traced:
        def cap():  # a guest that never idles would write the trace until the disk is full
            while p.poll() is None:
                if os.path.getsize(err_p) > TRACE_CAP:
                    p.kill()
                    return
                time.sleep(1)
        threading.Thread(target=cap, daemon=True).start()
    return p, (fo, fe), cmd, out_p, err_p


def stop(p, files):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
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
    t = time.monotonic()
    p, files, cmd, out_p, err_p = start(a, plan, tag, traced, budget + CHECK_BUDGET_S)
    print(f"[{tag}] {' '.join(cmd)}", flush=True)
    try:
        res = exercise(p, case, budget)
    finally:
        # the process may have ended by itself with its report: give it a moment to flush before stopping it
        time.sleep(0.5)
        exited = p.poll() is not None
        stop(p, files)
    res.update({"tag": tag, "cmd": " ".join(cmd), "exited_on_its_own": exited, "exit": p.returncode, "wall_s": round(time.monotonic() - t, 1), "stdout": out_p, "stderr": err_p})
    rep = au.parse_report("\n".join(tail_lines(err_p, 400, 1 << 20)))
    res["report"] = rep
    print(f"[{tag}] ready={res['ready']} served={res['served']} exit={p.returncode} report-status={(rep or {}).get('status')} failure={res['failure']}", flush=True)
    return res


def squeeze(path):
    au.squeeze(path)


def cmd_cell(a):
    os.makedirs(a.out, exist_ok=True)
    name, case = a.image, CASES[a.image]
    result = {"image": name, "ready": False, "served": False, "first_failure": None, "kind": None, "harness_error": None}

    def finish(rc=0):
        with open(os.path.join(a.out, "result.json"), "w") as f:
            json.dump(result, f, indent=1)
        print(f"{name}: ready={result['ready']} served={result['served']} first failure: {result['first_failure']}", flush=True)
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

    run = run_once(a, plan, case, name, False, a.ready_timeout)
    result.update(ready=run["ready"], served=run["served"], ready_s=run["ready_s"], failure=run["failure"], exit=run["exit"], status=(run["report"] or {}).get("status"),
                  cmd=run["cmd"], wall_s=run["wall_s"], stderr_tail=tail_lines(run["stderr"]), stdout_tail=tail_lines(run["stdout"], 20))
    if run["served"]:
        result["first_failure"] = None
        result["kind"] = "served"
        squeeze(run["stderr"])
        return finish()

    # the failure: again with the trace, to say which call or instruction ended it
    tr = run_once(a, plan, case, name + "-trace", True, TRACE_BUDGET_S)
    rep = tr["report"]
    fault = au.fault_info(rep, a.fs, run_argv[0] if shebang else exe) if rep else None
    summary = au.parse_trace(tr["stderr"]) if os.path.getsize(tr["stderr"]) else None
    if summary is not None and rep is not None:
        kind, verdict = au.classify({"report": rep, "exit": tr["exit"], "stderr_tail": ""}, summary, fault)
    elif summary is not None:
        kind, verdict = "unclassified", f"armless-exec did not reach its report in the traced run (exit {tr['exit']}; {tr['failure']})"
    else:
        kind, verdict = "unclassified", "the traced run left no trace"
    result.update(kind=kind, verdict=verdict, traced_failure=tr["failure"], traced_status=(rep or {}).get("status"), fault=fault,
                  trace=summary, stderr_tail=tail_lines(tr["stderr"]), traced_stdout_tail=tail_lines(tr["stdout"], 20))
    result["first_failure"] = verdict if kind == "blocked" else f"{run['failure']}; {verdict}"
    for p_ in (run["stderr"], tr["stderr"]):
        squeeze(p_)
    return finish()


# ---- the table

def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


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
    out = ["## armless user mode: supa-wasm gate images", "", (a.source or "").strip(), "",
           "| image | ready | served | first failure |", "|---|---|---|---|"]
    for r in rows:
        ff = r["first_failure"] or "-"
        out.append(f"| {r['image']} | {yn(r['ready'])} | {yn(r['served'])} | {esc(ff)} |")
    out += ["", "ready: a connect to the guest's port (the hook runtimes: the launcher's /ready and /run) succeeds from the host. served: the image's serve checks pass.", ""]
    for r in details:
        out += [f"### {r['image']}", ""]
        if r.get("status"):
            out.append(f"untraced run: status `{r['status']}`, exit {r.get('exit')}, {r.get('wall_s')} s; harness: {esc(r.get('failure') or 'none')}")
        if r.get("traced_status") is not None:
            out.append(f"traced run: status `{r['traced_status']}`; harness: {esc(r.get('traced_failure') or 'none')}")
        t = r.get("trace") or {}
        if t:
            out.append(f"trace: {t.get('records')} calls, ENOSYS: {t.get('enosys') or 'none'}, first ENOSYS at: {t.get('first_enosys') or 'none'}")
        if r.get("fault"):
            out.append(f"fault: {r['fault']}")
        for title, key in (("last 40 lines of stderr", "stderr_tail"), ("last lines of the guest's output", "traced_stdout_tail" if r.get("traced_stdout_tail") else "stdout_tail")):
            lines = r.get(key) or []
            out += ["", f"<details><summary>{title}</summary>", "", "```", *[l[:300] for l in lines], "```", "", "</details>"]
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
    r = sub.add_parser("report")
    r.add_argument("--in", dest="inp", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--source", default="")
    a = ap.parse_args()
    sys.exit(cmd_cell(a) if a.cmd == "cell" else cmd_report(a))


if __name__ == "__main__":
    main()
