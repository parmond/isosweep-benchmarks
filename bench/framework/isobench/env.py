"""Environment description and environment-class checks (reference/preflight)."""
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _imds(path):
    """EC2 instance metadata (IMDSv2); None off-EC2."""
    try:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        tok = urllib.request.urlopen(req, timeout=1).read().decode()
        req = urllib.request.Request(f"http://169.254.169.254/latest/meta-data/{path}",
                                     headers={"X-aws-ec2-metadata-token": tok})
        return urllib.request.urlopen(req, timeout=1).read().decode()
    except Exception:  # noqa: BLE001
        return None


def collect():
    e = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "platform": platform.platform(),
         "system": platform.system(), "machine": platform.machine(), "python": sys.version.split()[0],
         "logical_cpus": os.cpu_count(),
         # R7: names of every variable (values are not recorded: they may hold secrets)
         "process_env_names": sorted(os.environ)}
    if platform.system() == "Linux":
        model = None
        for line in (_read("/proc/cpuinfo") or "").splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
        if not model:
            for line in (_run(["lscpu"]) or "").splitlines():
                if line.startswith("Model name"):
                    model = line.split(":", 1)[1].strip()
        e["cpu"] = model
        cores = _run(["lscpu", "-p=CORE,SOCKET"])
        e["physical_cores"] = len({l for l in (cores or "").splitlines() if l and not l.startswith("#")}) or None
        govs = set()
        for cpu in range(os.cpu_count() or 0):
            g = _read(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
            if g:
                govs.add(g)
        e["governors"] = sorted(govs)
        nt = _read("/sys/devices/system/cpu/intel_pstate/no_turbo")
        boost = _read("/sys/devices/system/cpu/cpufreq/boost")
        e["turbo_disabled"] = (nt == "1") if nt is not None else ((boost == "0") if boost is not None else None)
        thp = _read("/sys/kernel/mm/transparent_hugepage/enabled") or ""
        e["thp"] = thp[thp.find("[") + 1:thp.find("]")] if "[" in thp else thp or None
        e["aslr"] = _read("/proc/sys/kernel/randomize_va_space")
        e["kernel"] = platform.release()
        e["virtualization"] = _run(["systemd-detect-virt"]) or "unknown"
        flags = ""
        for line in (_read("/proc/cpuinfo") or "").splitlines():
            if line.lower().startswith(("flags", "features")):
                flags = line.split(":", 1)[1]
                break
        e["hypervisor_cpu_flag"] = "hypervisor" in flags.split()
        e["load_avg_1m"] = os.getloadavg()[0]
        e["instance_type"] = _imds("instance-type")
        e["instance_id"] = _imds("instance-id")
        e["strace"] = _run(["strace", "-V"]).splitlines()[0] if shutil.which("strace") else None
    else:
        e["cpu"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        e["load_avg_1m"] = os.getloadavg()[0]
        e["strace"] = None
    return e


def check(env, env_class, policy):
    """Return list of (requirement, ok, detail) for an environment class."""
    spec = policy["environments"][env_class]
    req = spec.get("require", {})
    res = []
    if "instance_types" in spec and env_class != "indicative":
        ok = env.get("instance_type") in spec["instance_types"]
        res.append(("instance_type", ok, f"{env.get('instance_type')} in {spec['instance_types']}"))
    for k, want in req.items():
        if k == "bare_metal":
            it = env.get("instance_type") or ""
            ok = env.get("hypervisor_cpu_flag") is False and ".metal" in it
            res.append((k, ok == want, f"hypervisor_flag={env.get('hypervisor_cpu_flag')} instance={it} virt={env.get('virtualization')}"))
        elif k == "turbo_disabled":
            res.append((k, env.get("turbo_disabled") is True, f"{env.get('turbo_disabled')}"))
        elif k == "governor":
            g = env.get("governors") or []
            res.append((k, g == [want], f"{g} == [{want}]"))
        elif k == "thp":
            res.append((k, env.get("thp") == want, f"{env.get('thp')} == {want}"))
        elif k == "aslr":
            res.append((k, str(env.get("aslr")) == str(want), f"{env.get('aslr')} == {want}"))
        elif k == "max_load_avg_1m":
            la = env.get("load_avg_1m")
            res.append((k, la is not None and la <= want, f"{la} <= {want}"))
        elif k == "strace":
            res.append((k, bool(env.get("strace")) == want, f"{env.get('strace')}"))
        elif k == "process_env_clean":
            names = env.get("process_env_names")
            extra = None if names is None else sorted(set(names) - set(policy["process_env"]["allow"]))
            res.append((k, (extra == []) == want, f"not allowed: {extra}"))
        else:
            res.append((k, False, f"unknown requirement {k}"))
    return res
