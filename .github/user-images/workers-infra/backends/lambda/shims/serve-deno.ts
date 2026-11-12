// Native Deno serving shim for the deno catalog runtime.
//
// Deno already speaks the Web-standard `fetch` contract natively via
// `Deno.serve`, so — unlike the Node shim — there's no HTTP translation to do:
// this just resolves the baked user module in APP_DIR, and hands its
// `export default { fetch }` straight to `Deno.serve` on PORT (native
// streaming, HTTP/2, etc.). No baked code → the port is still bound and
// answers 503.
const PORT = Number(Deno.env.get("PORT") ?? 8080);
const APP_DIR = Deno.env.get("APP_DIR") ?? "/app";
const ENTRY_CANDIDATES = ["main.ts", "main.js", "main.mjs", "index.ts", "index.js", "index.mjs"];

type Handler = (request: Request) => Response | Promise<Response>;

async function loadFetch(): Promise<Handler | null> {
  for (const file of ENTRY_CANDIDATES) {
    const path = `${APP_DIR}/${file}`;
    try {
      await Deno.stat(path);
    } catch {
      continue;
    }
    const mod = await import(`file://${path}`);
    const handler = mod?.default?.fetch ??
      (typeof mod?.default === "function" ? mod.default : undefined);
    if (typeof handler === "function") {
      console.error(`compute shim: serving ${file}`);
      return handler.bind(mod.default);
    }
    console.error(`compute shim: ${file} has no default { fetch } export — skipping`);
  }
  return null;
}

const handler = await loadFetch();
console.error(`compute shim: listening on :${PORT} (${handler ? "serving" : "idle, no code"})`);
const server = Deno.serve(
  { port: PORT, hostname: "0.0.0.0" },
  handler ?? (() => new Response("compute: no application code loaded\n", { status: 503 })),
);
// Graceful drain: stop accepting new connections and let in-flight requests
// finish instead of dropping them on SIGTERM.
Deno.addSignalListener("SIGTERM", () => {
  server.shutdown();
});
