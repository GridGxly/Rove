"""Install the local browser and deterministic feed services on macOS."""

import os
import plistlib
import subprocess
from pathlib import Path

from .runtime import state_root

SERVICE_PATH = str(Path.home() / ".local/bin") + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def install():
    executable = Path(__file__).resolve().parents[2] / ".venv/bin/autopilot"
    if not executable.is_file():
        raise ValueError("Install the project virtual environment first")
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    logs = state_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = []
    for name, command in [
        ("browser", ["browser", "serve"]),
        ("feed", ["feed", "tick"]),
        ("workflow", ["workflow", "tick"]),
    ]:
        label = "dev.erga-autopilot." + name
        path = agents / (label + ".plist")
        data = {
            "Label": label,
            "ProgramArguments": [str(executable), *command],
            "WorkingDirectory": str(executable.parent.parent.parent),
            "EnvironmentVariables": {
                # A fixed PATH keeps the plist byte-identical across installs; a PATH copied
                # from the installing shell forced a reload (and a browser restart) whenever
                # the shell differed. Every tool the services run is addressed absolutely.
                "PATH": SERVICE_PATH,
                "AUTOPILOT_STATE_DIR": str(state_root()),
            },
            "StandardOutPath": str(logs / (name + ".out.log")),
            "StandardErrorPath": str(logs / (name + ".err.log")),
            "ProcessType": "Interactive" if name == "browser" else "Background",
            "RunAtLoad": name != "browser",
        }
        if name != "browser":
            data["StartInterval"] = 900 if name == "feed" else 30
        domain = f"gui/{os.getuid()}"
        unchanged = path.exists() and plistlib.loads(path.read_bytes()) == data
        loaded = (
            subprocess.run(
                ["launchctl", "print", domain + "/" + label], capture_output=True, check=False
            ).returncode
            == 0
        )
        if not unchanged or not loaded:
            with open(path, "wb") as f:
                plistlib.dump(data, f)
            path.chmod(0o600)
            if loaded:
                subprocess.run(
                    ["launchctl", "bootout", domain + "/" + label], capture_output=True, check=True
                )
            subprocess.run(
                ["launchctl", "bootstrap", domain, str(path)], capture_output=True, check=True
            )
        result.append(
            {
                "service": label,
                "installed": True,
                "unchanged": unchanged and loaded,
                "interval_seconds": (900 if name == "feed" else 30) if name != "browser" else None,
            }
        )
    return result
