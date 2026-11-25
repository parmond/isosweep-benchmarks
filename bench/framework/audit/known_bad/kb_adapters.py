"""Known-bad adapters for the auditor self-test. NEVER referenced by the real spec."""
import numpy as np


class ReadsFileInCompute:
    def compute(self):
        with open(self.path, "rb") as fh:          # file I/O inside the timed region
            return np.frombuffer(fh.read(), dtype="<f8")


class PlotsInCompute:
    def compute(self):
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()                   # plotting objects inside the timed region
        return ax.contour(self.z, levels=self.levels)


class CallsUnlistedFunction:
    def compute(self):
        return np.fromfile(self.path)              # not in the allowlist (and denylisted)


class HiddenIoViaAllowlistedHelper:
    """Static check passes (helper is allowlisted) — only the dynamic check can catch it."""
    def compute(self):
        return self.kernel(self.z)
