"""Which tests drive a Chromium: found when the suite is collected, enforced while it runs.

CI runs browser tests in their own job (`-m e2e`) and everything else without a browser
installed (`-m "not e2e"`). Nobody has to remember the marker:

- At collection, a test is marked `e2e` when its body, one of its fixtures, or a helper
  defined under tests/ that either one calls starts a browser: `sync_playwright()`,
  `.launch(...)`, `.launch_persistent_context(...)`, `RecruitingBrowser(...)`, or the
  offline bench fixture. Every test in an end-to-end scenario file
  (`test_end_to_end*.py`) is marked as well.
- While tests run, a browser launch from a test without the marker fails that test and
  names it, so a launch the reading above cannot see (one reached through `src/`) is
  caught the first time it happens. Mark such a test `@pytest.mark.e2e` by hand.

`tests/conftest.py` imports the two hooks below; pytest picks them up from there.
"""

import ast
import fnmatch
import functools
import inspect
import textwrap
import types
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
STARTERS = {"sync_playwright", "async_playwright", "RecruitingBrowser"}
LAUNCH_METHODS = {"launch", "launch_persistent_context"}
BENCH_RUNS = {"fixture", "run_fixture"}
SCENARIO_FILES = "test_end_to_end*.py"

_verdicts: dict = {}
_running: dict = {"item": None}


def _defined_in_tests(function) -> bool:
    try:
        return Path(function.__code__.co_filename).resolve().is_relative_to(TESTS)
    except (AttributeError, OSError, ValueError):
        return False


def _starts_a_browser(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in STARTERS
    if isinstance(func, ast.Attribute):
        if func.attr in STARTERS or func.attr in LAUNCH_METHODS:
            return True
        owner = func.value
        return func.attr in BENCH_RUNS and isinstance(owner, ast.Name) and owner.id == "benchmark"
    return False


def _resolve(func: ast.expr, namespace: dict):
    """The object a call refers to, when it is a plain name or `module.name`."""
    if isinstance(func, ast.Name):
        return namespace.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = namespace.get(func.value.id)
        if isinstance(owner, types.ModuleType):
            return getattr(owner, func.attr, None)
    return None


def launches_browser(function) -> bool:
    """Whether calling `function` (a test, fixture or test helper) can start a browser."""
    function = inspect.unwrap(function)
    if not inspect.isfunction(function) or not _defined_in_tests(function):
        return False
    if function in _verdicts:
        return _verdicts[function]
    _verdicts[function] = False  # a helper that calls itself is not a browser on its own
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, SyntaxError):
        return False
    verdict = any(
        _starts_a_browser(node) or launches_browser(_resolve(node.func, function.__globals__))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    )
    _verdicts[function] = verdict
    return verdict


def _fixture_functions(item):
    info = getattr(item, "_fixtureinfo", None)
    for definitions in getattr(info, "name2fixturedefs", {}).values():
        for definition in definitions:
            yield definition.func


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Mark browser tests before `-m` selects among them."""
    for item in items:
        if item.get_closest_marker("e2e"):
            continue
        if fnmatch.fnmatch(item.path.name, SCENARIO_FILES):
            item.add_marker(pytest.mark.e2e)
            continue
        functions = [getattr(item, "function", None), *_fixture_functions(item)]
        if any(launches_browser(f) for f in functions if f is not None):
            item.add_marker(pytest.mark.e2e)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _running["item"] = item
    try:
        return (yield)
    finally:
        _running["item"] = None


def _refuse_unmarked(launch):
    @functools.wraps(launch)
    def checked(self, *args, **kwargs):
        item = _running["item"]
        if item is not None and item.get_closest_marker("e2e") is None:
            raise RuntimeError(
                f"{item.nodeid} starts a Chromium but is not marked e2e. Add "
                "@pytest.mark.e2e to it so CI runs it in the browser job (tests/ci_marks.py)."
            )
        return launch(self, *args, **kwargs)

    return checked


def guard_browser_launches():
    from patchright.sync_api import BrowserType

    for name in LAUNCH_METHODS:
        method = getattr(BrowserType, name)
        if not getattr(method, "rove_guarded", False):
            guarded = _refuse_unmarked(method)
            guarded.rove_guarded = True
            setattr(BrowserType, name, guarded)


def pytest_configure(config):
    guard_browser_launches()
