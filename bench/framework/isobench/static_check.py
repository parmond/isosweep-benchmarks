"""Static check of what runs inside the timed region.

Python adapters: every call in compute() must match the adapter's
compute_allowlist, and none may match the global static_denylist.
Native workers: the same over source text between BEGIN_TIMED / END_TIMED
markers (every such block in the file, including callbacks invoked by the
library during compute: "BEGIN_TIMED callee"). R11 clock adjacency: the wall
clock must be read on the statement line directly before every (non-callee)
BEGIN_TIMED and directly after its END_TIMED, and nowhere else in the file, so
the region the clock measures is exactly the region analysed here.
"""
import ast
import fnmatch
import inspect
import re
import textwrap

KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "match", "Some", "Ok", "Err", "loop",
            "else", "case", "defined"}


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else f"*.{node.attr}"
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return None


def python_calls(cls):
    src = textwrap.dedent(inspect.getsource(cls.compute))
    tree = ast.parse(src)
    calls = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            calls.append(_dotted(n.func) or "<dynamic>")
    return calls


def native_calls(path):
    text = open(path).read()
    # Markers must be standalone comment lines ("/* BEGIN_TIMED ... */" or
    # "// BEGIN_TIMED"), so prose mentioning the words is never mistaken for one.
    pat = (r"(?ms)^[ \t]*(?:/\*|//)[ \t]*BEGIN_TIMED\b[^\n]*\n"
           r"(.*?)"
           r"^[ \t]*(?:/\*|//)[ \t]*END_TIMED\b")
    blocks = re.findall(pat, text)
    if not blocks:
        return None
    calls = []
    for b in blocks:
        b = re.sub(r"/\*.*?\*/|//[^\n]*", " ", b, flags=re.S)       # strip comments
        b = re.sub(r'"(?:\\.|[^"\\])*"', '""', b)                     # strip string literals
        for m in re.finditer(r"([A-Za-z_]\w*(?:(?:\.|::|->)[A-Za-z_]\w*)*)\s*(?:::<[^>]*>)?\s*\(", b):
            name = m.group(1)
            if name.split(".")[-1].split("::")[-1] in KEYWORDS or name in KEYWORDS:
                continue
            calls.append(name.replace("->", "."))
    return calls


_MARK = re.compile(r"^[ \t]*(?:/\*|//)[ \t]*(BEGIN_TIMED|END_TIMED)\b([^\n]*)$")
WALL_START = [r"\bclk\(\s*CLOCK_MONOTONIC\s*\)", r"\bInstant::now\(\)"]
WALL_STOP = [r"\bclk\(\s*CLOCK_MONOTONIC\s*\)", r"\.elapsed\(\)"]
WALL_ANY = [r"\bclk\(\s*CLOCK_MONOTONIC\s*\)", r"\bInstant::now\(\)", r"\.elapsed\(\)",
            r"\bclock_gettime\(\s*CLOCK_MONOTONIC", r"\bgettimeofday\(", r"\bmach_absolute_time\("]


def _strip_comments_keep_lines(text):
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return [re.sub(r"//.*", "", l) for l in text.split("\n")]


def native_clock_adjacency(path):
    """→ list of problems ([] = the clock brackets exactly the timed blocks)."""
    raw = open(path).read().split("\n")
    code = _strip_comments_keep_lines("\n".join(raw))
    probs, blocks, open_at = [], 0, None
    def neighbour(i, step):
        j = i + step
        while 0 <= j < len(code) and not code[j].strip():
            j += step
        return code[j] if 0 <= j < len(code) else ""
    for i, line in enumerate(raw):
        m = _MARK.match(line)
        if not m:
            continue
        callee = "callee" in m.group(2)
        if m.group(1) == "BEGIN_TIMED":
            open_at = (i, callee)
            if not callee:
                blocks += 1
                if not any(re.search(p, neighbour(i, -1)) for p in WALL_START):
                    probs.append(f"line {i + 1}: BEGIN_TIMED not directly preceded by the wall-clock start")
        else:
            if open_at is None:
                probs.append(f"line {i + 1}: END_TIMED without BEGIN_TIMED")
                continue
            b, callee_b = open_at
            open_at = None
            body = "\n".join(code[b + 1:i])
            if any(re.search(p, body) for p in WALL_ANY):
                probs.append(f"lines {b + 1}-{i + 1}: clock read inside a timed block")
            if not callee_b and not any(re.search(p, neighbour(i, +1)) for p in WALL_STOP):
                probs.append(f"line {i + 1}: END_TIMED not directly followed by the wall-clock stop")
    n_reads = sum(len(re.findall(p, "\n".join(code))) for p in WALL_ANY)
    if n_reads != 2 * blocks:
        probs.append(f"{n_reads} wall-clock reads in the file for {blocks} clocked blocks (want {2 * blocks})")
    return probs


def check_native(path, allowlist, denylist):
    """Calls inside the timed blocks + clock adjacency. → (ok, violations[])."""
    ok, bad = check(native_calls(path), allowlist, denylist)
    adj = native_clock_adjacency(path)
    return ok and not adj, bad + adj


def _matches(name, patterns):
    tail = name.split(".")[-1].split("::")[-1]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(tail, p) or fnmatch.fnmatch(f"*.{tail}", p)
               for p in patterns)


def check(calls, allowlist, denylist):
    """→ (ok, violations[])."""
    if calls is None:
        return False, ["no BEGIN_TIMED/END_TIMED block found"]
    bad = []
    for c in calls:
        if c == "<dynamic>":
            bad.append("dynamic call target (cannot be verified)")
        elif _matches(c, denylist):
            bad.append(f"{c}: matches static_denylist")
        elif not any(fnmatch.fnmatch(c, p) for p in allowlist) and \
                not any(fnmatch.fnmatch(c.split('.')[-1], p.split('.')[-1]) and p.startswith("*.") for p in allowlist):
            bad.append(f"{c}: not in compute_allowlist")
    return not bad, bad
