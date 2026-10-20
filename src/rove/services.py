"""Install the local browser and deterministic feed services on macOS."""

import contextlib
import json
import os
import plistlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .runtime import hold_path, on_hold, state_root, write_private

SERVICE_PATH = str(Path.home() / ".local/bin") + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# (name, rove command, launchd interval in seconds; None keeps a daemon running).
# Each timed tick decides for itself whether it is enabled, so an unconfigured service
# (the feed without feed.json, mail without the Zoho values) runs and does nothing.
SERVICES = [
    ("browser", ["browser", "serve"], None),
    ("feed", ["feed", "tick"], 900),
    ("workflow", ["workflow", "tick"], 30),
    ("mail", ["mail", "tick"], 900),
]
# The Hermes gateway is the Discord bot; a hold stops it with Rove's own services.
GATEWAY = "ai.hermes.gateway"


def install():
    executable = Path(__file__).resolve().parents[2] / ".venv/bin/rove"
    if not executable.is_file():
        raise ValueError("Install the project virtual environment first")
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    logs = state_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = []
    for name, command, interval in SERVICES:
        label = "dev.rove." + name
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
                "ROVE_STATE_DIR": str(state_root()),
            },
            "StandardOutPath": str(logs / (name + ".out.log")),
            "StandardErrorPath": str(logs / (name + ".err.log")),
            "ProcessType": "Interactive" if name == "browser" else "Background",
            "RunAtLoad": True,
        }
        if interval is not None:
            data["StartInterval"] = interval
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
                "interval_seconds": interval,
            }
        )
    return result


def labels() -> list[str]:
    return ["dev.rove." + name for name, _, _ in SERVICES] + [GATEWAY]


def launchctl(*args: str) -> subprocess.CompletedProcess:
    command = ["/bin/launchctl", *args]
    return subprocess.run(command, capture_output=True, check=False)  # noqa: S603 -- own labels


def loaded(label: str) -> bool:
    return launchctl("print", f"gui/{os.getuid()}/{label}").returncode == 0


def hold(action: str) -> dict:
    """Put every service on hold, take it off, or report it.

    `on` writes the hold file first, so a tick that starts during the stop already does
    nothing, then disables and unloads each launchd agent so none returns at login, and
    stops the local model. `off` removes the file before loading the agents again, so the
    browser service does not start, see the hold and exit. The private config switches
    (`enabled` in workflow, feed and mail) are left as they are either way.
    """
    domain = f"gui/{os.getuid()}"
    if action == "on":
        if not on_hold():
            write_private(hold_path(), {"since": datetime.now(UTC).isoformat()})
        for label in labels():
            launchctl("disable", f"{domain}/{label}")
            launchctl("bootout", f"{domain}/{label}")
        omlx = Path.home() / ".omlx/bin/omlx"
        if omlx.is_file():
            subprocess.run([str(omlx), "stop"], capture_output=True, check=False)  # noqa: S603 -- fixed path
    elif action == "off":
        hold_path().unlink(missing_ok=True)
        agents = Path.home() / "Library/LaunchAgents"
        for label in labels():
            path = agents / (label + ".plist")
            if path.exists():
                launchctl("enable", f"{domain}/{label}")
                if not loaded(label):
                    launchctl("bootstrap", domain, str(path))
    since = None
    if on_hold():
        with contextlib.suppress(ValueError, OSError):
            since = json.loads(hold_path().read_text()).get("since")
    return {
        "held": on_hold(),
        "since": since,
        "services": {label: "running" if loaded(label) else "stopped" for label in labels()},
    }
