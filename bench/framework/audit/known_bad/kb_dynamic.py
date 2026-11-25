"""Live strace fixtures: one action between ISOBENCH markers. Usage: kb_dynamic.py CASE TMPFILE"""
import concurrent.futures
import gc
import mmap
import os
import socket
import subprocess
import sys

import numpy as np

case, tmp = sys.argv[1], sys.argv[2]
a = np.random.default_rng(0).random(2_000_000)
pre = open(tmp, "rb")
_ = np.sort(a)
gc.collect()


def act():
    if case == "clean":
        return np.sort(a)
    if case == "threads":
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            return list(ex.map(np.sort, np.array_split(a, 4)))
    if case == "file_open_read":
        with open(tmp, "rb") as fh:
            return fh.read()
    if case == "read_preopened":
        # the raw descriptor: a buffered read would be served from the buffer the
        # warm-up filled, with no syscall in the window (2026-11-21 preflight)
        return os.read(pre.fileno(), 16)
    if case == "subprocess":
        return subprocess.run(["/bin/true"])
    if case == "socket":
        return socket.socket()
    if case == "mmap_file":
        return mmap.mmap(pre.fileno(), 0, access=mmap.ACCESS_READ)
    if case == "stat":
        return os.stat(tmp)
    raise SystemExit(f"unknown case {case}")


act()          # warm-up, as the harness warms every tool (P2: lazy imports and first-use
               # setup happen before the window; the action itself still runs inside it)
os.write(2, b"ISOBENCH_T0\n")
r = act()
os.write(2, b"ISOBENCH_T1\n")
