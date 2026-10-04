"""`rove doctor`: one plain line per check of a running installation.

Read-only. It calls no model, launches no browser, starts no service and posts nothing:
the browser service is asked for a peek that never reaches the browser, the model server
for its model list, launchd for what it has loaded, SQLite for what waits. The exit code
is 1 when any check finds something wrong.

Every probe of the machine goes through `Probe`, so the checks run against stubs in tests.
"""

import ast
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import services
from .runtime import state_root

# Per outbox table: what still waits to be delivered, and what failed.
DELIVERY_COUNTS = tuple(
    (
        f"SELECT COUNT(*) FROM {table} "  # noqa: S608 -- one of three fixed table names
        "WHERE delivery IN ('pending','sending') AND created_at<?",
        f"SELECT COUNT(*) FROM {table} "  # noqa: S608 -- one of three fixed table names
        "WHERE delivery='failed' AND created_at>?",
    )
    for table in ("application_events", "owner_notices", "system_outbox")
)


def tool(name: str) -> str:
    """A system tool's full path, or its name when the PATH does not have it."""
    return shutil.which(name) or name


ROOT = Path(__file__).resolve().parents[2]
GATEWAY = "ai.hermes.gateway"
# Read-only chat tools, called against the live state; a name the chat module no longer
# has is skipped, so a renamed tool is not reported as broken.
STATUS_TOOLS = (
    "rove_status",
    "whats_waiting",
    "sends_today",
    "job_feed_status",
    "application_workflow_status",
)
# Long-running processes that keep the code they started with: (marker, what, restart).
LONG_RUNNING = (
    (
        "rove browser serve",
        "Browser service",
        "launchctl kickstart -k gui/$(id -u)/dev.rove.browser",
    ),
    ("rove mcp", "Chat tools server", "uv run rove gateway restart"),
)
CONFIG_FILES = ("workflow.json", "feed.json", "mail.json")
QUIET_PASS = timedelta(minutes=2)
UNMARKED_PASS = timedelta(minutes=15)
STUCK_DELIVERY = timedelta(minutes=10)
RECENT_FAILURE = timedelta(hours=24)


@dataclass
class Check:
    ok: bool
    line: str


class Probe:
    """Everything the checks read from the machine."""

    def launchd(self, label: str) -> dict | None:
        """What launchd says about a loaded agent, or None when it is not loaded."""
        try:
            run = subprocess.run(  # noqa: S603 -- a fixed command; only the label varies
                [tool("launchctl"), "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if run.returncode != 0:
            return None
        info: dict = {}
        for line in run.stdout.splitlines():
            key, sep, value = line.strip().partition(" = ")
            if sep and key in {
                "state",
                "pid",
                "last exit code",
                "runs",
                "stdout path",
                "stderr path",
            }:
                info.setdefault(key, value.strip())
        return info

    def mtime(self, path: str) -> float | None:
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return None

    def browser_peek(self) -> dict | None:
        """The browser service's peek, straight over its socket; None when it is not
        there. Never starts the service and never reaches the browser."""
        from .live_browser import read_reply, socket_path

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3)
        try:
            client.connect(str(socket_path()))
            client.sendall(b'{"action": "status", "peek": true}\n')
            reply = json.loads(read_reply(client, 5))
        except (OSError, ValueError, TimeoutError):
            return None
        finally:
            client.close()
        return reply.get("result") if isinstance(reply, dict) else None

    def model(self) -> tuple[str, str]:
        """("answering" | "stopped" | "busy" | "refused", detail)."""
        import httpx

        from .runtime import BASE_URL, api_key

        try:
            key = api_key()
        except (OSError, KeyError, ValueError):
            return "refused", "no API key is configured"
        try:
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(
                    BASE_URL + "/models", headers={"Authorization": "Bearer " + key}
                )
        except httpx.ConnectError:
            return "stopped", ""
        except httpx.TimeoutException:
            return "busy", ""
        except httpx.HTTPError as error:
            return "refused", type(error).__name__
        if response.is_success:
            return "answering", ""
        return "refused", f"HTTP {response.status_code}"

    def processes(self) -> list[tuple[float, str]]:
        """(start time, command line) of every process of this user."""
        try:
            run = subprocess.run(  # noqa: S603 -- a fixed command
                [tool("ps"), "-x", "-o", "lstart=,command="],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        found = []
        for line in run.stdout.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                started = time.mktime(time.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y"))
            except ValueError:
                continue
            found.append((started, " ".join(parts[5:])))
        return found

    def code_changed_at(self) -> float | None:
        """When the checkout's code last changed: its last commit, or a newer source file
        (a merge rewrites the files it changed)."""
        times = []
        try:
            run = subprocess.run(  # noqa: S603 -- a fixed command on this checkout
                [tool("git"), "-C", str(ROOT), "log", "-1", "--format=%ct"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if run.returncode == 0 and run.stdout.strip().isdigit():
                times.append(float(run.stdout.strip()))
        except (OSError, subprocess.TimeoutExpired):
            pass
        source = ROOT / "src/rove"
        times.extend(p.stat().st_mtime for p in source.rglob("*.py") if p.is_file())
        return max(times) if times else None

    def tick_running(self) -> bool:
        """Whether a worker tick holds its lock right now (a preparation is then live)."""
        path = state_root() / "workflow.lock"
        if not path.exists():
            return False
        with path.open() as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def free_bytes(self) -> int:
        from .recovery import free_bytes

        return free_bytes()


def ago(seconds: float) -> str:
    seconds = max(seconds, 0)
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h ago"
    return f"{int(seconds // 86400)} days ago"


def minutes(delta: timedelta) -> str:
    total = int(delta.total_seconds() // 60)
    return f"{total} min" if total < 120 else f"{total / 60:.1f} h"


# --- checks -----------------------------------------------------------------------


def check_services(probe: Probe) -> list[Check]:
    checks = []
    now = time.time()
    for name, _command, interval in services.SERVICES:
        label = "dev.rove." + name
        info = probe.launchd(label)
        title = f"{name.capitalize()} service"
        if info is None:
            checks.append(Check(False, f"{title}: not loaded in launchd ({label})"))
            continue
        logs = [info.get("stdout path"), info.get("stderr path")]
        if not any(logs):
            logs = [
                str(state_root() / f"logs/{name}.out.log"),
                str(state_root() / f"logs/{name}.err.log"),
            ]
        stamps = [t for t in (probe.mtime(p) for p in logs if p) if t]
        last = f"last ran {ago(now - max(stamps))}" if stamps else "no run on record"
        if interval is None:
            running = info.get("state") == "running" and info.get("pid")
            checks.append(
                Check(
                    bool(running),
                    f"{title}: {'running' if running else 'loaded but not running'}"
                    + (f" (pid {info['pid']})" if running else f" · {last}"),
                )
            )
            continue
        late = not stamps or now - max(stamps) > 3 * interval + 60
        exit_code = info.get("last exit code", "")
        failing = exit_code not in {"", "0", "(never exited)"}
        words = f"{title}: loaded · {last}"
        if failing:
            words += f" · last exit code {exit_code}"
        if late:
            words += (
                f" · expected every {interval // 60 or interval} {'min' if interval >= 60 else 's'}"
            )
        checks.append(Check(not late and not failing, words))
    return checks


def check_gateway(probe: Probe) -> Check:
    info = probe.launchd(GATEWAY)
    if info is None:
        return Check(False, f"Hermes gateway: not loaded in launchd ({GATEWAY})")
    running = info.get("state") == "running" and info.get("pid")
    return Check(bool(running), "Hermes gateway: " + ("running" if running else "not running"))


def check_browser(probe: Probe) -> Check:
    peek = probe.browser_peek()
    if peek is None:
        return Check(False, "Browser service: not answering on its socket")
    app = peek.get("app") or {}
    name = app.get("name") or "no app"
    words = f"Browser service: answering · uses {name}"
    words += " (open)" if peek.get("browser_running") else " (not open)"
    if app.get("problem"):
        return Check(False, words + f" · {app['problem']}")
    busy = peek.get("busy")
    if busy:
        words += f" · busy with {busy.get('action')} for {int(busy.get('seconds') or 0)} s"
    tabs = len(peek.get("open_tabs") or [])
    if tabs:
        words += f" · {tabs} application tab{'s' if tabs != 1 else ''}"
    return Check(True, words)


def check_profile() -> Check:
    from .onboarding import read_approved

    try:
        approved = read_approved()
    except Exception as error:  # noqa: BLE001 -- every reason is a line here
        detail = " ".join(str(error).split())[:120] or type(error).__name__
        return Check(False, f"Approved profile: does not validate ({detail})")
    return Check(bool(approved.get("profile_hash")), "Approved profile: valid")


def check_model(probe: Probe) -> Check:
    state, detail = probe.model()
    words = {
        "answering": "Model server: answering",
        "stopped": "Model server: stopped (it starts when a job needs it)",
        "busy": "Model server: did not answer in 5 s (busy with a request)",
        "refused": f"Model server: refused the check ({detail})",
    }[state]
    return Check(state != "refused", words)


def known_keys() -> set[str]:
    """Every snake_case string the code itself uses: a config key the code never names is
    one no code reads (a typo, or a key that was removed)."""
    keys: set[str] = set()
    for path in (ROOT / "src/rove").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
        keys.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"[a-z][a-z0-9_]*", node.value)
        )
    return keys


def check_config() -> Check:
    known = known_keys()
    unknown: list[str] = []
    for name in CONFIG_FILES:
        path = state_root() / "config" / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return Check(False, f"Config: {name} is not readable JSON")
        if isinstance(data, dict):
            unknown.extend(f"{key} in {name}" for key in data if key not in known)
    if unknown:
        return Check(False, "Config: keys no code reads: " + ", ".join(sorted(unknown)[:8]))
    return Check(True, "Config: every key is one the code reads")


def check_deliveries() -> Check:
    from . import alerts, delivery

    now = datetime.now(UTC)
    stuck_before = (now - STUCK_DELIVERY).isoformat()
    failed_after = (now - RECENT_FAILURE).isoformat()
    stuck = failed = 0
    with delivery.conn() as conn:
        for waiting_sql, failed_sql in DELIVERY_COUNTS:
            stuck += conn.execute(waiting_sql, (stuck_before,)).fetchone()[0]
            failed += conn.execute(failed_sql, (failed_after,)).fetchone()[0]
    with alerts.db() as conn:
        stuck += conn.execute(
            "SELECT COUNT(*) FROM owner_alerts WHERE delivery='pending' AND raised_at<?",
            (stuck_before,),
        ).fetchone()[0]
    if stuck or failed:
        return Check(
            False,
            f"Discord: {stuck} post{'s' if stuck != 1 else ''} waiting over 10 min · "
            f"{failed} given up in the last day",
        )
    return Check(True, "Discord: nothing waiting, nothing given up in the last day")


def check_applications(probe: Probe) -> Check:
    from . import recovery, workflow
    from .submission import STALE_SEND_AFTER

    now = datetime.now(UTC)
    live = probe.tick_running()
    marked = recovery.marked_passes()
    with workflow.db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id,title,status,updated_at FROM application_queue "
                "WHERE status IN ('PREPARING','SUBMITTING')"
            )
        ]
    with recovery.db() as conn:
        beats = {
            r["application_id"]: r["beat_at"]
            for r in conn.execute("SELECT application_id,beat_at FROM preparation_heartbeats")
        }
    stuck: list[str] = []
    working: list[str] = []
    for row in rows:
        title = workflow.display_title(row)
        latest = max(row["updated_at"], beats.get(row["id"], ""))
        age = now - datetime.fromisoformat(latest)
        if row["status"] == "SUBMITTING":
            (stuck if age > STALE_SEND_AFTER else working).append(
                f"{title} sending for {minutes(age)}"
            )
            continue
        limit = QUIET_PASS if row["id"] in marked else UNMARKED_PASS
        if live or age <= limit:
            working.append(f"{title} preparing, last sign of life {minutes(age)} ago")
        else:
            stuck.append(f"{title} preparing with no sign of life for {minutes(age)}")
    if stuck:
        return Check(False, "Applications stuck: " + "; ".join(stuck[:4]))
    if working:
        return Check(True, "Applications: " + "; ".join(working[:4]))
    return Check(True, "Applications: none in preparation or sending")


def check_chat_tools() -> Check:
    try:
        from . import server
    except Exception as error:  # noqa: BLE001 -- an import failure is the finding
        return Check(False, f"Chat tools: the tool server does not load ({type(error).__name__})")
    present = [name for name in STATUS_TOOLS if callable(getattr(server, name, None))]
    if not present:
        return Check(False, "Chat tools: none of the status tools is there")
    broken = []
    for name in present:
        try:
            getattr(server, name)()
        except Exception as error:  # noqa: BLE001 -- each failure is named
            broken.append(f"{name} ({type(error).__name__})")
    if broken:
        return Check(False, "Chat tools failing: " + ", ".join(broken))
    return Check(True, f"Chat tools: all {len(present)} status tools answer")


def check_disk(probe: Probe) -> Check:
    from . import recovery, workflow

    threshold = recovery.min_free_gb(workflow.config())
    try:
        free = probe.free_bytes() / 1e9
    except OSError:
        return Check(False, "Disk: free space could not be read")
    words = f"Disk: {free:.1f} GB free (new work pauses below {threshold:g} GB)"
    return Check(free >= threshold, words)


def check_code(probe: Probe) -> list[Check]:
    changed = probe.code_changed_at()
    if changed is None:
        return [Check(True, "Code: no change time found for this checkout")]
    marker_root = str(ROOT)
    checks = []
    found = probe.processes()
    for marker, what, restart in LONG_RUNNING:
        mine = [
            (start, command)
            for start, command in found
            if marker in command and marker_root in command
        ]
        if not mine:
            continue
        oldest = min(start for start, _ in mine)
        when = datetime.fromtimestamp(changed).astimezone().strftime("%H:%M on %b %d")
        if oldest < changed:
            checks.append(
                Check(
                    False,
                    f"{what}: started before the code changed at {when}; restart it (`{restart}`)",
                )
            )
        else:
            checks.append(Check(True, f"{what}: running the current code"))
    if not checks:
        checks.append(Check(True, "Code: no long-running Rove process from this checkout"))
    return checks


def run(probe: Probe | None = None) -> list[Check]:
    probe = probe or Probe()
    checks: list[Check] = []
    steps = (
        lambda: check_services(probe),
        lambda: [check_gateway(probe)],
        lambda: [check_browser(probe)],
        lambda: [check_profile()],
        lambda: [check_model(probe)],
        lambda: [check_config()],
        lambda: [check_deliveries()],
        lambda: [check_applications(probe)],
        lambda: [check_chat_tools()],
        lambda: [check_disk(probe)],
        lambda: check_code(probe),
    )
    for step in steps:
        try:
            checks.extend(step())
        except Exception as error:  # noqa: BLE001 -- one broken check never hides the rest
            checks.append(
                Check(False, f"A check could not run ({type(error).__name__}: {error})"[:200])
            )
    return checks


def main(probe: Probe | None = None) -> int:
    checks = run(probe)
    for check in checks:
        print(("ok       " if check.ok else "problem  ") + check.line)  # noqa: T201 -- the command's output
    return 0 if all(check.ok for check in checks) else 1
