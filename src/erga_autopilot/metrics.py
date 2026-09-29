"""Observed OS counters. Unavailable measurements stay null, never guessed."""

import re
import subprocess
import time

import psutil


def memory_snapshot() -> dict:
    vm = psutil.virtual_memory()
    swap = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=False
    )
    pressure = subprocess.run(
        ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        capture_output=True,
        text=True,
        check=False,
    )
    stat = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False).stdout
    gpu = subprocess.run(
        ["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    gpu_match = re.search(r'"Device Utilization %"=(\d+)', gpu)
    thermal = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, check=False)
    page_match = re.search(r"page size of (\d+) bytes", stat)
    page = int(page_match.group(1)) if page_match else None
    counts = dict(re.findall(r"^([^:\n]+):\s+(\d+)\.", stat, re.MULTILINE))
    processes = []
    for p in psutil.process_iter(["pid", "name", "exe", "memory_info", "cpu_times"]):
        try:
            name = p.info["name"] or ""
            is_browser = "ms-playwright" in (p.info["exe"] or "")
            is_qmd = "node" in name.lower() and any("/qmd/" in arg for arg in p.cmdline())
            if (
                is_browser
                or is_qmd
                or any(x in name.lower() for x in ["omlx", "python", "chromium", "hermes"])
            ):
                processes.append(
                    {
                        "pid": p.pid,
                        "name": name,
                        "kind": "browser" if is_browser else "qmd" if is_qmd else name,
                        "rss_bytes": p.info["memory_info"].rss,
                        "cpu_seconds": sum(p.info["cpu_times"][:2]),
                    }
                )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    match = re.search(r"used = ([\d.]+)M", swap.stdout)
    return {
        "time": time.time(),
        "physical_bytes": vm.total,
        "available_bytes": vm.available,
        "psutil_used_bytes": vm.used,
        "wired_bytes": getattr(vm, "wired", None),
        "compressed_physical_bytes": int(counts.get("Pages occupied by compressor", 0)) * page
        if page
        else None,
        "file_backed_bytes": int(counts.get("File-backed pages", 0)) * page if page else None,
        "swap_used_bytes": float(match.group(1)) * 1024**2 if match else None,
        "pressure_level": pressure.stdout.strip() or None,
        "processes": processes,
        "gpu_utilization": int(gpu_match.group(1)) if gpu_match else None,
        "gpu_scope": "whole system, not exclusively inference",
        "thermal_state": thermal.stdout.strip() or None,
    }
