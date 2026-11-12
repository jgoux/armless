#!/usr/bin/env python3
"""The armless filesystem (fs.json and blobs/) of an image, made from the same flattened tar that root.erofs is built from.

    armless_fs.py IMAGE.tar OUT_DIR ARMLESS_SRC

IMAGE.tar is a `docker save` tar (what gates/pk0/convert.py reads). The layers are applied with convert.py's own functions (whiteouts,
opaque directories, hard links, mtime 0, sorted: `flat_tar`), so the tree is the one the package holds; then armless's own
tests/fs/fs2json.py and copy-to-sha256.py (at the pinned commit, ARMLESS_SRC) turn that tar into OUT_DIR/fs.json and OUT_DIR/blobs, the layout
`armless-exec --fs` reads. OUT_DIR/tree.json is what the harness needs to launch the image the way T0 does without reading blobs: the passwd file, every
path as [kind, mode, link target] and the first line of every executable script (`#!`). Needs python3 with the `zstandard` module (armless's scripts use it).
"""
import importlib.util, json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    image, out, armless = sys.argv[1:]
    spec = importlib.util.spec_from_file_location("pk0_convert", os.path.join(HERE, "..", "pk0", "convert.py"))
    cv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cv)
    src = cv.Source(image)
    config, layers = cv.locate(src, "linux/arm64")
    state, counter = {}, [0]
    os.makedirs(out, exist_ok=True)
    # config.json, as convert.py main writes it (the package's config, which is what the launch is derived from); not in the original, which
    # got it from the package next to this directory
    cfg = json.loads(config).get("config") or {}
    conf = {k: cfg[k] for k in ("Env", "Entrypoint", "Cmd", "WorkingDir", "User") if cfg.get(k)}
    if cfg.get("ExposedPorts"):
        conf["ExposedPorts"] = sorted(cfg["ExposedPorts"])
    with open(os.path.join(out, "config.json"), "w") as o:
        json.dump(conf, o, indent=1)
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.mkdir(data)
        for i, l in enumerate(layers):
            lt = os.path.join(tmp, f"l{i}")
            os.mkdir(lt)
            cv.apply_layer(state, cv.spool(src, l, lt), data, counter)
            os.unlink(os.path.join(lt, "layer.tar"))
        tree = {"passwd": "", "paths": {}, "shebangs": {}}
        for path, f in state.items():
            tree["paths"]["/" + path] = [f.kind, f.mode & 0o7777, f.link if f.kind == "sym" else ""]
            if f.kind == "reg" and f.mode & 0o111 and f.size >= 2:
                with open(f.data, "rb") as d:
                    head = d.read(512)
                if head.startswith(b"#!"):
                    tree["shebangs"]["/" + path] = head.split(b"\n", 1)[0].decode("utf-8", "replace")
        pw = state.get("etc/passwd")
        if pw is not None and pw.kind == "reg":
            with open(pw.data, "rb") as d:
                tree["passwd"] = d.read().decode("utf-8", "replace")
        with open(os.path.join(out, "tree.json"), "w") as o:
            json.dump(tree, o)
        flat = os.path.join(tmp, "flat.tar")
        cv.flat_tar(state, flat)
        fs = os.path.join(out, "fs.json")
        blobs = os.path.join(out, "blobs")
        os.makedirs(blobs, exist_ok=True)
        tools = os.path.join(armless, "tests", "fs")
        # both scripts log every file to stderr: the log is a file next to the result, not the job's output
        with open(os.path.join(out, "armless-fs.log"), "w") as log:
            subprocess.run([sys.executable, os.path.join(tools, "fs2json.py"), "--zstd", "--out", fs, flat], check=True, stdout=log, stderr=log)
            subprocess.run([sys.executable, os.path.join(tools, "copy-to-sha256.py"), "--zstd", flat, blobs], check=True, stdout=log, stderr=log)
    print(f"armless_fs: {len(layers)} layers, {len(state)} paths, {os.path.getsize(fs)} bytes of fs.json, {len(os.listdir(blobs))} blobs", file=sys.stderr)


if __name__ == "__main__":
    main()
