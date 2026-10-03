"""Rove as someone installs it, and what importing it may do.

The `smoke` tests build the sdist and wheel, install the wheel into a fresh virtualenv
with the locked dependency versions, and run it from a folder outside the source tree:
`rove --help`, every subcommand's `--help`, an import of every module, and the offline
`rove bench fixture`. They need `uv` and a Playwright Chromium, take about a minute, and
run only when selected: `pytest -m smoke`.

The import test near the top runs everywhere: importing a module must not touch the
home folder, the state root, the network, a database or a subprocess.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import rove

# The first smoke test also builds and installs the package; give it room on a cold cache.
pytestmark = pytest.mark.timeout(600)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "rove"
ICON = "rove/assets/rove-app-icon.png"
MODULES = sorted(
    ".".join(("rove", *path.relative_to(SOURCE).with_suffix("").parts)).removesuffix(".__init__")
    for path in SOURCE.rglob("*.py")
)

# Run in a fresh interpreter: install an audit hook, give Python a throwaway home, import
# the named modules, and print every event that touched the outside world.
PROBE = r"""
import json, os, sys
from pathlib import Path

home, state, modules = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
real_home = Path(os.path.expanduser("~"))
private = [home, state] + [real_home / p for p in (
    ".config/rove", ".hermes", ".omlx", ".local/share/rove", "Library/LaunchAgents")]
events = []
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

def private_path(value):
    try:
        path = Path(os.fsdecode(value)).absolute()
    except (TypeError, ValueError):
        return False
    return any(path == p or p in path.parents for p in private)

def hook(event, args):
    if event == "open":
        path, mode, flags = args
        writes = any(c in str(mode or "") for c in "wax+") or bool((flags or 0) & WRITE)
        if path is not None and (writes or private_path(path)):
            events.append([event, str(path), str(mode), flags])
    elif event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod",
                   "os.symlink", "os.truncate", "shutil.rmtree", "shutil.move"}:
        events.append([event, str(args[0])])
    elif event.split(".")[0] in {"subprocess", "socket", "sqlite3", "webbrowser"} or (
        event.startswith("os.") and ("exec" in event or "spawn" in event or event in
        {"os.system", "os.fork", "os.forkpty", "os.kill", "os.startfile"})):
        events.append([event, repr(args)[:200]])

Path.home = classmethod(lambda cls: home)
sys.addaudithook(hook)
import importlib
for name in modules:
    importlib.import_module(name)
print(json.dumps({"events": events, "state_created": state.exists(),
                  "home_entries": sorted(p.name for p in home.iterdir()),
                  "files": {n: sys.modules[n].__file__ for n in modules}}))
"""


def probe(python: str, modules: list[str], scratch: Path, env: dict | None = None) -> dict:
    home, state = scratch / "home", scratch / "state"
    home.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [python, "-c", PROBE, str(home), str(state), *modules],
        cwd=scratch,
        env={**(env or os.environ), "PYTHONDONTWRITEBYTECODE": "1", "ROVE_STATE_DIR": str(state)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.splitlines()[-1])


def assert_clean(found: dict, module: str):
    assert found["events"] == [], f"importing {module} did: {found['events']}"
    assert not found["state_created"], f"importing {module} created the state root"
    assert found["home_entries"] == [], f"importing {module} wrote to the home folder"


def test_importing_any_module_touches_no_home_state_network_or_process(tmp_path):
    """Every module, imported alone in a fresh interpreter, only defines things."""
    env = {**os.environ, "PYTHONPATH": str(Path(rove.__file__).parent.parent)}
    env.pop("OBSIDIAN_VAULT_PATH", None)
    for module in MODULES:
        found = probe(sys.executable, [module], tmp_path / module, env)
        assert_clean(found, module)
        assert Path(found["files"][module]).is_relative_to(Path(rove.__file__).parent)


def test_the_import_probe_sees_a_module_that_does_something(tmp_path):
    """The check above is only as good as its probe: a module with side effects is caught."""
    (tmp_path / "busy_module.py").write_text(
        "import sqlite3\n"
        "from pathlib import Path\n"
        "from rove.runtime import state_root\n"
        "state_root()\n"
        "(Path.home() / 'note.txt').write_text('x')\n"
        "sqlite3.connect(':memory:').close()\n"
    )
    package = str(Path(rove.__file__).parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), package])}
    found = probe(sys.executable, ["busy_module"], tmp_path / "scratch", env)
    kinds = {event[0] for event in found["events"]}
    assert {"os.mkdir", "open", "sqlite3.connect"} <= kinds
    assert found["state_created"] and found["home_entries"] == ["note.txt"]


# ---------------------------------------------------------------------------------------
# The built package. Everything below runs only under `-m smoke`.
# ---------------------------------------------------------------------------------------


def run(command: list, cwd: Path, env: dict | None = None, timeout: int = 300):
    result = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, f"{command}\n{result.stdout[-2000:]}\n{result.stderr[-3000:]}"
    return result


def only(found) -> Path:
    found = list(found)
    assert len(found) == 1, found
    return found[0]


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    """The sdist, the wheel, and a fresh virtualenv with the wheel installed."""
    uv = shutil.which("uv")
    assert uv, "the smoke tests build with uv; install it (https://docs.astral.sh/uv/)"
    # CI builds once in its own step and points ROVE_SMOKE_DIST at the result.
    prebuilt = os.environ.get("ROVE_SMOKE_DIST")
    dist = Path(prebuilt).resolve() if prebuilt else tmp_path_factory.mktemp("dist")
    if not prebuilt:
        run([uv, "build", "--out-dir", dist, ROOT], cwd=ROOT)
    constraints = tmp_path_factory.mktemp("constraints") / "constraints.txt"
    run(
        [uv, "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project",
         "--output-file", constraints],
        cwd=ROOT,
    )  # fmt: skip
    venv = tmp_path_factory.mktemp("venv") / "rove"
    run([uv, "venv", "--python", sys.executable, venv], cwd=dist)
    python = venv / "bin" / "python"
    wheel = only(dist.glob("rove-*.whl"))
    run([uv, "pip", "install", "--python", python, wheel, "-c", constraints], cwd=dist)
    outside = tmp_path_factory.mktemp("outside")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "VIRTUAL_ENV", "OBSIDIAN_VAULT_PATH", "PYTHONHOME"}
    }
    env.update(
        {
            "PATH": f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}",
            "ROVE_STATE_DIR": str(outside / "state"),
            "ROVE_MODEL_API_KEY": "smoke-test-key",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return {
        "sdist": only(dist.glob("rove-*.tar.gz")),
        "wheel": wheel,
        "python": python,
        "rove": venv / "bin" / "rove",
        "env": env,
        "outside": outside,
    }


@pytest.mark.smoke
def test_the_wheel_holds_every_module_and_the_app_icon(package):
    names = set(zipfile.ZipFile(package["wheel"]).namelist())
    source = {f"rove/{p.relative_to(SOURCE).as_posix()}" for p in SOURCE.rglob("*.py")}
    assert source <= names, f"missing from the wheel: {sorted(source - names)}"
    assert ICON in names
    assert not [n for n in names if re.search(r"(^|/)(tests?|docs|scripts)/", n)]


@pytest.mark.smoke
def test_the_sdist_holds_the_source_and_nothing_private(package):
    with tarfile.open(package["sdist"]) as sdist:
        names = {name.split("/", 1)[1] for name in sdist.getnames() if "/" in name}
    for needed in ("pyproject.toml", "LICENSE", "README.md", "src/rove/__init__.py", f"src/{ICON}"):
        assert needed in names, needed
    private = [
        n for n in names if re.search(r"(^|/)(\.venv|\.env|\.git)(/|$)|\.(sqlite3?|db|pem|key)$", n)
    ]
    assert private == []


@pytest.mark.smoke
def test_the_installed_package_is_the_wheel_not_the_source_tree(package):
    found = run(
        [
            package["python"],
            "-c",
            "import importlib.resources, rove; print(rove.__file__); "
            "print(importlib.resources.files('rove').joinpath('assets/rove-app-icon.png')"
            ".read_bytes() == open(__import__('sys').argv[1], 'rb').read())",
            SOURCE / "assets" / "rove-app-icon.png",
        ],
        cwd=package["outside"],
        env=package["env"],
    ).stdout.split()
    assert not Path(found[0]).is_relative_to(ROOT), found[0]
    assert "site-packages" in found[0]
    assert found[1] == "True", "the installed app icon differs from the source copy"


@pytest.mark.smoke
def test_rove_help_and_every_subcommand_help(package):
    top = run([package["rove"], "--help"], cwd=package["outside"], env=package["env"]).stdout
    choices = re.search(r"\{([a-z0-9_,-]+)\}", top)
    assert choices, top
    commands = choices.group(1).split(",")
    assert {"bench", "workflow", "browser", "mail", "onboarding"} <= set(commands)
    for command in commands:
        shown = run(
            [package["rove"], command, "--help"], cwd=package["outside"], env=package["env"]
        )
        assert shown.stdout.startswith("usage: rove " + command), command
    assert not (package["outside"] / "state").exists(), "a --help created the state root"


@pytest.mark.smoke
@pytest.mark.parametrize("module", MODULES)
def test_every_module_imports_from_the_installed_package(package, module, tmp_path):
    found = probe(str(package["python"]), [module], tmp_path, package["env"])
    assert_clean(found, module)
    assert "site-packages" in found["files"][module]


@pytest.mark.smoke
def test_the_offline_bench_fixture_runs_from_the_installed_package(package):
    shown = run(
        [package["rove"], "bench", "fixture"],
        cwd=package["outside"],
        env=package["env"],
        timeout=240,
    ).stdout
    assert "1 application · 3 preparation passes · 1 submission" in shown, shown
    # The fixture keeps its own throwaway state root; the configured one is left alone.
    state = package["outside"] / "state"
    assert not (state / "recruiting.sqlite3").exists()
