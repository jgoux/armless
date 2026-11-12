// Fetch shim for the node catalog runtime.
//
// Node has no built-in fetch-handler server (unlike Deno's `Deno.serve`,
// which the deno base uses natively — serve-deno.ts), so this hand-written
// node:http harness is the one that's
// actually needed: it speaks the Web-standard `fetch` contract by translating
// node:http requests/responses to/from Web Request/Response. Baked into the
// node base as its entrypoint (see backends/lambda/images/node/Dockerfile,
// internal/backend/microvm/catalog.go).
//
// Three roles:
//   - start script: a package.json `scripts.start` says the app is a server
//     in its own right. The shim hands it the port and disappears — see
//     runStartScript.
//   - fetch handler: absent a start script, a built image bakes user code
//     into APP_DIR; the shim imports its
//     `export default { fetch(request): Response }` handler and serves it on
//     PORT.
//   - bare runtime: no baked code — the shim still binds PORT and answers
//     503, so the port is up even with nothing to serve.
//
// Before any of them it installs the app's dependencies, through corepack and
// under the manager that wrote the project's lockfile — at a version the image
// already carries (backends/lambda/images/node/Dockerfile), so the install
// never begins by downloading the thing that installs, and in that manager's
// lockfile-exact mode, so the versions that run are the versions the project
// resolved.
import { createServer } from "node:http";
import { Buffer } from "node:buffer";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import process from "node:process";

const PORT = Number(process.env.PORT ?? 8080);
const APP_DIR = process.env.APP_DIR ?? "/app";
// Bounds the install so a registry that hangs fails the boot with its own
// reason, rather than as the launcher's blank "app did not listen" — under
// that 60s wait, with room left for the import and the listen themselves.
const INSTALL_TIMEOUT_MS = Number(process.env.WORKERS_INSTALL_TIMEOUT_MS ?? 45_000);
// The managers corepack ships shims for.
const PACKAGE_MANAGERS = ["npm", "pnpm", "yarn"];
// What the base image baked, name -> versions, newest major first. The image
// decides these, so a version lives in one place and the shim runs what is
// actually on disk. Empty outside that image (a test, a bare `node
// serve.mjs`), which falls back to letting corepack resolve the name itself.
const BAKED = parseBaked(process.env.WORKERS_PACKAGE_MANAGERS ?? "");

// A lockfile names the manager that wrote it, which is the only manager that
// can install it without rewriting it — so the lockfile on disk, not the
// advisory `packageManager` field, is what picks the manager here. `yarn.lock`
// is shared by two managers that are not the same tool, so it is read rather
// than just found (see yarnFlavour).
//
// `install` is each manager's permissive mode, which re-resolves a range when
// the lockfile does not satisfy it; the mode named here is the one that
// installs the locked versions and fails instead of changing them. That is the
// whole point of running the install in the guest: the tree that boots is the
// tree the project resolved.
const LOCKFILES = [
  { file: "pnpm-lock.yaml", name: "pnpm", args: ["install", "--frozen-lockfile"] },
  { file: "package-lock.json", name: "npm", args: ["ci"] },
  { file: "npm-shrinkwrap.json", name: "npm", args: ["ci"] },
  // No args: Classic and Berry spell the same mode differently, and only the
  // file's own format says which of them wrote it.
  { file: "yarn.lock", name: "yarn", args: null },
];
// Bun's lockfile is not corepack's to install, and installing it with
// something else is not installing it at all.
const BUN_LOCKFILES = ["bun.lock", "bun.lockb"];

function parseBaked(list) {
  const baked = new Map();
  for (const spec of list.split(",").map((entry) => entry.trim()).filter(Boolean)) {
    // rsplit: a version never contains @, a scoped name could.
    const at = spec.lastIndexOf("@");
    if (at < 1) continue;
    const name = spec.slice(0, at);
    baked.set(name, [...(baked.get(name) ?? []), spec.slice(at + 1)]);
  }
  for (const versions of baked.values()) {
    versions.sort((a, b) => Number(major(b)) - Number(major(a)));
  }
  return baked;
}

function major(version) {
  return version.split(".")[0];
}

/** The app's manifest, or null when it ships without one. */
async function readManifest() {
  let text;
  try {
    text = await readFile(`${APP_DIR}/package.json`, "utf8");
  } catch {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch (err) {
    throw new Error(`package.json is not valid JSON: ${err.message}`);
  }
}

/** The manager name a `packageManager` field names, or "" for anything else. */
function pinnedName(manifest) {
  const field = manifest?.packageManager;
  const pin = typeof field === "string" ? field.trim() : "";
  const name = pin.split("@")[0];
  return PACKAGE_MANAGERS.includes(name) ? name : "";
}

/**
 * Which yarn wrote this lockfile. Yarn 1 and Yarn 2+ share a name and a
 * filename and are not the same package manager: Classic's lockfile is its own
 * format under a `# yarn lockfile v1` banner, Berry's is YAML carrying
 * `__metadata`. Berry reads a v1 lockfile only by rewriting it, which
 * `--immutable` then refuses, so guessing here is the difference between an
 * install and a failed boot.
 */
async function yarnFlavour(path) {
  // The banner and the metadata block are both at the top; a lockfile can be
  // megabytes, and none of it past the first lines says anything more.
  const head = (await readFile(path, "utf8")).slice(0, 4096);
  if (/^#\s*yarn\s+lockfile\s+v1/im.test(head)) {
    return { berry: false, args: ["install", "--frozen-lockfile"] };
  }
  if (/^__metadata:/im.test(head)) {
    return { berry: true, args: ["install", "--immutable"] };
  }
  throw new Error(
    "yarn.lock is neither a Yarn Classic nor a Yarn Berry lockfile — remove it, " +
      "or deploy a Dockerfile to run the install yourself",
  );
}

/**
 * The build of a manager to run: always one the image already carries, never
 * the one `packageManager` asks for. Fetching a pinned build puts a registry
 * round trip for the installer inside the same liveness budget as the install,
 * so the field's version is ignored outright and the image's is what runs.
 *
 * `want` narrows that to a major where the major is the tool: Classic and
 * Berry are selected by the lockfile, and an image missing the one a lockfile
 * needs cannot install it at all.
 *
 * Null outside the base image (a test, a bare `node serve.mjs`), which leaves
 * the version to corepack.
 */
function bakedBuild(name, want) {
  const baked = BAKED.get(name);
  if (!baked) {
    if (BAKED.size === 0) return null;
    throw new Error(
      `this runtime does not carry ${name} (it carries ${[...BAKED.keys()].join(", ")})`,
    );
  }
  const version = want ? baked.find(want.match) : baked[0];
  if (!version) {
    throw new Error(
      `this runtime does not carry ${want.describe} (it carries ${name}@${baked.join(`, ${name}@`)})`,
    );
  }
  return version;
}

/**
 * The install to run: which manager, at which of the image's builds, in which
 * mode. Driven by the lockfile, because that is the only file that says who
 * resolved these versions.
 *
 * With no lockfile there is nothing to be faithful to — the install resolves
 * ranges against the registry as it finds them that day — so the mode is the
 * permissive one and the guest log says as much. The manager is then the
 * field's, since nothing else expresses a preference, and npm when it
 * expresses none either.
 */
async function resolveInstall(manifest) {
  for (const bun of BUN_LOCKFILES) {
    if (existsSync(`${APP_DIR}/${bun}`)) {
      throw new Error(
        `${bun} is a Bun lockfile, and this runtime installs with corepack ` +
          `(${PACKAGE_MANAGERS.join(", ")}) — deploy a Dockerfile to install with Bun`,
      );
    }
  }

  const found = LOCKFILES.filter((lock) => existsSync(`${APP_DIR}/${lock.file}`));
  const managers = [...new Set(found.map((lock) => lock.name))];
  if (managers.length > 1) {
    // Two lockfiles are two answers to the same question, and installing from
    // the wrong one is exactly the silent version drift this mode exists to
    // prevent. The field is allowed to break the tie, because a project that
    // names its manager has said which lockfile it maintains.
    const pinned = pinnedName(manifest);
    if (!managers.includes(pinned)) {
      throw new Error(
        `the app carries lockfiles for ${managers.join(" and ")} (${
          found.map((lock) => lock.file).join(", ")
        }) — remove the stale one, or name the one you maintain in package.json "packageManager"`,
      );
    }
    found.length = 0;
    found.push(...LOCKFILES.filter((lock) => lock.name === pinned && existsSync(`${APP_DIR}/${lock.file}`)));
  }

  const lock = found[0];
  if (!lock) {
    const name = pinnedName(manifest) || "npm";
    return { name, version: bakedBuild(name, null), args: ["install"], locked: false };
  }
  if (lock.name === "yarn") {
    const flavour = await yarnFlavour(`${APP_DIR}/${lock.file}`);
    const want = flavour.berry
      ? { match: (v) => Number(major(v)) >= 2, describe: "a Yarn Berry build (yarn 2 or newer)" }
      : { match: (v) => major(v) === "1", describe: "a Yarn Classic build (yarn 1)" };
    return { name: "yarn", version: bakedBuild("yarn", want), args: flavour.args, locked: lock.file };
  }
  return { name: lock.name, version: bakedBuild(lock.name, null), args: lock.args, locked: lock.file };
}

function declaresDependencies(manifest) {
  return ["dependencies", "devDependencies", "optionalDependencies"].some(
    (field) => Object.keys(manifest[field] ?? {}).length > 0,
  );
}

/**
 * How to name the manager build in the app's log: `pnpm@12.4.2` for a build
 * the image carries, a bare `pnpm` when no version was chosen. Outside the
 * base image the shim names none and corepack picks one for itself, and
 * there is nothing to report then — no version ran that this knows of.
 */
function describeBuild(name, version) {
  return version ? `${name}@${version}` : name;
}

/** Run the install to completion — bounded, with its output in the guest log. */
function runInstall(name, version, args) {
  return new Promise((resolve, reject) => {
    // Naming the version is what keeps corepack from resolving the project's
    // `packageManager` over the network; a bare name is the unbaked fallback,
    // where it does.
    const spec = version ? `${name}@${version}` : name;
    const child = spawn("corepack", [spec, ...args], {
      cwd: APP_DIR,
      // No stdin and no download prompt: both would wait on an answer no
      // guest can give. The rest of the environment is the app's own, so
      // registry settings delivered as env reach the install too.
      env: {
        ...process.env,
        COREPACK_ENABLE_DOWNLOAD_PROMPT: "0",
        // Belt to the spec's braces: with a baked version named, corepack has
        // nothing left to fetch, and saying so turns any path that would have
        // fetched into a failure here rather than a download the boot pays for.
        ...(version ? { COREPACK_ENABLE_NETWORK: "0" } : {}),
        // Load-bearing, not a workaround: this is what ignoring the field's
        // version is made of. pnpm checks `packageManager` again on its own
        // account, after corepack has already chosen the binary, and refuses to
        // run a version the field contradicts (ERR_PNPM_BAD_PM_VERSION) rather
        // than warning — so a project pinning pnpm@9 would fail on the image's
        // pnpm@12 without this. COREPACK_ENABLE_STRICT does not do it: it
        // relaxes the manager name check and never the version.
        npm_config_manage_package_manager_versions: "false",
        npm_config_package_manager_strict: "false",
        // Yarn Berry links dependencies through Plug'n'Play by default and
        // writes no node_modules at all, which the plain `node` that imports
        // the app cannot resolve. Yarn 1 has no linkers and ignores this.
        YARN_NODE_LINKER: "node-modules",
      },
      stdio: ["ignore", "inherit", "inherit"],
    });
    const invocation = [spec, ...args].join(" ");
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`${invocation} exceeded ${INSTALL_TIMEOUT_MS}ms`));
    }, INSTALL_TIMEOUT_MS);
    child.on("exit", (code, signal) => {
      clearTimeout(timer);
      if (code === 0) resolve();
      // A lockfile-exact install also fails here when the lockfile and
      // package.json disagree, which is the manager reporting drift it was
      // asked not to paper over.
      else reject(new Error(`${invocation} exited ${signal ?? code}`));
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

/**
 * Install the app's dependencies before its handler is imported — nothing
 * else does. A catalog build only copies code onto the base image, and on the
 * code-store path the archive lands in APP_DIR at boot, so an app that
 * declares dependencies has none on disk until this runs. Skipped when there
 * is nothing to install and when node_modules is already there, which leaves
 * a self-contained app's boot free of the network entirely.
 */
async function installDependencies(manifest) {
  if (!manifest || !declaresDependencies(manifest)) return;
  if (existsSync(`${APP_DIR}/node_modules`)) {
    console.error("compute shim: node_modules already present — skipping install");
    return;
  }
  const { name, version, args, locked } = await resolveInstall(manifest);
  if (!locked) {
    console.error(
      "compute shim: no lockfile in the app — installing the ranges package.json " +
        "declares, which resolve against the registry as it is today; commit a " +
        "lockfile to have the versions you tested install here",
    );
  }
  console.error(
    `compute shim: installing dependencies with ${describeBuild(name, version)}` +
      (locked ? ` from ${locked}` : ""),
  );
  const start = Date.now();
  await runInstall(name, version, args);
  // For the app's own developer, reading this in their worker's logs: the
  // three facts that explain a boot slower than they expected, or a tree that
  // is not the one they tested — which manager build installed it, whether
  // the versions are the ones they pinned, and what it cost the boot. A
  // warning because both halves are theirs to remove: shipping node_modules
  // skips the install, committing a lockfile pins it. Nothing downstream
  // reads a level off these lines, so the word is in the line itself.
  console.warn(
    `compute shim: warning: installed dependencies with ${describeBuild(name, version)} ` +
      (locked
        ? `deterministically from ${locked}`
        : "non-deterministically (no lockfile)") +
      ` in ${Date.now() - start}ms`,
  );
}

/** The app's own start command, or null when it declares none. */
function startScriptOf(manifest) {
  const start = manifest?.scripts?.start;
  return typeof start === "string" && start.trim() !== "" ? start.trim() : null;
}

/**
 * Hand the serving port to the app's own server and leave. A start script is
 * the app saying it listens for itself, so there is nothing here to serve on
 * its behalf — and importing it to find out would be too late, since the
 * import binds the port the shim is about to ask for.
 *
 * execve rather than spawn: the app takes over this pid, so it inherits the
 * launcher's supervision as-is — the terminate hook's SIGTERM reaches the
 * server itself, its exit is the instance's exit, and no second supervisor
 * stands between them. Never returns.
 */
function runStartScript(script) {
  // What a package manager's `run` would put there, without a package manager
  // in the middle of the signal path: a start script's own binaries resolve.
  const path = [`${APP_DIR}/node_modules/.bin`, process.env.PATH ?? ""].join(":");
  console.error(`compute shim: handing the port to the start script: ${script}`);
  process.chdir(APP_DIR);
  // The shell is what makes `scripts.start` mean what it means everywhere
  // else, and `exec` is what keeps it from also becoming a process: without
  // it dash forks the command and stays as its parent, so the app answers
  // from a pid the launcher is not supervising. A script `exec` cannot carry
  // runs under the shell, and drains only as well as that shell forwards
  // signals.
  const argument = execable(script) ? `exec ${script}` : script;
  process.execve("/bin/sh", ["sh", "-c", argument], { ...process.env, PATH: path });
}

/**
 * Whether `exec` in front of this script still means what the script means.
 * It does for one simple command: `exec` replaces the shell with it, taking
 * redirections along. It does not once a control operator makes the script
 * more than one command (`exec` would discard everything after the first),
 * nor when a leading assignment would be read as the command to exec.
 * Conservative by construction — a script this cannot prove simple keeps the
 * shell, which costs a pid and never correctness.
 */
function execable(script) {
  return !/[;&|()\n]/.test(script) && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(script);
}

// Entry candidates, in priority order. Node resolves the .js/.mjs ones; Deno
// and Bun additionally resolve the .ts ones (they import TypeScript natively).
const ENTRY_CANDIDATES = [
  "index.js",
  "index.mjs",
  "index.ts",
  "main.js",
  "main.mjs",
  "main.ts",
  "server.js",
];

/** Locate and load the user module's fetch handler, or null if none is baked in. */
async function loadFetch() {
  for (const file of ENTRY_CANDIDATES) {
    const path = `${APP_DIR}/${file}`;
    try {
      await stat(path);
    } catch {
      continue;
    }
    const mod = await import(path);
    const handler = mod?.default?.fetch ??
      (typeof mod?.default === "function" ? mod.default : undefined) ??
      (typeof mod?.fetch === "function" ? mod.fetch : undefined);
    if (typeof handler === "function") {
      console.error(`compute shim: serving ${file}`);
      return handler.bind(mod?.default ?? mod);
    }
    console.error(`compute shim: ${file} has no default { fetch } export — skipping`);
  }
  return null;
}

/** Buffer a node request body into a single Uint8Array (non-streaming, MVP). */
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

/** Translate a node IncomingMessage into a Web Request. */
async function toWebRequest(req) {
  const url = `http://${req.headers.host ?? `localhost:${PORT}`}${req.url}`;
  const method = req.method ?? "GET";
  const hasBody = method !== "GET" && method !== "HEAD";
  const body = hasBody ? await readBody(req) : undefined;
  return new Request(url, {
    method,
    headers: req.headers,
    body: body && body.length ? body : undefined,
  });
}

/**
 * Write a Web Response back onto a node ServerResponse, streaming the body as
 * it arrives. Buffering it whole would break SSE and any long-lived response:
 * the client would see nothing until the handler's stream closed, which for a
 * live feed is never (the platform's proxy times the request out first).
 */
async function writeWebResponse(res, response) {
  const headers = {};
  for (const [k, v] of response.headers) headers[k] = v;
  res.writeHead(response.status, headers);
  if (!response.body) {
    res.end();
    return;
  }
  const reader = response.body.getReader();
  // A client that goes away mid-stream must stop the producer, or the handler
  // keeps generating into a dead socket.
  let aborted = false;
  res.on("close", () => {
    aborted = true;
    reader.cancel().catch(() => {});
  });
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done || aborted) break;
      if (!res.write(Buffer.from(value))) {
        // Respect backpressure rather than growing an unbounded buffer.
        await new Promise((resolve) => res.once("drain", resolve));
      }
    }
  } finally {
    if (!aborted) res.end();
  }
}

// Fail closed: an app whose dependencies are missing answers every request
// with the same import error, so die here instead — the launcher fails the
// run hook and the platform retries on a fresh instance.
let manifest = null;
try {
  manifest = await readManifest();
  await installDependencies(manifest);
} catch (err) {
  console.error(`compute shim: boot failed: ${err?.message ?? err}`);
  process.exit(1);
}

// An explicit start script wins over the entry search: the search is a
// convention, and it is the one that cannot be tried first without side
// effects.
const startScript = startScriptOf(manifest);
if (startScript) runStartScript(startScript);

const handler = await loadFetch();

const server = createServer(async (req, res) => {
  if (!handler) {
    res.writeHead(503, { "content-type": "text/plain" });
    res.end("compute: no application code loaded\n");
    return;
  }
  try {
    const response = await handler(await toWebRequest(req));
    await writeWebResponse(res, response ?? new Response(null, { status: 204 }));
  } catch (err) {
    console.error("compute shim: handler error:", err);
    if (!res.headersSent) res.writeHead(500, { "content-type": "text/plain" });
    res.end("internal error\n");
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.error(`compute shim: listening on :${PORT} (${handler ? "serving" : "idle, no code"})`);
});

// Graceful drain: stop accepting new connections and let in-flight requests
// finish rather than dropping them on SIGTERM.
process.on("SIGTERM", () => server.close());
