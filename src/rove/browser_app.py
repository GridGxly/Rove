"""The recruiting browser's own application: a copy of the owner's Google Chrome.

macOS treats every running instance of an app bundle as the same app. While Rove's
recruiting browser was the shared /Applications/Google Chrome.app, a Dock click or a
link opened from another app could land in the recruiting profile. `rove browser
install` therefore copies the installed Chrome to `browser/Rove Browser.app` under the
state root and changes only its identity: bundle id `dev.rove.browser`, the name
"Rove Browser", Rove's icon, and no Keystone update keys, so Google's updater never
registers or touches the copy. The framework, helpers and every other key stay as they
are, so a page sees the same binary, engine and version as the owner's Chrome. Nothing is
downloaded. The copy is re-signed ad hoc and rebuilt whenever Chrome updates.
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .runtime import state_root, write_private

SHARED_CHROME = Path("/Applications/Google Chrome.app")
SHARED_BUNDLE_ID = "com.google.Chrome"
SHARED_SETTING = "shared-chrome"
APP_NAME = "Rove Browser.app"
DISPLAY_NAME = "Rove Browser"
BUNDLE_ID = "dev.rove.browser"
ICON = Path(__file__).parent / "assets/rove-app-icon.png"
# Keystone's registration parameters. Without KSUpdateURL and KSVersion, Chrome's
# KeystoneGlue never registers the copy. KSProductID and KSChannelID stay: they are what
# Chrome reads its channel from, and without them it reports an unknown channel.
UPDATE_KEYS = ("KSUpdateURL", "KSVersion", "KSBrandID")
# Chrome's own code signature carries entitlements that need Google's certificate (the
# application identifier, keychain groups, com.apple.developer.*) and the hardened runtime
# with library validation. The kernel kills an ad-hoc signed binary that keeps any of
# them, so the copy is signed with neither and nested code keeps only its identifier.
NESTED_PRESERVE = "--preserve-metadata=identifier"
MACHO_MAGIC = (
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
)
BUNDLE_SUFFIXES = {".app", ".bundle", ".xpc", ".appex", ".plugin"}
ICON_SIZES = (16, 32, 128, 256, 512)


def app_path() -> Path:
    return state_root() / "browser" / APP_NAME


def record_file() -> Path:
    return state_root() / "browser/rove-browser.json"


def now() -> str:
    return datetime.now(UTC).isoformat()


def tool(arguments: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """One place for the macOS tools the build runs; tests replace it."""
    return subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=timeout)


def bundle_info(app: Path) -> dict:
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def bundle_version(app: Path) -> str:
    return str(bundle_info(app).get("CFBundleShortVersionString", ""))


def installed() -> dict | None:
    """The record of the current copy, or None when it is missing or its app is gone."""
    try:
        record = json.loads(record_file().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    app = Path(str(record.get("path") or app_path()))
    if not (app / "Contents/MacOS").is_dir():
        return None
    return {**record, "path": str(app)}


def running(app: Path) -> bool:
    """Whether any process of this bundle (the browser or one of its helpers) is alive."""
    pattern = re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", str(app) + "/Contents/")
    return tool(["/usr/bin/pgrep", "-f", pattern], timeout=10).returncode == 0


def write_icns(png: Path, target: Path) -> None:
    """Every size macOS asks for, from the one 1024 px PNG, as an .icns next to Chrome's."""
    iconset = target.with_name("rove.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    try:
        for size in ICON_SIZES:
            for scale in (1, 2):
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                px = str(size * scale)
                result = tool(
                    ["/usr/bin/sips", "-z", px, px, str(png), "--out", str(iconset / name)]
                )
                if result.returncode != 0:
                    raise RuntimeError(f"sips failed: {result.stderr.strip()[:200]}")
        result = tool(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(target)])
        if result.returncode != 0 or not target.is_file():
            raise RuntimeError(f"iconutil failed: {result.stderr.strip()[:200]}")
    finally:
        shutil.rmtree(iconset, ignore_errors=True)


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) in MACHO_MAGIC
    except OSError:
        return False


def nested_code(app: Path) -> tuple[list[Path], list[Path]]:
    """Every Mach-O file and every nested bundle, deepest first; frameworks by version."""
    binaries, bundles = [], []
    for path in app.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file() and is_macho(path):
            binaries.append(path)
        elif path.is_dir() and path != app:
            if path.suffix == ".framework":
                versions = path / "Versions"
                if versions.is_dir():
                    bundles += [v for v in versions.iterdir() if v.is_dir() and not v.is_symlink()]
                else:
                    bundles.append(path)
            elif path.suffix in BUNDLE_SUFFIXES:
                bundles.append(path)

    def depth(path: Path) -> int:
        return len(path.parts)

    return sorted(binaries, key=depth, reverse=True), sorted(bundles, key=depth, reverse=True)


def sign(app: Path) -> dict:
    """Ad-hoc signatures, deepest first, then the app itself under its new identifier."""
    binaries, bundles = nested_code(app)
    for path in [*binaries, *bundles]:
        result = tool(["/usr/bin/codesign", "--force", "--sign", "-", NESTED_PRESERVE, str(path)])
        if result.returncode != 0:
            raise RuntimeError(f"codesign failed on {path.name}: {result.stderr.strip()[:200]}")
    result = tool(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])
    if result.returncode != 0:
        raise RuntimeError(f"codesign failed on the app: {result.stderr.strip()[:200]}")
    check = tool(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    if check.returncode != 0:
        raise RuntimeError(f"The signed copy does not verify: {check.stderr.strip()[:300]}")
    return {"binaries": len(binaries), "bundles": len(bundles), "signature": "ad hoc"}


def prune_framework_versions(app: Path, version: str) -> list[str]:
    """Chrome keeps the previous framework versions around after an update; the copy only
    needs the one its main binary loads."""
    removed = []
    for versions in (app / "Contents/Frameworks").glob("*.framework/Versions"):
        current = (versions / "Current").resolve().name if (versions / "Current").exists() else ""
        for folder in versions.iterdir():
            if folder.is_symlink() or not folder.is_dir() or folder.name in {version, current}:
                continue
            shutil.rmtree(folder)
            removed.append(folder.name)
    return removed


def rewrite_plist(app: Path) -> dict:
    """Identity only: id, names, the icon file, and no update keys. Everything else stays."""
    path = app / "Contents/Info.plist"
    raw = path.read_bytes()
    info = plistlib.loads(raw)
    removed = sorted(k for k in info if k in UPDATE_KEYS or k == "CFBundleIconName")
    for key in removed:
        del info[key]  # CFBundleIconName would make the asset catalog's icon win over ours
    info["CFBundleIdentifier"] = BUNDLE_ID
    info["CFBundleName"] = DISPLAY_NAME
    info["CFBundleDisplayName"] = DISPLAY_NAME
    info["CFBundleIconFile"] = "app.icns"
    fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
    with open(path, "wb") as handle:
        plistlib.dump(info, handle, fmt=fmt)
    return {"removed_keys": removed, "version": str(info.get("CFBundleShortVersionString", ""))}


def build(source: Path | None = None, target: Path | None = None, icon: Path = ICON) -> dict:
    """Copy `source` to `target` with Rove's identity. The caller checks nothing is running."""
    source = source or SHARED_CHROME
    target = target or app_path()
    version = bundle_version(source)
    if not version:
        raise RuntimeError(f"Google Chrome is not installed at {source}")
    if not icon.is_file():
        raise RuntimeError(f"The icon is missing: {icon}")
    staging = target.with_name(target.name + ".building")
    old = target.with_name(target.name + ".old")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        copied = tool(["/usr/bin/ditto", str(source), str(staging)])
        if copied.returncode != 0 or not (staging / "Contents/Info.plist").is_file():
            raise RuntimeError(f"ditto failed: {copied.stderr.strip()[:200]}")
        pruned = prune_framework_versions(staging, version)
        plist = rewrite_plist(staging)
        write_icns(icon, staging / "Contents/Resources/app.icns")
        tool(["/usr/bin/xattr", "-d", "com.apple.application-instance", str(staging)], timeout=60)
        tool(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(staging)], timeout=120)
        signing = sign(staging)
        if target.exists():
            target.rename(old)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    target.touch()  # a new modification time makes Launch Services look again
    record = {
        "chrome_version": version,
        "source": str(source),
        "path": str(target),
        "bundle_id": BUNDLE_ID,
        "name": DISPLAY_NAME,
        "copied_at": now(),
        "removed_keys": plist["removed_keys"],
        "pruned_framework_versions": pruned,
        "signing": signing,
    }
    write_private(record_file(), record)
    return record


def install(check: bool = False) -> dict:
    """Build or refresh the Rove Browser from the installed Chrome, or report what is there.

    Idempotent: a copy of the installed Chrome's version is left alone, `check` never
    builds, and nothing is rebuilt while the Rove Browser is running.
    """
    available = bundle_version(SHARED_CHROME)
    if not available:
        raise RuntimeError(f"Google Chrome is not installed at {SHARED_CHROME}")
    current = installed()
    report = {
        "source": str(SHARED_CHROME),
        "chrome_version": available,
        "installed": current["chrome_version"] if current else None,
        "path": str(app_path()),
        "bundle_id": BUNDLE_ID,
        "name": DISPLAY_NAME,
    }
    if current and current["chrome_version"] == available:
        return {**report, "status": "current"}
    report["status"] = "missing" if current is None else "outdated"
    if check:
        return report
    if current and running(Path(current["path"])):
        return {
            **report,
            "status": "running",
            "problem": "The Rove Browser is running; it is rebuilt after it quits",
        }
    record = build()
    return {
        **report,
        "status": "installed" if current is None else "rebuilt",
        "installed": record["chrome_version"],
        "removed_keys": record["removed_keys"],
        "pruned_framework_versions": record["pruned_framework_versions"],
        "signing": record["signing"],
    }


def refresh() -> str | None:
    """Rebuild an existing copy when Chrome has updated and the Rove Browser is not running.

    Returns one line for the system log when a rebuild happened. A missing copy is not
    built here: that is the owner's explicit `rove browser install`.
    """
    current = installed()
    if current is None:
        return None
    available = bundle_version(SHARED_CHROME)
    if not available or available == current["chrome_version"]:
        return None
    if running(Path(current["path"])):
        return None
    record = build()
    return (
        f"rebuilt the Rove Browser from Google Chrome {record['chrome_version']} "
        f"(the copy was {current['chrome_version']})"
    )


def chosen(settings: dict) -> dict:
    """The app the daemon launches: the Rove Browser copy, or the shared Google Chrome only
    when the private config says `browser_app: "shared-chrome"`.

    There is no fallback from one to the other.
    """
    if settings.get("browser_app") == SHARED_SETTING:
        if not SHARED_CHROME.is_dir():
            raise RuntimeError(
                f"browser_app is {SHARED_SETTING} but {SHARED_CHROME} is not installed"
            )
        return {
            "name": "Google Chrome",
            "path": str(SHARED_CHROME),
            "bundle_id": SHARED_BUNDLE_ID,
            "version": bundle_version(SHARED_CHROME),
            "shared_chrome": True,
        }
    current = installed()
    if current is None:
        raise RuntimeError(
            "The Rove Browser is not installed under the state root; run `rove browser install`"
        )
    return {
        "name": DISPLAY_NAME,
        "path": current["path"],
        "bundle_id": BUNDLE_ID,
        "version": current["chrome_version"],
        "shared_chrome": False,
    }


def status(settings: dict) -> dict:
    """Like `chosen`, for status output: a problem is reported, never raised."""
    try:
        app = {**chosen(settings), "problem": None}
    except RuntimeError as error:
        return {
            "name": None,
            "path": None,
            "bundle_id": None,
            "version": None,
            "shared_chrome": settings.get("browser_app") == SHARED_SETTING,
            "chrome_version": bundle_version(SHARED_CHROME) or None,
            "problem": str(error),
        }
    app["chrome_version"] = bundle_version(SHARED_CHROME) or None
    if not app["shared_chrome"] and app["chrome_version"] not in (None, app["version"]):
        app["problem"] = (
            f"Google Chrome is {app['chrome_version']} and the Rove Browser is "
            f"{app['version']}; it is rebuilt when it is not running"
        )
    return app


def profile_dir(shared_chrome: bool) -> Path:
    """The Rove Browser gets its own profile: its cookies are encrypted with the mock
    keychain's key, which the old profile's cookies were not. The shared Chrome keeps the
    old profile, untouched."""
    name = "recruiting-profile" if shared_chrome else "recruiting-profile-rove"
    return state_root() / "browser" / name


def devtools_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as reply:
            return reply.status == 200
    except (OSError, ValueError):
        return False


def session_info() -> dict:
    path = state_root() / "browser/session.json"
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def profile_holder(profile: Path) -> tuple[int, str] | None:
    """(pid, executable) of the browser holding this profile, from Chrome's SingletonLock."""
    try:
        target = os.readlink(profile / "SingletonLock")
    except OSError:
        return None
    _host, _, pid = target.rpartition("-")
    if not pid.isdigit():
        return None
    try:
        executable = subprocess.check_output(
            ["/bin/ps", "-o", "comm=", "-p", pid], timeout=5, text=True
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return (int(pid), executable) if executable else None
