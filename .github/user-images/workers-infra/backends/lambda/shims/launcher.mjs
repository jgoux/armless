// Guest launcher for Lambda-backed MicroVM images (node/bun/deno bases —
// portable JS, all three run it).
//
// Injected as the image ENTRYPOINT wrapping the runtime's serving shim
// (everything after `--`), it answers the platform's lifecycle hooks. The
// hook port must match what the controller enables at image build
// (src/controller/backends/lambda.ts HOOK_PORT).
//
//   - /ready: 200 with the app deliberately unstarted — the image snapshot
//     captures a warm runtime and a cold app.
//   - /run: the once-per-instance moment before traffic. The payload names
//     where to fetch the config bundle and a credential to fetch it with;
//     the launcher materializes the bundle's env into the app's environment,
//     starts it, and answers 200 only once the app listens on PORT — or as
//     soon as it runs, when the bundle says the worker takes no traffic. A
//     failed fetch never starts the app and answers non-200, which keeps
//     traffic gated and lets the platform retry the hook (fail-closed). The
//     whole hook answers within its own deadline (WORKERS_RUN_DEADLINE_MS),
//     under the platform's 60-second hook timeout: a boot that misses it is
//     reported as such, in the log and to the control plane, rather than
//     cut off silently.
//   - /terminate: the platform's pre-release notice. The launcher passes it
//     on as SIGTERM (the shims stop accepting and drain in-flight work) and
//     answers 200 once the app exits — or after a bounded wait, so work that
//     never ends (an endless stream) cannot hold termination open.
//
// An empty /run body means the launch carried no payload (the image has
// hooks but delivery is unconfigured): there is nothing to deliver, so the
// app starts with its baked environment alone.
import { createServer } from "node:http";
import { Buffer } from "node:buffer";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, createWriteStream, existsSync, writeFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { connect } from "node:net";
import { constants as osConstants, tmpdir } from "node:os";
import { basename, join } from "node:path";
import process from "node:process";
import { pipeline } from "node:stream/promises";

const HOOK_PORT = Number(process.env.WORKERS_HOOK_PORT ?? 61009);
// The host-restoring proxy baked into the image (WORKERS_HOSTPROXY): set, it
// owns the serving port and the app moves to a loopback port behind it. The
// listen wait follows the app — the proxy binds instantly, so only the app's
// own socket says anything about readiness.
const HOSTPROXY = process.env.WORKERS_HOSTPROXY || undefined;
const SERVING_PORT = process.env.PORT ? Number(process.env.PORT) : 8080;
const APP_PORT = HOSTPROXY
  ? Number(process.env.WORKERS_APP_PORT ?? 8081)
  : process.env.PORT
  ? Number(process.env.PORT)
  : undefined;
const FETCH_TIMEOUT_MS = Number(process.env.WORKERS_BUNDLE_TIMEOUT_MS ?? 10_000);
// The code archive is a real download where the bundle is a small JSON body,
// so it gets its own, longer timeout — and a size cap, because the guest disk
// is the instance's own but filling it during boot helps nobody.
const CODE_FETCH_TIMEOUT_MS = Number(process.env.WORKERS_CODE_TIMEOUT_MS ?? 60_000);
const CODE_MAX_BYTES = Number(process.env.WORKERS_CODE_MAX_BYTES ?? 256 * 1024 * 1024);
const APP_DIR = process.env.APP_DIR ?? "/app";
const LISTEN_TIMEOUT_MS = Number(process.env.WORKERS_LISTEN_TIMEOUT_MS ?? 60_000);
// The run hook's whole budget from arrival, fetch included. Under the
// platform's 60-second hook timeout (its maximum), so a boot that misses it
// is reported as such — in the log and to the control plane — rather than
// cut off silently.
const RUN_DEADLINE_MS = Number(process.env.WORKERS_RUN_DEADLINE_MS ?? 50_000);
// Must stay under the terminate hook's platform-side timeout, or the answer
// arrives after the platform has stopped listening.
const DRAIN_TIMEOUT_MS = Number(process.env.WORKERS_DRAIN_TIMEOUT_MS ?? 20_000);
// The obituary is best-effort by design: a short cap so neither the
// terminate answer nor the dying gasp waits on an unreachable control plane.
const OBITUARY_TIMEOUT_MS = Number(process.env.WORKERS_OBITUARY_TIMEOUT_MS ?? 2_000);

/** The platform POSTs lifecycle hooks under this prefix. */
const HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1";

const sep = process.argv.indexOf("--");
const appCommand = sep >= 0 ? process.argv.slice(sep + 1) : [];
if (appCommand.length === 0) {
  console.error("compute launcher: no app command after --");
  process.exit(2);
}

let child = null;
let proxy = null;
// Latches the first /run attempt so a concurrent or repeated hook call joins
// it instead of double-starting the app; a failed attempt resets the latch so
// the next call retries the fetch.
let starting = null;
// While the terminate hook drains the app, its exit is the expected outcome —
// the fate-sharing exit handler must not fire before the hook has answered.
let terminating = false;
// The instance's own name, from the run hook's envelope; the bundle's
// obituary grant names where to report the instance's death.
let instanceId = null;
let obituary = null;
// Whether the run hook waits for the app to open its port. The bundle says
// no for a worker that takes no traffic (private exposure), which may be a
// loop with no server at all.
let listenRequired = true;

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

async function fetchBundle(payload, timeoutMs) {
  const res = await fetch(payload.url, {
    headers: { authorization: `Bearer ${payload.bootstrapToken}` },
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`bundle fetch failed: HTTP ${res.status}`);
  const bundle = await res.json();
  if (typeof bundle?.env !== "object" || bundle.env === null) {
    throw new Error("bundle response has no env object");
  }
  return bundle;
}

/**
 * Fetches the worker's code archive named by the bundle's code descriptor,
 * verifies its digest, and unpacks it into APP_DIR before the shim starts —
 * the shim then finds the entry module exactly as if it had been baked in.
 * Any failure rejects, which fails the /run hook: a worker must never start
 * on missing or tampered code (fail-closed, and the platform retries).
 */
async function fetchCode(code) {
  const res = await fetch(code.url, { signal: AbortSignal.timeout(CODE_FETCH_TIMEOUT_MS) });
  if (!res.ok) throw new Error(`code fetch failed: HTTP ${res.status}`);

  const dir = await mkdtemp(join(tmpdir(), "workers-code-"));
  const file = join(dir, "code.tar.gz");
  const hash = createHash("sha256");
  let bytes = 0;
  const out = createWriteStream(file);
  try {
    await pipeline(
      res.body,
      async function* (source) {
        for await (const chunk of source) {
          bytes += chunk.length;
          if (bytes > CODE_MAX_BYTES) {
            throw new Error(`code archive exceeds ${CODE_MAX_BYTES} bytes`);
          }
          hash.update(chunk);
          yield chunk;
        }
      },
      out,
    );
    const digest = hash.digest("hex");
    if (code.sha256 && digest !== code.sha256) {
      throw new Error(`code archive digest mismatch: got ${digest}`);
    }
    // busybox tar on the alpine bases; extraction stays inside APP_DIR
    // because the archive was validated server-side at deploy.
    await new Promise((resolve, reject) => {
      const tar = spawn("tar", ["-xzf", file, "-C", APP_DIR], {
        stdio: ["ignore", "ignore", "inherit"],
      });
      tar.on("exit", (codeNum) =>
        codeNum === 0 ? resolve() : reject(new Error(`tar exited ${codeNum}`)),
      );
      tar.on("error", reject);
    });
    console.error(`compute launcher: unpacked code archive (${bytes} bytes)`);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

/**
 * Reports this instance's death — best-effort and bounded, since whoever is
 * waiting on us (the terminate hook, or the process's own exit) must not be
 * held by an unreachable control plane. The report is only a hint: the
 * control plane verifies against the platform before acting on it.
 */
async function sendObituary(reason) {
  if (!obituary?.url || !instanceId) return;
  try {
    const res = await fetch(obituary.url, {
      method: "POST",
      headers: {
        authorization: `Bearer ${obituary.token}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(reason ? { instanceId, reason } : { instanceId }),
      signal: AbortSignal.timeout(OBITUARY_TIMEOUT_MS),
    });
    console.error(`compute launcher: obituary sent (${res.status})`);
  } catch (err) {
    // The refresh sweep is the backstop for a report that never lands.
    console.error(`compute launcher: obituary failed: ${err?.message ?? err}`);
  }
}

/**
 * The deno shim runs with a fixed `--config APP_DIR/deno.json` (config
 * discovery follows the main module — the shim — so the app's own config is
 * never found without it). This makes the flag's target real: the user's
 * deno.json as-is, their deno.jsonc copied verbatim (Deno's config parser is
 * JSONC-tolerant regardless of extension), or an empty default. Runs after
 * the code fetch, so a runtime-delivered config counts too.
 */
function ensureDenoConfig() {
  if (basename(appCommand[0]) !== "deno") return;
  const json = join(APP_DIR, "deno.json");
  try {
    if (existsSync(json)) return;
    const jsonc = join(APP_DIR, "deno.jsonc");
    if (existsSync(jsonc)) {
      copyFileSync(jsonc, json);
      console.error("compute launcher: materialized deno.json from deno.jsonc");
    } else {
      writeFileSync(json, "{}\n");
    }
  } catch (err) {
    // The shim will fail loudly on the missing config; this is just why.
    console.error("compute launcher: deno config:", err?.message ?? err);
  }
}

/**
 * The app's environment: the launcher's own, minus everything under `AWS_`
 * — the platform injects its microVM identity there (`AWS_LAMBDA*`,
 * `AWS_REGION`, whatever comes next), and all of it is infrastructure
 * detail no workload should see or depend on. The bundle's variables layer
 * on top, so a user secret that happens to carry the prefix is the user's
 * to keep.
 */
function appEnv(env) {
  const inherited = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (name.startsWith("AWS_")) continue;
    inherited[name] = value;
  }
  // In proxy mode PORT is rewritten to the loopback port the proxy targets;
  // an app following the PORT contract lands behind the proxy unmodified.
  if (HOSTPROXY) inherited.PORT = String(APP_PORT);
  return { ...inherited, ...env };
}

/**
 * Starts the host-restoring proxy on the serving port, aimed at the app's
 * loopback port. Its fate is the instance's: a dead proxy is a dead serving
 * port, so its exit ends the process just as the app's does.
 */
function startHostproxy() {
  if (!HOSTPROXY || proxy) return;
  proxy = spawn(HOSTPROXY, ["-listen", `:${SERVING_PORT}`, "-target", `127.0.0.1:${APP_PORT}`], {
    stdio: ["ignore", "inherit", "inherit"],
  });
  proxy.on("exit", () => {
    if (terminating || child?.exitCode !== null) return;
    console.error("compute launcher: hostproxy exited — serving port is dead");
    sendObituary("hostproxy exited").finally(() => process.exit(1));
  });
  proxy.on("error", (err) => {
    console.error("compute launcher: hostproxy failed to start:", err);
    process.exit(1);
  });
}

function startApp(env) {
  ensureDenoConfig();
  startHostproxy();
  child = spawn(appCommand[0], appCommand.slice(1), {
    env: appEnv(env),
    stdio: ["ignore", "inherit", "inherit"],
  });
  // The launcher's fate is the app's: when the app dies the hook port closes
  // with it, so the platform sees the instance as gone rather than a husk
  // that still answers hooks — after one dying gasp toward the control
  // plane, so the route dies in seconds instead of at the next sweep.
  child.on("exit", (code, signal) => {
    if (terminating) return;
    // The one line that says why the app is gone: an exit code, or the
    // signal that took it — 9 is what the guest kernel's OOM killer sends.
    const signum = signal ? (osConstants.signals[signal] ?? 1) : undefined;
    const reason = code !== null
      ? `app exited with code ${code}`
      : signum === 9
        ? "app killed by signal 9 (SIGKILL: out of memory is the usual cause)"
        : `app killed by signal ${signum}`;
    console.error(`compute launcher: ${reason}`);
    sendObituary(reason).finally(() => process.exit(code ?? 128 + signum));
  });
  child.on("error", (err) => {
    console.error("compute launcher: app failed to start:", err);
    process.exit(1);
  });
}

function tryConnect(port) {
  return new Promise((resolve, reject) => {
    const sock = connect({ port, host: "127.0.0.1" }, () => {
      sock.destroy();
      resolve();
    });
    sock.on("error", (err) => {
      sock.destroy();
      reject(err);
    });
  });
}

// Waits for the app to open its port, until the earlier of the listen
// timeout and the hook's own deadline.
async function waitForListen(hookDeadline) {
  if (APP_PORT === undefined) return;
  const started = Date.now();
  const deadline = Math.min(hookDeadline, started + LISTEN_TIMEOUT_MS);
  while (Date.now() < deadline) {
    if (child?.exitCode !== null) throw new Error("app exited before listening");
    try {
      await tryConnect(APP_PORT);
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  throw new Error(`app did not listen on :${APP_PORT} within ${Date.now() - started}ms`);
}

/**
 * The run hook's body is an envelope (`microVmId`, ...) carrying the
 * `RunMicrovm` payload as a JSON string under one of these fields; a bare
 * payload body is accepted too, for robustness across wire generations.
 */
function payloadOf(body) {
  if (!body) return undefined;
  const parsed = JSON.parse(body);
  // `microvmId` is the live wire's spelling (measured); the camel-case
  // variant is kept for the same cross-generation robustness as the
  // payload fields below.
  const envelopeId = parsed.microvmId ?? parsed.microVmId;
  if (typeof envelopeId === "string" && envelopeId) {
    instanceId = envelopeId;
  } else {
    // Without the platform's name for this instance no death can be
    // reported; say so at boot instead of failing silently at death.
    console.error("compute launcher: run body carries no instance id — obituary disabled");
  }
  const raw = parsed.runPayload ?? parsed.runHookPayload ?? parsed.payload;
  if (typeof raw === "string") return JSON.parse(raw);
  if (raw && typeof raw === "object") return raw;
  return parsed.url ? parsed : undefined;
}

async function handleRun(req) {
  // One deadline from arrival, so the answer — success or the reason it is
  // not — lands before the platform stops listening.
  const deadline = Date.now() + RUN_DEADLINE_MS;
  if (!child && !starting) {
    const attempt = (async () => {
      const payload = payloadOf((await readBody(req)).trim());
      let env = {};
      if (payload?.url) {
        const bundle = await fetchBundle(
          payload,
          Math.max(1, Math.min(FETCH_TIMEOUT_MS, deadline - Date.now())),
        );
        env = bundle.env;
        listenRequired = bundle.listen !== false;
        console.error(
          `compute launcher: applied config bundle ${bundle.version} (${
            Object.keys(env).length
          } vars)`,
        );
        if (bundle.obituary?.url) {
          obituary = bundle.obituary;
        }
        if (bundle.code?.url) {
          await fetchCode(bundle.code);
        }
      }
      startApp(env);
    })();
    starting = attempt.catch((err) => {
      starting = null;
      throw err;
    });
  }
  await starting;
  if (!listenRequired) {
    if (child?.exitCode !== null) throw new Error("app exited during the run hook");
    return;
  }
  await waitForListen(deadline);
}

async function handleTerminate() {
  if (!child || child.exitCode !== null) {
    console.error("compute launcher: terminate hook — no app to drain");
    await sendObituary();
    return;
  }
  terminating = true;
  console.error("compute launcher: terminate hook — draining app");
  child.kill("SIGTERM");
  // The proxy drains its own in-flight requests on SIGTERM; signalling it
  // with the app keeps both inside the platform's pre-release window.
  proxy?.kill("SIGTERM");
  const drained = await Promise.race([
    new Promise((resolve) => child.once("exit", () => resolve(true))),
    new Promise((resolve) => setTimeout(() => resolve(false), DRAIN_TIMEOUT_MS).unref()),
  ]);
  console.error(
    drained
      ? "compute launcher: app drained"
      : `compute launcher: app still running after ${DRAIN_TIMEOUT_MS}ms — answering anyway`,
  );
  await sendObituary();
}

const server = createServer(async (req, res) => {
  const path = req.url?.split("?")[0];
  try {
    if (path === `${HOOK_PREFIX}/ready`) {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("ready\n");
      return;
    }
    if (path === `${HOOK_PREFIX}/run`) {
      await handleRun(req);
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("running\n");
      return;
    }
    if (path === `${HOOK_PREFIX}/terminate`) {
      await handleTerminate();
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("terminating\n");
      return;
    }
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found\n");
  } catch (err) {
    console.error("compute launcher:", err);
    // The platform will cut the instance off for the failed hook; the
    // report carries the reason it cannot say itself.
    if (path === `${HOOK_PREFIX}/run`) await sendObituary(err?.message ?? String(err));
    if (!res.headersSent) res.writeHead(500, { "content-type": "text/plain" });
    res.end(`${err?.message ?? err}\n`);
  }
});

server.listen(HOOK_PORT, "0.0.0.0", () => {
  console.error(`compute launcher: hooks on :${HOOK_PORT}, app: ${appCommand.join(" ")}`);
});

for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => {
    proxy?.kill(sig);
    if (child) child.kill(sig);
    else process.exit(0);
  });
}
