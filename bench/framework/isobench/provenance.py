# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
"""Source provenance: the exact file set a run is built from, its hash, git state.

TREE defines the deployable source tree. The same list drives (a) the rsync
to the benchmark node (--files-from), (b) the source-tree hash recorded in
every run and compared between preflight and reference runs, and (c) the
source snapshot archived with the results.
"""
import fnmatch
import hashlib
import io
import os
import subprocess
import sys
import tarfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

TREE_INCLUDE = [
    "Makefile", "include/*.h", "src/*.c", "src/*.h",            # libisosweep
    "tools/fieldgen/*.c", "tools/fieldgen/*.h",                 # benchmark field generator
    "cli/*.c", "cli/*.h",
    "python/setup.py", "python/pyproject.toml", "python/isocurves/*.py", "python/isocurves/*.c",
    "bench/testcases/catalog.tsv", "bench/tune_cpu.sh",
    "bench/framework/**/*.py", "bench/framework/**/*.yaml", "bench/framework/**/*.md",
    "bench/framework/**/*.c", "bench/framework/**/*.h", "bench/framework/**/*.rs",
    "bench/framework/**/*.toml", "bench/framework/**/*.lock", "bench/framework/**/*.sh",
    "bench/framework/cloud/*.env",
]
# Post-measurement tooling (site export, claims audit) does not affect what is measured
# and is excluded, so copy edits do not invalidate a preflight.
TREE_EXCLUDE = ["archive/*", "*/target/*", "*/__pycache__/*", "*/node_modules/*", "*/.venv/*", "*/build/*",
                "bench/framework/spec/claims.yaml", "bench/framework/audit/claims.py",
                "bench/framework/export_site.py"]


def _match(rel, pat):
    # fnmatch '*' already crosses '/', so '**/' just needs an optional variant
    return fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.replace("**/", ""))


def tree_files(root=REPO):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir.startswith(("bench/results", ".git", "sites", "patent", "docs")):
            dirnames[:] = []
            continue
        for f in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if any(_match(rel, p) for p in TREE_INCLUDE) and not any(fnmatch.fnmatch(rel, p) for p in TREE_EXCLUDE):
                out.append(rel)
    return sorted(out)


def tree_hash(root=REPO, files=None):
    h = hashlib.sha256()
    for rel in files or tree_files(root):
        with open(os.path.join(root, rel), "rb") as fh:
            h.update(rel.encode() + b"\0" + hashlib.sha256(fh.read()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def git_state(root=REPO):
    def g(*a):
        try:
            return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    head = g("rev-parse", "HEAD")
    if not head:
        return {"available": False}
    files = set(tree_files(root))
    dirty = [l[3:] for l in (g("status", "--porcelain") or "").splitlines() if l[3:] in files]
    return {"available": True, "head": head, "dirty_tree_files": dirty, "clean": not dirty}


def snapshot(path, root=REPO, files=None):
    """Write a deterministic tar.gz of the tree; returns its sha256."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for rel in files or tree_files(root):
            ti = tar.gettarinfo(os.path.join(root, rel), arcname=rel)
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
            with open(os.path.join(root, rel), "rb") as fh:
                tar.addfile(ti, fh)
    data = buf.getvalue()
    with open(path, "wb") as fh:
        fh.write(data)
    return hashlib.sha256(data).hexdigest()


def _first_line(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def toolchain():
    """R8: what built and runs the artifacts (compared preflight vs reference)."""
    import platform
    osr = None
    if os.path.exists("/etc/os-release"):
        for line in open("/etc/os-release"):
            if line.startswith("PRETTY_NAME="):
                osr = line.split("=", 1)[1].strip().strip('"')
    try:
        freeze = subprocess.run([sys.executable, "-m", "pip", "freeze", "--all"], capture_output=True,
                                text=True, timeout=120).stdout.split()
    except (OSError, subprocess.SubprocessError):
        freeze = None
    return {"machine": platform.machine(), "os_release": osr, "libc": "-".join(platform.libc_ver()),
            "python": sys.version.split()[0], "python_executable": sys.executable,
            "cc": _first_line(["cc", "--version"]), "rustc": _first_line(["rustc", "--version"]),
            "cargo": _first_line(["cargo", "--version"]), "cmake": _first_line(["cmake", "--version"]),
            "pip_freeze": sorted(freeze) if freeze is not None else None}


def file_hashes(root, rels):
    out = {}
    for rel in rels:
        h = hashlib.sha256()
        with open(os.path.join(root, rel), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out[rel] = h.hexdigest()
    return out


def artifact_manifest(bundle, root, mode):
    """Build-once artifacts (R8): the bundle a preflight built, installed
    unchanged on the reference node. Per-file hashes of what is on disk now
    plus the bundle's own hash."""
    with tarfile.open(bundle) as t:
        rels = sorted(m.name for m in t.getmembers() if m.isfile())
    h = hashlib.sha256()
    with open(bundle, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"mode": mode, "bundle_sha256": h.hexdigest(), "files": file_hashes(root, rels)}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print("\n".join(tree_files()))
    elif len(sys.argv) > 1 and sys.argv[1] == "manifest":    # manifest BUNDLE ROOT MODE OUT
        import json
        json.dump(artifact_manifest(sys.argv[2], sys.argv[3], sys.argv[4]), open(sys.argv[5], "w"), indent=1)
    else:
        print(tree_hash())
