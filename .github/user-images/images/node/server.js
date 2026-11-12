// The app of the node benchmark image: built-in modules only, GET / answers a known body.
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const zlib = require("node:zlib");
const url = require("node:url");
const util = require("node:util");

const body = "bench-node ok\n";
const packed = zlib.gzipSync(Buffer.alloc(64 * 1024, "supa"));
const checksum = crypto.createHash("sha256").update(zlib.gunzipSync(packed)).digest("hex");
const where = path.join(os.tmpdir(), "bench-node");
fs.writeFileSync(where, checksum);
fs.unlinkSync(where);

const server = http.createServer((req, res) => {
  const { pathname } = new url.URL(req.url, "http://localhost");
  if (req.method === "GET" && pathname === "/") {
    res.writeHead(200, { "content-type": "text/plain", "content-length": Buffer.byteLength(body) });
    res.end(body);
  } else {
    res.writeHead(404).end();
  }
});
server.listen(Number(process.env.PORT || 8080), "0.0.0.0", () => {
  console.log(util.format("node app: %s, listening", checksum.slice(0, 12)));
});
