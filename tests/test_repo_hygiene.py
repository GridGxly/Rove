"""What a public repository must never hold, links that must resolve, documented
configuration, and the CI machinery itself.

The repository is public (AGENTS.md). These checks read every tracked file, so a real
Discord ID, a personal address or a path from someone's Mac fails CI wherever it lands,
not only when the staged-file guard happens to run.
"""

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
from ci_marks import _refuse_unmarked, launches_browser

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "rove"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tracked() -> list[Path]:
    names = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    return [ROOT / name.decode() for name in names if name and (ROOT / name.decode()).is_file()]


def text_files() -> dict[Path, str]:
    found = {}
    for path in tracked():
        try:
            found[path] = path.read_text()
        except UnicodeDecodeError:
            continue  # images and other binary files
    return found


def where(path: Path, text: str, match: re.Match) -> str:
    return f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, match.start()) + 1}"


# ---------------------------------------------------------------------------------------
# Secrets and personal data
# ---------------------------------------------------------------------------------------


def test_no_secret_or_private_artifact_is_tracked():
    """The staged-file guard's patterns, over the whole tree."""
    guard = load_script("check_staged")
    blocked = [
        str(path.relative_to(ROOT))
        for path in tracked()
        if guard.blocked(path.relative_to(ROOT).as_posix(), path.read_bytes())
    ]
    assert blocked == [], "review these tracked files for private data"


# A snowflake made of one repeated digit or the run 1234567890... is a placeholder.
def synthetic_snowflake(number: str) -> bool:
    return len(set(number)) == 1 or number == ("1234567890" * 2)[: len(number)]


DISCORD_ID = re.compile(r"(?<![\w.])[0-9]{17,19}(?![\w.])")


def test_no_discord_id_unless_plainly_synthetic():
    found = [
        f"{where(path, text, m)} {m.group()}"
        for path, text in text_files().items()
        for m in DISCORD_ID.finditer(text)
        if not synthetic_snowflake(m.group())
    ]
    assert found == [], (
        "17-19 digit numbers look like real Discord IDs; use 123456789012345678 or one "
        "repeated digit in examples"
    )


EMAIL = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
SYNTHETIC_DOMAINS = re.compile(
    r"(^|\.)(example\.(com|org|net)|example|invalid|test|localhost)$", re.IGNORECASE
)
# Reviewed addresses that are not synthetic domains: placeholders and role accounts.
REVIEWED_EMAILS = {
    "someone@gmail.com": "a placeholder sender in mail tests, no person",
    "friend@gmail.com": "a placeholder sender in mail tests, no person",
    "no-reply@us.greenhouse-mail.io": "Greenhouse's role sender, as recruiting mail shows it",
    "no-reply@ashbyhq.com": "Ashby's role sender, as recruiting mail shows it",
    "no-reply@lever.co": "a job board's role sender in a mail test, no person",
    "hr@evil.pages.dev": "a lookalike sender the mail tests must refuse",
}


def test_no_email_address_outside_synthetic_domains():
    found = []
    for path, text in text_files().items():
        for m in EMAIL.finditer(text):
            if text[max(m.start() - 3, 0) : m.start()] == "://":
                continue  # a URL's user part, not an address
            if SYNTHETIC_DOMAINS.search(m.group(1)) or m.group().lower() in REVIEWED_EMAILS:
                continue
            found.append(f"{where(path, text, m)} {m.group()}")
    assert found == [], "use an example.com or .invalid address, or review and list it"


def test_no_absolute_path_from_a_machine():
    pattern = re.compile(r"/(?:Users|home)/(?!example\b|username\b|runner\b|user\b)[^/\s\"'`]+/")
    found = [
        where(path, text, m) for path, text in text_files().items() for m in pattern.finditer(text)
    ]
    assert found == [], "write ~/... or a placeholder such as /Users/example/..."


# ---------------------------------------------------------------------------------------
# Links between documents
# ---------------------------------------------------------------------------------------

LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"<[^>]+>", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    found = set()
    in_code = False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if in_code or not line.startswith("#"):
            continue
        base = slug(line.lstrip("#"))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if not count else f"{base}-{count}")
    return found


def markdown_files() -> list[Path]:
    return [p for p in tracked() if p.suffix == ".md"]


def test_relative_links_in_the_docs_resolve():
    broken = []
    for path in markdown_files():
        text = re.sub(r"```.*?```", "", path.read_text(), flags=re.DOTALL)
        for m in LINK.finditer(text):
            target = m.group(1)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("//"):
                continue  # http(s), mailto and other absolute links
            file_part, _, anchor = target.partition("#")
            linked = (path.parent / file_part).resolve() if file_part else path
            if not linked.exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target} (no such file)")
            elif anchor and linked.suffix == ".md" and anchor not in anchors(linked):
                broken.append(f"{path.relative_to(ROOT)} -> {target} (no such heading)")
    assert broken == []


# ---------------------------------------------------------------------------------------
# Every key the code reads from config/workflow.json is documented
# ---------------------------------------------------------------------------------------

# Read by the code but missing from the workflow.json table in docs/requirements.md when
# this check was added. The list only shrinks: document a key, then remove it here.
UNDOCUMENTED_TODAY: set[str] = set()


class ConfigReads:
    """Keys read from `workflow.config()`, found by reading the source.

    A value from `workflow.config()` (or `config()` inside workflow.py) held in a local
    name, passed on to a parameter of another function in the package, or read through a
    helper that takes the key as an argument (`number(settings, "key", 1)`), counts; a key
    read with `.get("k")`, `["k"]`, `.setdefault("k")`, `.pop("k")` or `"k" in` counts.
    """

    def __init__(self, root: Path):
        self.modules = {}
        for path in sorted(root.rglob("*.py")):
            name = ".".join(path.relative_to(root.parent).with_suffix("").parts)
            self.modules[name.removesuffix(".__init__")] = ast.parse(path.read_text())
        self.functions = {}
        for module, tree in self.modules.items():
            for node in tree.body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    self.functions[(module, node.name)] = node
        self.held: dict = {key: set() for key in self.functions}  # parameters given config
        # function -> {(parameter naming the key, parameter holding the config or None)}
        self.keyed: dict = {key: set() for key in self.functions}
        self.keys: dict[str, set[str]] = {}

    def names(self, module: str) -> dict:
        """Local name -> ("module", target) or ("function", (module, name))."""
        found = {}
        package = module.rsplit(".", 1)[0] if module.count(".") else module
        for node in ast.walk(self.modules[module]):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                base = package if not node.module else f"{package}.{node.module}"
                for alias in node.names:
                    local = alias.asname or alias.name
                    if f"{base}.{alias.name}" in self.modules:
                        found[local] = ("module", f"{base}.{alias.name}")
                    else:
                        found[local] = ("function", (base, alias.name))
        for owner, name in self.functions:
            if owner == module:
                found.setdefault(name, ("function", (module, name)))
        return found

    def is_config_call(self, node, names) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            kind = names.get(func.value.id)
            return func.attr == "config" and kind == ("module", "rove.workflow")
        return isinstance(func, ast.Name) and names.get(func.id) == (
            "function",
            ("rove.workflow", "config"),
        )

    def callee(self, func, names):
        if isinstance(func, ast.Name):
            kind = names.get(func.id)
            return kind[1] if kind and kind[0] == "function" else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            kind = names.get(func.value.id)
            if kind and kind[0] == "module":
                return (kind[1], func.attr)
        return None

    @staticmethod
    def lookups(node):
        """(dict expression, key expression) for every keyed read under `node`."""
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in {"get", "setdefault", "pop"}
                and inner.args
            ):
                yield inner.func.value, inner.args[0]
            elif isinstance(inner, ast.Subscript):
                yield inner.value, inner.slice
            elif isinstance(inner, ast.Compare) and isinstance(inner.ops[0], ast.In | ast.NotIn):
                yield inner.comparators[0], inner.left

    def found(self, key_node, module: str):
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            self.keys.setdefault(key_node.value, set()).add(module)

    def scan(self, key, names) -> bool:
        """One pass over one function; True when it taught us something new."""
        module, node = key[0], self.functions[key]
        params = [a.arg for a in [*node.args.posonlyargs, *node.args.args]]
        local = {
            target.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Assign) and self.is_config_call(inner.value, names)
            for target in inner.targets
            if isinstance(target, ast.Name)
        }
        holders = local | {params[i] for i in self.held[key] if i < len(params)}

        def holds(expr) -> bool:
            named = isinstance(expr, ast.Name) and expr.id in holders
            return named or self.is_config_call(expr, names)

        before = (sum(map(len, self.held.values())), sum(map(len, self.keyed.values())))
        for subject, key_node in self.lookups(node):
            if not holds(subject):
                continue
            self.found(key_node, module)
            if isinstance(key_node, ast.Name) and key_node.id in params:
                given = isinstance(subject, ast.Name) and subject.id not in local
                config_index = params.index(subject.id) if given else None
                self.keyed[key].add((params.index(key_node.id), config_index))
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            target = self.callee(call.func, names)
            if target in self.functions:
                self.follow(call, target, holds, module)
        return before != (sum(map(len, self.held.values())), sum(map(len, self.keyed.values())))

    def follow(self, call: ast.Call, target, holds, module: str):
        """Config passed into another function, and keys read through a keyed helper."""
        self.held[target] |= {i for i, argument in enumerate(call.args) if holds(argument)}
        for key_index, config_index in self.keyed[target]:
            passed = config_index is None or (
                config_index < len(call.args) and holds(call.args[config_index])
            )
            if passed and key_index < len(call.args):  # else the helper read another dict
                self.found(call.args[key_index], module)

    def run(self) -> dict[str, set[str]]:
        names = {module: self.names(module) for module in self.modules}
        changed = True
        while changed:  # until a whole pass learns nothing new
            changed = False
            for key in self.functions:
                changed = self.scan(key, names[key[0]]) or changed
        return self.keys


def documented_workflow_keys() -> str:
    text = (ROOT / "docs" / "requirements.md").read_text()
    section = text.split("### `config/workflow.json`", 1)[1]
    return section.split("\n### ", 1)[0]


def test_every_workflow_config_key_the_code_reads_is_documented():
    keys = ConfigReads(SOURCE).run()
    documented = documented_workflow_keys()
    missing = {key for key in keys if f"`{key}`" not in documented}
    assert missing <= UNDOCUMENTED_TODAY, (
        f"document these config/workflow.json keys in docs/requirements.md: "
        f"{sorted(missing - UNDOCUMENTED_TODAY)}"
    )
    stale = UNDOCUMENTED_TODAY - missing
    assert not stale, f"now documented or no longer read; drop from UNDOCUMENTED_TODAY: {stale}"


def test_the_config_reader_sees_keys_held_passed_on_and_read_through_helpers():
    keys = ConfigReads(SOURCE).run()
    # held in a local, passed to a parameter, read through a keyed helper, read by key name
    assert {"submit_adapters", "auto_submit", "max_waiting_applications"} <= set(keys)
    assert {"memory_channel_id", "max_submissions_per_day"} <= set(keys)
    # mail.json and feed.json have their own config(); their keys are not workflow keys
    assert "authserv_ids" not in keys and "lookback_days" not in keys


# ---------------------------------------------------------------------------------------
# The CI machinery: browser marking, the ratchet, the noqa reasons
# ---------------------------------------------------------------------------------------


def opens_a_page():
    with sync_playwright() as playwright:  # noqa: F821 -- only read, never run
        playwright.chromium.launch()


def calls_a_helper_that_opens_a_page():
    opens_a_page()


def reads_html_only():
    return "<p>no browser here</p>".upper()


def test_browser_tests_are_found_through_helpers_and_nothing_else_is():
    assert launches_browser(opens_a_page)
    assert launches_browser(calls_a_helper_that_opens_a_page)
    assert not launches_browser(reads_html_only)


def test_an_unmarked_test_that_starts_a_browser_is_stopped(monkeypatch, request):
    import ci_marks

    started = []
    launch = _refuse_unmarked(lambda self, **kwargs: started.append(kwargs) or "browser")
    monkeypatch.setitem(ci_marks._running, "item", request.node)
    with pytest.raises(RuntimeError, match="not marked e2e"):
        launch(object(), headless=True)
    request.node.add_marker(pytest.mark.e2e)
    assert launch(object(), headless=True) == "browser" and started == [{"headless": True}]


def test_the_ratchet_fails_above_and_below_the_ceiling_and_only_lowers_on_update():
    ratchet = load_script("ratchet")
    stored = {"E501": {"a.py": 2, "b.py": 1}}
    now = ratchet.tally([{"rule": "E501", "path": "a.py"}] * 3 + [{"rule": "C901", "path": "c.py"}])
    above, below = ratchet.compare(now, stored)
    assert above == [("C901", "c.py", 1, 0), ("E501", "a.py", 3, 2)]
    assert below == [("E501", "b.py", 0, 1)]
    assert ratchet.tally_min(now, stored) == {"E501": {"a.py": 2}}


def test_a_suppressed_blind_except_must_say_why(tmp_path, monkeypatch):
    ratchet = load_script("ratchet")
    (tmp_path / "a.py").write_text(
        "try:\n    pass\nexcept Exception:  # noqa: BLE001\n    pass\n"
        "try:\n    pass\nexcept Exception:  # noqa: BLE001 -- the log line never stops work\n"
        "    pass\n"
        "x = eval('1')  # noqa: S307\n"
        "import os  # noqa: F401\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True)
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    assert ratchet.missing_reasons() == [
        "a.py:3: `noqa: BLE001` needs `-- reason`",
        "a.py:9: `noqa: S307` needs `-- reason`",
    ]


def test_smoke_tests_run_only_when_the_marker_expression_names_them():
    def collected(expression: str) -> list[str]:
        shown = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly"]
            + ["-p", "no:cacheprovider", "-m", expression, "tests/test_smoke.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return [line for line in shown.splitlines() if "::" in line]

    smoke = "test_rove_help_and_every_subcommand_help"
    assert not any(smoke in line for line in collected("not e2e and not performance"))
    assert any(smoke in line for line in collected("smoke"))
    assert any(smoke in line for line in collected("smoke or not smoke"))


def test_every_ci_job_calls_a_command_that_exists():
    """Scripts and pytest markers named in the workflow are the ones in this repo."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for script in re.findall(r"scripts/([\w-]+\.py)", workflow):
        assert (ROOT / "scripts" / script).exists(), script
    configured = (ROOT / "pyproject.toml").read_text()
    for marker in set(re.findall(r'-m "?(?:not )?(\w+)', workflow)):
        assert f'"{marker}:' in configured, f"marker {marker} is not registered"
