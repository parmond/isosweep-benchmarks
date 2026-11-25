#!/bin/bash
# Copyright 2026 Parmond AB
# SPDX-License-Identifier: Apache-2.0
# tune_cpu.sh — Configure Linux system for reproducible benchmarking.
#
# Run as root on the benchmark instance. Sets deterministic CPU state:
# - Fixed CPU frequency (disable turbo boost)
# - Performance governor on all cores
# - Disable ASLR and transparent huge pages
#
# Reversible: reboot restores default settings.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root" >&2
    exit 1
fi

echo "=== CPU tuning for benchmarking ==="

# --- Disable turbo boost ---
if [ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]; then
    echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo
    echo "  Intel turbo boost disabled"
elif [ -f /sys/devices/system/cpu/cpufreq/boost ]; then
    echo 0 > /sys/devices/system/cpu/cpufreq/boost
    echo "  AMD/generic turbo boost disabled"
else
    echo "  Turbo boost control not found (may be N/A for this CPU)"
fi

# --- Set performance governor ---
GOVERNOR_SET=0
for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
    if [ -d "$cpu_dir" ]; then
        echo performance > "$cpu_dir/scaling_governor" 2>/dev/null || true
        GOVERNOR_SET=1
    fi
done
if [ "$GOVERNOR_SET" -eq 1 ]; then
    echo "  CPU governor set to 'performance'"
else
    echo "  No cpufreq governor found"
fi

# --- Disable ASLR ---
echo 0 > /proc/sys/kernel/randomize_va_space
echo "  ASLR disabled"

# --- Disable transparent huge pages ---
if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
    echo never > /sys/kernel/mm/transparent_hugepage/enabled
    echo "  Transparent huge pages disabled"
fi

# --- Report CPU info ---
echo ""
echo "=== System info ==="
lscpu | grep -E "^(Architecture|Model name|CPU\(s\)|Thread|Core|Socket|CPU max MHz|CPU min MHz)"
echo ""
echo "Tuning complete."
