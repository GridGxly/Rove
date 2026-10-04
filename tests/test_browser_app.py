"""The recruiting browser is its own app: a copy of the owner's Chrome under the state
root with Rove's identity, never the shared Google Chrome unless the private config asks
for it by name. Offline; the macOS tools are recorded, not run."""

import json
import plistlib
import shutil
from pathlib import Path

import pytest

from rove import browser_app, live_browser, workflow

VERSION = "154.0.8037.97"
MACHO = b"\xcf\xfa\xed\xfe" + b"\0" * 28
STOCK_INFO = {
    "CFBundleIdentifier": "com.google.Chrome",
    "CFBundleName": "Chrome",
    "CFBundleDisplayName": "Google Chrome",
    "CFBundleExecutable": "Google Chrome",
    "CFBundleShortVersionString": VERSION,
    "CFBundleIconFile": "app.icns",
    "CFBundleIconName": "AppIcon",
    "CFBundleURLTypes": [{"CFBundleURLSchemes": ["http", "https"]}],
    "LSMinimumSystemVersion": "13.0",
    "KSProductID": "com.google.Chrome",
    "KSChannelID": "universal",
    "KSUpdateURL": "https://tools.google.com/service/update2",
    "KSVersion": VERSION,
    "KSBrandID": "GGRO",
}


def fake_chrome(root: Path, version: str = VERSION, old_version: str = "154.0.8037.58") -> Path:
    """A bundle shaped like Google Chrome: main binary, a framework with two versions and a
    helper app inside the current one, Chrome's icon and Keystone keys."""
    app = root / "Google Chrome.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/Google Chrome").write_bytes(MACHO)
    info = {**STOCK_INFO, "CFBundleShortVersionString": version, "KSVersion": version}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Resources/app.icns").write_bytes(b"chrome icon")
    versions = app / "Contents/Frameworks/Google Chrome Framework.framework/Versions"
    for folder in (version, old_version):
        (versions / folder / "Helpers").mkdir(parents=True)
        (versions / folder / "Google Chrome Framework").write_bytes(MACHO)
        helper = versions / folder / "Helpers/Google Chrome Helper (Renderer).app"
        (helper / "Contents/MacOS").mkdir(parents=True)
        (helper / "Contents/MacOS/Google Chrome Helper (Renderer)").write_bytes(MACHO)
        (helper / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.google.Chrome.helper.renderer"})
        )
    (versions / "Current").symlink_to(version)
    return app


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("ROVE_STATE_DIR", str(root))
    (root / "config").mkdir(parents=True)
    (root / "config/workflow.json").write_text("{}")
    return root


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    app = fake_chrome(tmp_path / "Applications")
    monkeypatch.setattr(browser_app, "SHARED_CHROME", app)
    return app


@pytest.fixture
def tools(monkeypatch):
    """The macOS tools the build runs, recorded: ditto copies, sips and iconutil write
    their outputs, codesign and xattr succeed, pgrep answers from `running`."""
    calls, running = [], {"rove": False}

    def fake(arguments, timeout=600):
        calls.append(arguments)
        name = Path(arguments[0]).name
        code, err = 0, ""
        if name == "ditto":
            shutil.copytree(arguments[1], arguments[2], symlinks=True)
        elif name == "sips":
            Path(arguments[arguments.index("--out") + 1]).write_bytes(b"png")
        elif name == "iconutil":
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"icns")
        elif name == "pgrep":
            code = 0 if running["rove"] else 1
        elif name not in {"codesign", "xattr"}:
            raise AssertionError(f"unexpected tool {arguments}")
        return type("Done", (), {"returncode": code, "stdout": "", "stderr": err})()

    monkeypatch.setattr(browser_app, "tool", fake)
    return calls, running


@pytest.fixture
def launch(monkeypatch):
    """A launcher whose `open` is recorded, whose port answers at once, and whose focus
    handling and system-log lines are captured."""
    commands, lines = [], []
    monkeypatch.setattr(
        live_browser.subprocess, "run", lambda command, **kwargs: commands.append(command)
    )
    monkeypatch.setattr(live_browser, "devtools_alive", lambda port: True)
    monkeypatch.setattr(live_browser, "front_app", lambda: ("", ""))
    monkeypatch.setattr(live_browser, "watch_front", lambda previous, watch_seconds=6.0: {})
    monkeypatch.setattr(browser_app, "profile_holder", lambda profile: None)
    monkeypatch.setattr(workflow, "system_line", lambda who, text: lines.append((who, text)))
    return commands, lines


def plist(app: Path) -> dict:
    return plistlib.loads((app / "Contents/Info.plist").read_bytes())


def test_install_copies_chrome_with_roves_identity(state, chrome, tools):
    calls, _running = tools
    report = browser_app.install()
    assert report["status"] == "installed" and report["installed"] == VERSION
    app = Path(report["path"])
    assert app == state / "browser/Rove Browser.app"
    info = plist(app)
    assert info["CFBundleIdentifier"] == "dev.rove.browser"
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == "Rove Browser"
    assert info["CFBundleIconFile"] == "app.icns" and "CFBundleIconName" not in info
    assert (app / "Contents/Resources/app.icns").read_bytes() == b"icns"
    # The updater's keys are gone; the channel keys and everything else stay.
    assert not any(key in info for key in ("KSUpdateURL", "KSVersion", "KSBrandID"))
    assert info["KSProductID"] == "com.google.Chrome" and info["KSChannelID"] == "universal"
    assert info["CFBundleURLTypes"] == STOCK_INFO["CFBundleURLTypes"]
    assert info["LSMinimumSystemVersion"] == "13.0"
    assert report["removed_keys"] == ["CFBundleIconName", "KSBrandID", "KSUpdateURL", "KSVersion"]
    # The source is untouched, and only the framework version the binary loads is kept.
    assert plist(chrome)["CFBundleIdentifier"] == "com.google.Chrome"
    versions = app / "Contents/Frameworks/Google Chrome Framework.framework/Versions"
    assert sorted(p.name for p in versions.iterdir()) == [VERSION, "Current"]
    assert report["pruned_framework_versions"] == ["154.0.8037.58"]
    record = json.loads(browser_app.record_file().read_text())
    assert record["chrome_version"] == VERSION and record["source"] == str(chrome)
    assert record["path"] == str(app) and record["bundle_id"] == "dev.rove.browser"
    assert record["copied_at"] and record["signing"]["signature"] == "ad hoc"
    # The icon came from the package PNG through sips and iconutil.
    sips = [c for c in calls if c[0].endswith("sips")]
    assert len(sips) == 10 and all(c[4] == str(browser_app.ICON) for c in sips)
    assert any(c[0].endswith("iconutil") for c in calls)
    assert any(c[0].endswith("xattr") and "com.apple.quarantine" in c for c in calls)


def test_the_copy_is_signed_ad_hoc_deepest_first(state, chrome, tools):
    calls, _running = tools
    browser_app.install()
    signed = [c for c in calls if c[0].endswith("codesign") and c[1] == "--force"]
    assert all(c[2:4] == ["--sign", "-"] for c in signed)
    order = [c[-1] for c in signed]
    staging = str(state / "browser/Rove Browser.app.building")
    helper = f"{staging}/Contents/Frameworks/Google Chrome Framework.framework/Versions/{VERSION}/Helpers/Google Chrome Helper (Renderer).app"
    framework = (
        f"{staging}/Contents/Frameworks/Google Chrome Framework.framework/Versions/{VERSION}"
    )
    assert order.index(f"{helper}/Contents/MacOS/Google Chrome Helper (Renderer)") < order.index(
        helper
    )
    assert order.index(helper) < order.index(framework) < order.index(staging)
    assert order[-1] == staging
    # Nested code keeps its identifier; the app takes dev.rove.browser from its plist.
    assert all("--preserve-metadata=identifier" in c for c in signed[:-1])
    assert "--preserve-metadata=identifier" not in signed[-1]
    assert not any("runtime" in flag or "entitlements" in flag for c in signed for flag in c)
    verify = [c for c in calls if c[0].endswith("codesign") and c[1] == "--verify"]
    assert verify == [["/usr/bin/codesign", "--verify", "--deep", "--strict", staging]]


def test_install_is_idempotent_and_check_only_reports(state, chrome, tools):
    calls, _running = tools
    browser_app.install()
    copies = len([c for c in calls if c[0].endswith("ditto")])
    assert browser_app.install()["status"] == "current"
    assert browser_app.install(check=True)["status"] == "current"
    assert len([c for c in calls if c[0].endswith("ditto")]) == copies
    assert browser_app.refresh() is None


def test_a_chrome_update_rebuilds_the_copy_but_never_while_running(state, chrome, tools):
    _calls, running = tools
    browser_app.install()
    newer = {**plist(chrome), "CFBundleShortVersionString": "155.0.1.1"}
    (chrome / "Contents/Info.plist").write_bytes(plistlib.dumps(newer))
    report = browser_app.install(check=True)
    assert report["status"] == "outdated"
    assert report["installed"] == VERSION and report["chrome_version"] == "155.0.1.1"
    running["rove"] = True
    assert browser_app.refresh() is None
    assert browser_app.install()["status"] == "running"
    assert browser_app.installed()["chrome_version"] == VERSION
    status = browser_app.status({})
    assert status["version"] == VERSION and "155.0.1.1" in status["problem"]
    running["rove"] = False
    note = browser_app.refresh()
    assert "155.0.1.1" in note and VERSION in note
    assert browser_app.installed()["chrome_version"] == "155.0.1.1"
    app = state / "browser/Rove Browser.app"
    assert plist(app)["CFBundleShortVersionString"] == "155.0.1.1"
    assert not app.with_name("Rove Browser.app.building").exists()
    assert not app.with_name("Rove Browser.app.old").exists()
    assert browser_app.status({})["problem"] is None


def test_install_needs_chrome(state, chrome, tools, tmp_path):
    shutil.rmtree(chrome)
    with pytest.raises(RuntimeError, match="Google Chrome is not installed"):
        browser_app.install()
    assert browser_app.installed() is None


def test_launcher_uses_the_rove_browser(state, chrome, tools, launch):
    commands, lines = launch
    browser_app.install()
    port = live_browser.ChromeLauncher().ensure_running()
    (command,) = commands
    assert command[:6] == [
        "open",
        "-g",
        "-j",
        "-n",
        "-a",
        str(state / "browser/Rove Browser.app"),
    ]
    assert f"--user-data-dir={state / 'browser/recruiting-profile-rove'}" in command
    assert f"--remote-debugging-port={port}" in command
    assert "--use-mock-keychain" in command and "--no-startup-window" in command
    assert str(chrome) not in " ".join(command)
    assert (state / "browser/recruiting-profile-rove").is_dir()
    assert not (state / "browser/recruiting-profile").exists()
    session = json.loads((state / "browser/session.json").read_text())
    assert session["bundle"] == "dev.rove.browser" and session["name"] == "Rove Browser"
    assert session["version"] == VERSION and session["shared_chrome"] is False
    assert lines == []


def test_launch_rebuilds_a_stale_copy_first(state, chrome, tools, launch):
    commands, lines = launch
    browser_app.install()
    newer = {**plist(chrome), "CFBundleShortVersionString": "155.0.1.1"}
    (chrome / "Contents/Info.plist").write_bytes(plistlib.dumps(newer))
    live_browser.ChromeLauncher().ensure_running()
    assert lines and lines[0][1].startswith("rebuilt the Rove Browser from Google Chrome 155.0.1.1")
    session = json.loads((state / "browser/session.json").read_text())
    assert session["version"] == "155.0.1.1"
    assert len(commands) == 1


def test_launcher_refuses_to_fall_back_to_the_shared_chrome(state, chrome, tools, launch):
    commands, _lines = launch
    with pytest.raises(RuntimeError, match="rove browser install"):
        live_browser.ChromeLauncher().ensure_running()
    assert commands == []
    # A record whose app is gone counts as not installed.
    browser_app.record_file().parent.mkdir(parents=True)
    browser_app.record_file().write_text(
        json.dumps({"chrome_version": VERSION, "path": str(state / "missing.app")})
    )
    with pytest.raises(RuntimeError, match="rove browser install"):
        live_browser.ChromeLauncher().ensure_running()
    assert commands == []


def test_shared_chrome_only_by_name_and_with_a_warning(state, chrome, tools, launch, capsys):
    commands, lines = launch
    browser_app.install()
    # The setting must be the exact word; the old value "chrome" means the Rove Browser.
    (state / "config/workflow.json").write_text(json.dumps({"browser_app": "chrome"}))
    assert live_browser.ChromeLauncher().app()["bundle_id"] == "dev.rove.browser"
    assert live_browser.announce_app(workflow.config()) is None
    assert lines == []
    (state / "config/workflow.json").write_text(json.dumps({"browser_app": "shared-chrome"}))
    live_browser.ChromeLauncher().ensure_running()
    (command,) = commands
    assert command[5] == str(chrome)
    assert "--use-mock-keychain" not in command  # the old profile keeps its own encryption
    assert f"--user-data-dir={state / 'browser/recruiting-profile'}" in command
    session = json.loads((state / "browser/session.json").read_text())
    assert session["bundle"] == "com.google.Chrome" and session["shared_chrome"] is True
    warning = live_browser.announce_app(workflow.config())
    assert "shared Google Chrome" in warning and "rove browser install" in warning
    assert lines == [("browser", "warning · " + warning)]
    assert "warning:" in capsys.readouterr().err


class FakeBrowser:
    def __init__(self):
        self.pages, self.run, self.ensured = {}, None, 0
        self.launcher = live_browser.ChromeLauncher()

    def connected(self):
        return False

    def attach_if_running(self):
        return False

    def ensure(self):
        self.ensured += 1
        raise RuntimeError("launch requested")

    def close_run(self, run_id):
        return {"closed": run_id}


def test_status_names_the_app_without_launching(state, chrome, tools):
    browser = FakeBrowser()
    report = live_browser.handle_request(browser, {"action": "status"})
    assert report["browser_connected"] is False and report["browser_running"] is False
    assert report["app"]["problem"].endswith("run `rove browser install`")
    assert report["app"]["name"] is None and report["app"]["shared_chrome"] is False
    assert report["app"]["chrome_version"] == VERSION
    browser_app.install()
    report = live_browser.handle_request(browser, {"action": "status"})
    assert report["app"] == {
        "name": "Rove Browser",
        "path": str(state / "browser/Rove Browser.app"),
        "bundle_id": "dev.rove.browser",
        "version": VERSION,
        "shared_chrome": False,
        "chrome_version": VERSION,
        "problem": None,
        "running": False,
    }
    (state / "config/workflow.json").write_text(json.dumps({"browser_app": "shared-chrome"}))
    report = live_browser.handle_request(browser, {"action": "status"})
    assert report["app"]["name"] == "Google Chrome" and report["app"]["shared_chrome"] is True
    assert report["app"]["version"] == VERSION and report["app"]["bundle_id"] == "com.google.Chrome"
    assert browser.ensured == 0


def test_only_browser_actions_launch_the_browser(state):
    browser = FakeBrowser()
    assert live_browser.handle_request(browser, {"action": "close", "run_id": "r1"}) == {
        "closed": "r1"
    }
    assert browser.ensured == 0
    with pytest.raises(RuntimeError, match="launch requested"):
        live_browser.handle_request(browser, {"action": "open", "url": "https://example.com/j"})
    assert browser.ensured == 1
    with pytest.raises(PermissionError, match="Unsupported"):
        live_browser.handle_request(browser, {"action": "evaluate"})


def test_a_running_browser_of_another_app_is_retired(state, launch, monkeypatch):
    _commands, lines = launch
    app = state / "browser/Rove Browser.app"
    holders = {
        state / "browser/recruiting-profile": (
            4242,
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
    }
    killed = []
    monkeypatch.setattr(browser_app, "profile_holder", lambda profile: holders.get(profile))
    monkeypatch.setattr(live_browser.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(live_browser, "process_alive", lambda pid: False)
    stopped = live_browser.ChromeLauncher().retire_other_apps(str(app))
    assert [s["pid"] for s in stopped] == [4242]
    assert killed == [(4242, live_browser.signal.SIGTERM)]
    assert lines and "Google Chrome" in lines[0][1]
    # The configured app itself is left alone.
    holders[state / "browser/recruiting-profile-rove"] = (
        4343,
        str(app / "Contents/MacOS/Google Chrome"),
    )
    del holders[state / "browser/recruiting-profile"]
    killed.clear()
    assert live_browser.ChromeLauncher().retire_other_apps(str(app)) == []
    assert killed == []
