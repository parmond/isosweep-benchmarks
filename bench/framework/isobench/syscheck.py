"""Dynamic check: nothing but computation inside the timed region (allowlist, R6).

A validation process (validate_one.py) runs one compute() between two marker
writes (ISOBENCH_T0 / ISOBENCH_T1 on fd 2) under `strace -f -y`. Every
syscall logged between the markers — from any thread or child process — is
classified against the policy in global.yaml (syscall_audit).
"""
import re
import shutil
import subprocess

LINE = re.compile(r"^(?P<pid>\d+)\s+(?:<\.\.\. (?P<rname>\w+) resumed>|(?P<name>\w+)\()(?P<rest>.*)$")
FD = re.compile(r"^\s*(-?\d+)<([^>]*)>")


def available():
    return shutil.which("strace") is not None


def run_traced(cmd, log_path, policy, cwd=None, env=None, timeout=3600):
    sa = policy["syscall_audit"]
    trace = sa["tool"].split()
    p = subprocess.run(trace + ["-o", log_path, "--"] + cmd, cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _fd_kind(annot):
    if annot.startswith("pipe:"):
        return "pipe"
    if annot.startswith("anon_inode:"):
        return "anon_inode"
    if annot.startswith("socket:"):
        return "socket"
    if annot.startswith("/"):
        return "path"
    return "other"


_STR = re.compile(r'"((?:\\.|[^"\\])*)"')


def _open_path_flags(rest):
    m = _STR.search(rest)
    if not m:
        return None, ""
    return m.group(1), rest[m.end():]


def _lines(log_path):
    import gzip
    op = gzip.open if log_path.endswith(".gz") else open
    with op(log_path, "rt", errors="replace") as fh:
        yield from fh


def analyze(log_path, policy):
    """Allowlist model (R6) → dict(ok, window_found, violations[], window_syscalls,
    allowed_path_reads[])."""
    sa = policy["syscall_audit"]
    begin, end = sa["marker"]["begin"], sa["marker"]["end"]
    allowed = set(sa["allowed_syscalls"])
    thread_sc = set(sa["thread_syscalls"])
    fd_sc = set(sa["fd_syscalls"])
    open_sc = set(sa["path_open_syscalls"])
    kinds = set(sa["allowed_fd_kinds"])
    paths = set(sa["allowed_read_paths"])
    inside, seen_begin, seen_end = False, False, False
    violations, count, path_reads = [], 0, set()
    for raw in _lines(log_path):
        m = LINE.match(raw.strip())
        if not m:
            continue
        name = m.group("name") or m.group("rname")
        rest = m.group("rest")
        if name == "write" and begin in rest and not seen_begin:
            inside, seen_begin = True, True
            continue
        if name == "write" and end in rest and inside:
            inside, seen_end = False, True
            break
        if not inside or m.group("rname"):   # 'resumed' lines repeat a call already seen
            continue
        count += 1
        line = raw.strip()
        if name == "mmap":
            args = rest.split(",")
            fd = args[4].strip() if len(args) >= 5 else ""
            if sa.get("mmap_file_backed_forbidden") and not fd.startswith("-1"):
                violations.append(line)
        elif name in allowed:
            continue
        elif name in thread_sc:
            if sa["clone_requires_flag"] not in rest:
                violations.append(line)
        elif name in open_sc:
            path, after = _open_path_flags(rest)
            ro = "O_RDONLY" in after and not any(f in after for f in ("O_WRONLY", "O_RDWR", "O_CREAT",
                                                                      "O_TRUNC", "O_APPEND"))
            if path in paths and ro:
                path_reads.add(path)
            else:
                violations.append(line)
        elif name in fd_sc:
            fm = FD.match(rest)
            kind = _fd_kind(fm.group(2)) if fm else "unannotated"
            if kind in kinds:
                continue
            if kind == "path" and fm.group(2) in paths and name in ("read", "pread64", "close", "fstat",
                                                                    "newfstatat", "lseek"):
                path_reads.add(fm.group(2))
                continue
            violations.append(line)
        else:
            violations.append(line)            # not on the allowlist
    ok = seen_begin and seen_end and not violations
    return {"ok": ok, "window_found": seen_begin and seen_end, "violations": violations[:50],
            "n_violations": len(violations), "window_syscalls": count,
            "allowed_path_reads": sorted(path_reads)}
