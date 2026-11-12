#!/usr/bin/env python3
"""Deploy-time converter of gates/pk0: an OCI image to a package, no container runtime involved.

  convert.py IMAGE OUT_DIR [--inline] [--build-time0] [--mkfs mkfs.erofs]

IMAGE is an OCI image layout (a directory with index.json and blobs/) or a `docker save` / OCI layout tar. OUT_DIR gets
  root.erofs   the flattened root file system: the layers applied in order (whiteouts and opaque directories honoured,
               hard links kept as one inode) and `mkfs.erofs --tar=f -b4096 -E noinline_data`
  config.json  what the container runtime reads from the image config: Env, Entrypoint, Cmd, WorkingDir, User, ExposedPorts
  mkfs.version the mkfs.erofs that wrote root.erofs

--inline leaves out `-E noinline_data`: the tail of a file that does not fill a block is then stored right after its inode,
not on a block boundary (EROFS's flat inline layout), which is what the reader's alignment check and copy are for.
--build-time0 passes `-T0` (the files get mtime 0 in the flattened tar): inodes that fit are written in the compact
32-byte format instead of the extended 64-byte one.

Left out of the conversion: extended attributes (a layer's security.capability), and the timestamps of the layers (the
flattened tar carries mtime 0). Device nodes, fifos and sockets are kept in the tar; whether the image holds them is up to
mkfs.erofs.
"""
import argparse, gzip, io, json, os, shutil, subprocess, sys, tarfile, tempfile


class File:
    """One inode of the flattened tree; hard links are several paths holding the same object."""

    def __init__(self, ti, data=None):
        self.kind = "dir" if ti.isdir() else "sym" if ti.issym() else "chr" if ti.ischr() else "blk" if ti.isblk() else "fifo" if ti.isfifo() else "reg"
        self.mode, self.uid, self.gid = ti.mode, ti.uid, ti.gid
        self.link, self.major, self.minor = ti.linkname if ti.issym() else "", ti.devmajor, ti.devminor
        self.data, self.size = data, ti.size if self.kind == "reg" else 0


class Source:
    """Reads named blobs out of a layout directory or a tar of one."""

    def __init__(self, path):
        self.tar = None if os.path.isdir(path) else tarfile.open(path, "r:")
        self.root = path
        # member names as archived (`tar -C layout .` makes `./index.json`) by their normalized spelling
        self.names = None if self.tar is None else {os.path.normpath(n): n for n in self.tar.getnames()}

    def read(self, name):
        with self.open(name) as f:
            return f.read()

    def has(self, name):
        if self.tar is None:
            return os.path.exists(os.path.join(self.root, name))
        return os.path.normpath(name) in self.names

    def open(self, name):
        if self.tar is None:
            return open(os.path.join(self.root, name), "rb")
        return self.tar.extractfile(self.names[os.path.normpath(name)])


def blob(src, digest):
    return "blobs/" + digest.replace(":", "/")


def locate(src, platform):
    """(config bytes, [layer file names]) of the image for `platform` (os/arch)."""
    if src.has("manifest.json"):  # docker save
        m = json.loads(src.read("manifest.json"))[0]
        return src.read(m["Config"]), m["Layers"]
    index = json.loads(src.read("index.json"))
    want = platform.split("/")
    while True:
        picked = None
        for d in index["manifests"]:
            p = d.get("platform") or {}
            if p.get("os") in (None, want[0]) and p.get("architecture") in (None, want[1]) and "attestation" not in str(d.get("annotations", {})):
                picked = d
                break
        if picked is None:
            sys.exit(f"convert: no {platform} image in the index")
        doc = json.loads(src.read(blob(src, picked["digest"])))
        if "manifests" in doc:
            index = doc
            continue
        return src.read(blob(src, doc["config"]["digest"])), [blob(src, l["digest"]) for l in doc["layers"]]


def norm(name):
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        sys.exit(f"convert: {name}: a path with ..")
    return "/".join(parts)


def subtree(state, path):
    """The paths at and under `path` ("" is the root: every path)."""
    if not path:
        return list(state)
    return [p for p in state if p == path or p.startswith(path + "/")]


def spool(src, name, tmp):
    """The layer as an uncompressed tar file in `tmp`."""
    out = os.path.join(tmp, "layer.tar")
    with src.open(name) as f:
        head = f.read(4)
        f.seek(0)
        if head[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=f) as z, open(out, "wb") as o:
                shutil.copyfileobj(z, o)
        elif head == b"\x28\xb5\x2f\xfd":
            with open(out, "wb") as o:
                p = subprocess.Popen(["zstd", "-dc"], stdin=subprocess.PIPE, stdout=o)
                shutil.copyfileobj(f, p.stdin)
                p.stdin.close()
                if p.wait():
                    sys.exit("convert: zstd failed")
        else:
            with open(out, "wb") as o:
                shutil.copyfileobj(f, o)
    return out


def apply_layer(state, tarpath, data_dir, counter):
    with tarfile.open(tarpath, "r:") as tf:
        members = tf.getmembers()
        # whiteouts apply to the layers below, never to this layer's own entries: all of them first
        for m in members:
            d, _, base = norm(m.name).rpartition("/")
            if base == ".wh..wh..opq":
                for p in subtree(state, d):
                    if p != d:
                        del state[p]
            elif base.startswith(".wh.") and not base.startswith(".wh..wh."):
                for p in subtree(state, (d + "/" if d else "") + base[4:]):
                    del state[p]
        for m in members:
            path = norm(m.name)
            base = path.rpartition("/")[2]
            if not path or base.startswith(".wh."):
                continue
            # the ancestors are directories (made up when the layer does not list them)
            parts = path.split("/")
            for i in range(1, len(parts)):
                a = "/".join(parts[:i])
                if a not in state:
                    state[a] = File(tarfile.TarInfo(a))
                    state[a].kind, state[a].mode, state[a].uid, state[a].gid = "dir", 0o755, 0, 0
                elif state[a].kind != "dir":
                    sys.exit(f"convert: {path}: {a} is not a directory")
            if m.islnk():
                target = norm(m.linkname)
                if target not in state:
                    sys.exit(f"convert: {path}: hard link to {target}, which is not in the image so far")
                if path in state and state[path].kind == "dir":
                    for p in subtree(state, path):
                        del state[p]
                state[path] = state[target]
                continue
            data = None
            if m.isreg():
                counter[0] += 1
                data = os.path.join(data_dir, str(counter[0]))
                with tf.extractfile(m) as s, open(data, "wb") as o:
                    shutil.copyfileobj(s, o)
            f = File(m, data)
            old = state.get(path)
            if old is not None and old.kind == "dir" and f.kind != "dir":
                for p in subtree(state, path):
                    del state[p]
            elif old is not None and old.kind != "dir" and f.kind == "dir":
                del state[path]
            # a directory over a directory keeps the children (they are paths of their own) and takes the upper's metadata
            state[path] = f


def flat_tar(state, out):
    seen = {}
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tf:
        for path in sorted(state, key=lambda p: p.split("/")):
            f = state[path]
            ti = tarfile.TarInfo(path)
            ti.mode, ti.uid, ti.gid, ti.mtime = f.mode, f.uid, f.gid, 0
            if f.kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif id(f) in seen:
                ti.type, ti.linkname = tarfile.LNKTYPE, seen[id(f)]
                tf.addfile(ti)
            else:
                seen[id(f)] = path
                if f.kind == "reg":
                    ti.type, ti.size = tarfile.REGTYPE, f.size
                    with open(f.data, "rb") as d:
                        tf.addfile(ti, d)
                elif f.kind == "sym":
                    ti.type, ti.linkname = tarfile.SYMTYPE, f.link
                    tf.addfile(ti)
                else:
                    ti.type = {"chr": tarfile.CHRTYPE, "blk": tarfile.BLKTYPE, "fifo": tarfile.FIFOTYPE}[f.kind]
                    ti.devmajor, ti.devminor = f.major, f.minor
                    tf.addfile(ti)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--inline", action="store_true")
    ap.add_argument("--build-time0", action="store_true")
    ap.add_argument("--mkfs", default=os.environ.get("MKFS_EROFS", "mkfs.erofs"))
    ap.add_argument("--platform", default="linux/arm64")
    a = ap.parse_args()
    src = Source(a.image)
    config, layers = locate(src, a.platform)
    cfg = json.loads(config).get("config") or {}
    os.makedirs(a.out, exist_ok=True)
    state = {}
    counter = [0]
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.mkdir(data)
        for i, l in enumerate(layers):
            lt = os.path.join(tmp, f"l{i}")
            os.mkdir(lt)
            apply_layer(state, spool(src, l, lt), data, counter)
            os.unlink(os.path.join(lt, "layer.tar"))
        flat = os.path.join(tmp, "flat.tar")
        flat_tar(state, flat)
        root = os.path.join(a.out, "root.erofs")
        cmd = [a.mkfs, "--tar=f", "-b4096"] + ([] if a.inline else ["-E", "noinline_data"]) + (["-T0"] if a.build_time0 else []) + [root, flat]
        print("convert:", " ".join(cmd), file=sys.stderr)
        subprocess.run(cmd, check=True)
    ver = subprocess.run([a.mkfs, "--version"], capture_output=True, text=True)
    with open(os.path.join(a.out, "mkfs.version"), "w") as f:
        f.write((ver.stdout + ver.stderr).strip() + "\n")
    out = {k: cfg[k] for k in ("Env", "Entrypoint", "Cmd", "WorkingDir", "User") if cfg.get(k)}
    if cfg.get("ExposedPorts"):
        out["ExposedPorts"] = sorted(cfg["ExposedPorts"])
    with open(os.path.join(a.out, "config.json"), "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    kinds = {}
    for f in state.values():
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    print(f"convert: {len(layers)} layers, {len(state)} paths {kinds}, {os.path.getsize(root)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
