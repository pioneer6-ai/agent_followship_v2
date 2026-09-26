"""
Guard the documentation against drifting away from the code.

Every test here pins a class of defect that was actually found by hand:

* ``.env.example`` advertised ``SMTP_PRESET``, which no code read, so a clinic
  following the docs got no effect at all.
* ``.env.example`` declared seven variables twice, and a later empty declaration
  blanks an earlier populated one under ``load_env_file(override=True)``.
* The README told users to run ``python app.py`` -- a file that does not exist --
  and to open port 5000 while the dashboard listens on 8080.
* Six routes, including the booking flow, were missing from the API reference.
* ``test_data/README.md`` made the same missing-``app.py`` mistake, and its file
  inventory omitted three files that are actually in the directory. It was unseen
  because only ``DOC_FILES`` were checked; the sweep below covers every markdown
  file in the repository.
* ``proposal/deployment_evidence.md`` paired the symbol ``def send_sms`` with the
  line of ``send_sms_message``, and ``proposal/proposal.md`` cited
  ``agent/orchestrator.py:383`` where the named method is at 153.
* The line numbers in evidence report 2 were wrong for the same reason: the
  capture script resolved locators with ``needle in line``, so ``def send_sms``
  matched inside ``def send_sms_message``. Generated citations need a test on the
  generator, not only on the generated file.

These are cheap static checks over the real files, so they cannot silently rot.
"""

import os
import re
import subprocess
import sys
from typing import Dict, List, Set, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOC_FILES = ["README.md", "QUICKSTART.md", "ARCHITECTURE.md"]


def _all_markdown_files() -> List[str]:
    """
    Every markdown file tracked by git, relative to the project root.

    Returns:
        Sorted list of paths, or ``DOC_FILES`` if git is unavailable.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.md"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return list(DOC_FILES)
    found = sorted(line for line in out.split() if line.endswith(".md"))
    return found or list(DOC_FILES)


# Variables a person may legitimately see in these docs that this project does
# not read itself. ``SSL_CERT_FILE`` is read by ``tools.tls`` (so it is *not*
# here); these are the environment variables of other software.
FOREIGN_ENV_VARS: Set[str] = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_PROFILE",
    "AWS_SESSION_TOKEN",
    "CLINIC_SETUP_FILE",
    "PYTHONPATH",
    "VIRTUAL_ENV",
}


def _read(relative: str) -> str:
    with open(os.path.join(PROJECT_ROOT, relative), "r", encoding="utf-8") as fh:
        return fh.read()


def _iter_source_files() -> List[str]:
    """Every Python file belonging to the project, tests excluded."""
    roots = ["agent", "core", "tools", "utils", "web", "scripts"]
    found: List[str] = []
    for root in roots:
        base = os.path.join(PROJECT_ROOT, root)
        for dirpath, _dirnames, filenames in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for name in filenames:
                if name.endswith(".py"):
                    found.append(os.path.join(dirpath, name))
    for top in ("hospital_setup.py", "demo.py"):
        path = os.path.join(PROJECT_ROOT, top)
        if os.path.exists(path):
            found.append(path)
    return found


def _all_upper_literals() -> Set[str]:
    """Upper-case string literals appearing anywhere in the project's code."""
    literals: Set[str] = set()
    for path in _iter_source_files():
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        literals |= set(re.findall(r"""["']([A-Z][A-Z0-9_]{3,})["']""", text))
    return literals


def _declared_env_vars() -> Set[str]:
    """``NAME=`` declarations in ``.env.example``."""
    text = _read(".env.example")
    return set(re.findall(r"^\s*([A-Z][A-Z0-9_]{3,})\s*=", text, re.M))


def _duplicate_env_vars() -> Dict[str, int]:
    text = _read(".env.example")
    counts: Dict[str, int] = {}
    for name in re.findall(r"^\s*([A-Z][A-Z0-9_]{3,})\s*=", text, re.M):
        counts[name] = counts.get(name, 0) + 1
    return {name: n for name, n in counts.items() if n > 1}


def _code_routes() -> Set[Tuple[str, str]]:
    """``(method, path)`` for every Flask route, normalised to ``{param}``."""
    routes: Set[Tuple[str, str]] = set()
    
    # Scan web/app.py for @app.route
    text = _read("web/app.py")
    app_pattern = re.compile(
        r"@app\.route\(\s*[\"']([^\"']+)[\"']\s*(?:,\s*methods=\[([^\]]*)\])?\s*\)"
    )
    for path, methods in app_pattern.findall(text):
        path = re.sub(r"<[^>]+>", "{param}", path)
        if methods:
            for method in re.findall(r"[\"']([A-Z]+)[\"']", methods):
                routes.add((method, path))
        else:
            routes.add(("GET", path))
    
    # Scan Blueprint files: calendar_routes.py and staff_routes.py
    blueprint_files = [
        ("web/calendar_routes.py", "/api/calendar"),  # calendar_bp with url_prefix
        ("web/staff_routes.py", "/api/staff"),        # staff_bp with url_prefix
    ]
    
    bp_pattern = re.compile(
        r"@(?:calendar_bp|staff_bp)\.route\(\s*[\"']([^\"']+)[\"']\s*(?:,\s*methods=\[([^\]]*)\])?\s*\)"
    )
    
    for file_path, url_prefix in blueprint_files:
        try:
            text = _read(file_path)
            for path, methods in bp_pattern.findall(text):
                # Apply URL prefix and normalize parameters
                full_path = url_prefix + path
                full_path = re.sub(r"<[^>]+>", "{param}", full_path)
                
                if methods:
                    for method in re.findall(r"[\"']([A-Z]+)[\"']", methods):
                        routes.add((method, full_path))
                else:
                    routes.add(("GET", full_path))
        except FileNotFoundError:
            # Blueprint file doesn't exist, skip it
            pass
    
    return routes


def _documented_routes() -> Set[Tuple[str, str]]:
    """``(method, path)`` read from the API endpoint table in the README."""
    text = _read("README.md")
    routes: Set[Tuple[str, str]] = set()
    pattern = re.compile(
        r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`", re.M
    )
    for method, path in pattern.findall(text):
        path = re.sub(r"\{[^}]+\}", "{param}", path)
        routes.add((method, path))
    return routes


class TestEnvironmentTemplate:
    """The template must document what the code reads, and nothing more."""

    def test_the_template_is_not_empty(self):
        assert len(_declared_env_vars()) > 30

    def test_every_documented_variable_is_actually_read(self):
        """
        A variable in the template that no code mentions sends a clinic on a
        wild goose chase -- this is the ``SMTP_PRESET`` defect.
        """
        referenced = _all_upper_literals()
        dead = sorted(
            name
            for name in _declared_env_vars() - FOREIGN_ENV_VARS
            if name not in referenced
        )
        assert dead == [], f"documented but never read by any code: {dead}"

    def test_no_variable_is_declared_twice(self):
        """
        A later empty declaration silently blanks an earlier populated one when
        the file is loaded with ``override=True``.
        """
        assert _duplicate_env_vars() == {}

    def test_the_urgency_thresholds_are_documented(self):
        """Clinic-facing policy knobs must be visible without reading source."""
        declared = _declared_env_vars()
        assert "AGENT_HIGH_URGENCY_THRESHOLD_DAYS" in declared
        assert "AGENT_CRITICAL_URGENCY_THRESHOLD_DAYS" in declared

    def test_the_documented_policy_defaults_match_the_code(self):
        from core.config import ClinicPolicyConfig

        defaults = ClinicPolicyConfig()
        text = _read(".env.example")

        def documented(name: str) -> int:
            match = re.search(rf"^{name}=(\d+)", text, re.M)
            assert match, f"{name} is not declared with a numeric default"
            return int(match.group(1))

        assert documented("AGENT_HIGH_URGENCY_THRESHOLD_DAYS") == (
            defaults.high_urgency_threshold_days
        )
        assert documented("AGENT_CRITICAL_URGENCY_THRESHOLD_DAYS") == (
            defaults.critical_urgency_threshold_days
        )
        assert documented("AGENT_MAX_REMINDERS_BEFORE_ESCALATION") == (
            defaults.max_reminders_before_escalation
        )
        assert documented("AGENT_REMINDER_INTERVAL_DAYS") == (
            defaults.reminder_interval_days
        )

    def test_the_documented_aws_defaults_match_the_code(self):
        from tools.config import MessagingConfig

        config = MessagingConfig.from_env(env={})
        text = _read(".env.example")
        assert config.aws_sms_allowlist == [
            line.split("=", 1)[1]
            for line in text.splitlines()
            if line.startswith("AWS_SMS_ALLOWED_NUMBERS=")
        ]
        assert config.aws_email_allowlist == [
            line.split("=", 1)[1]
            for line in text.splitlines()
            if line.startswith("AWS_EMAIL_ALLOWED_ADDRESSES=")
        ]


class TestDocumentedEndpoints:
    """The API reference must match the routes the app actually serves."""

    def test_the_readme_documents_some_endpoints(self):
        assert len(_documented_routes()) >= 10

    def test_documented_endpoints_all_exist(self):
        invented = sorted(_documented_routes() - _code_routes())
        assert invented == [], f"documented but not implemented: {invented}"

    def test_every_route_is_documented(self):
        undocumented = sorted(_code_routes() - _documented_routes())
        assert undocumented == [], f"implemented but undocumented: {undocumented}"


class TestEntryPointsAndPorts:
    """Users copy these commands verbatim, so they must be runnable."""

    def test_the_docs_never_reference_a_missing_top_level_app(self):
        for name in DOC_FILES:
            text = _read(name)
            offenders = re.findall(r"python\s+app\.py", text)
            assert offenders == [], f"{name} tells users to run a missing file"

    def test_the_dashboard_file_exists_where_the_docs_say(self):
        assert os.path.exists(os.path.join(PROJECT_ROOT, "web", "app.py"))
        assert not os.path.exists(os.path.join(PROJECT_ROOT, "app.py"))

    def test_the_documented_port_matches_the_application(self):
        text = _read("web/app.py")
        match = re.search(r"^\s*port\s*=\s*(\d+)", text, re.M)
        assert match, "could not find the dashboard port"
        expected = match.group(1)
        for name in DOC_FILES:
            doc = _read(name)
            if "localhost:" not in doc:
                continue
            ports = set(re.findall(r"localhost:(\d+)", doc))
            assert ports <= {expected}, f"{name} names a stale port: {ports}"

    def test_no_doc_promises_an_unsupported_port_flag(self):
        """web/app.py has no argument parser, so --port is silently ignored."""
        app_source = _read("web/app.py")
        assert "argparse" not in app_source
        for name in DOC_FILES:
            doc = _read(name)
            assert not re.search(r"app\.py\s+--port", doc), (
                f"{name} suggests a --port flag the dashboard ignores"
            )


class TestMarkdownHygiene:
    """Cheap structural checks so the docs stay renderable."""

    def test_code_fences_are_balanced(self):
        for name in _all_markdown_files():
            fences = _read(name).count("\n```")
            assert fences % 2 == 0, f"{name} has an odd number of code fences"

    def test_internal_anchors_resolve(self):
        """Every ``#section`` link must have a matching heading."""
        for name in _all_markdown_files():
            text = _read(name)
            slugs = set()
            for line in text.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip().lower()
                    title = re.sub(r"[^\w\s-]", "", title)
                    slugs.add(re.sub(r"\s+", "-", title))
            broken = sorted(
                anchor
                for anchor in re.findall(r"\]\(#([^)]+)\)", text)
                if anchor not in slugs
            )
            assert broken == [], f"{name} has broken anchors: {broken}"


class TestEveryMarkdownFile:
    """
    The checks above stop at ``DOC_FILES``; these sweep the whole repository.

    A stale pointer in a secondary document is just as wrong as one in the
    README, and the files that rot fastest are the ones no test reads. These are
    deliberately weaker than the ``DOC_FILES`` checks -- they assert what can be
    asserted about *any* document, whatever its audience.
    """

    def test_every_documented_python_command_names_a_real_file(self):
        """``python path/to/thing.py`` must name a file that exists."""
        for name in _all_markdown_files():
            for arg in re.findall(r"python[\w.]*\s+(?!-m\b)([^\s`'\"]+\.py)", _read(name)):
                path = os.path.join(PROJECT_ROOT, arg)
                assert os.path.exists(path), (
                    f"{name} tells users to run `python {arg}`, which does not exist"
                )

    def test_documented_file_line_citations_point_inside_the_file(self):
        """A ``path.py:123`` citation must be a real file and a real line."""
        for name in _all_markdown_files():
            text = _read(name)
            for target, line in re.findall(r"`([\w][\w/.\-]*\.py):(\d+)`", text):
                path = os.path.join(PROJECT_ROOT, target)
                assert os.path.exists(path), f"{name} cites a missing file: {target}"
                with open(path, "r", encoding="utf-8") as fh:
                    total = len(fh.read().splitlines())
                assert int(line) <= total, (
                    f"{name} cites {target}:{line}, but that file has only {total} lines"
                )

    def test_documented_symbol_and_line_agree(self):
        """
        A table row pairing ``def name`` with a line must land on that definition.

        This is the check that would have caught ``def send_sms`` being cited at
        the line of ``send_sms_message``: both are in the same file and both lines
        exist, so only comparing the symbol to the line finds it.
        """
        row = re.compile(r"^\|\s*`([^`]+\.py)`\s*\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|", re.M)
        for name in _all_markdown_files():
            for target, symbol, line in row.findall(_read(name)):
                definition = re.match(r"(def|class)\s+(\w+)", symbol)
                if not definition:
                    continue
                path = os.path.join(PROJECT_ROOT, target)
                assert os.path.exists(path), f"{name} cites a missing file: {target}"
                with open(path, "r", encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                assert int(line) <= len(lines), f"{name} cites {target}:{line} out of bounds"
                actual = lines[int(line) - 1]
                assert f"{definition.group(1)} {definition.group(2)}(" in actual, (
                    f"{name} cites {target}:{line} as `{symbol}`, but that line is "
                    f"`{actual.strip()}`"
                )


def _evidence_capture_script():
    """
    Import ``proposal/scripts/capture_aws_config_evidence.py`` by path.

    It is not on ``sys.path`` -- ``proposal/scripts`` is a build-time directory,
    not a package the application imports.

    Returns:
        The imported module.
    """
    import importlib.util

    path = os.path.join(
        PROJECT_ROOT, "proposal", "scripts", "capture_aws_config_evidence.py"
    )
    spec = importlib.util.spec_from_file_location("capture_aws_config_evidence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEvidenceCaptureTooling:
    """
    The evidence pack's locator tables are *generated*, so a resolver bug becomes
    a wrong citation inside a hashed artifact -- harder to spot than a hand-typed
    one, because the hash makes the file look verified.

    ``capture_aws_config_evidence.py`` resolved each locator with
    ``needle in line``, so ``def send_sms`` matched inside
    ``def send_sms_message`` and report 2 cited 383 (the transport helper) for the
    entry point that is at 449. ``TestEveryMarkdownFile`` catches the wrong
    output; this pins the resolver that produced it.
    """

    def test_every_resolved_locator_lands_on_that_locator(self):
        """A locator must resolve to a whole identifier, not a longer one."""
        for entry in _evidence_capture_script().code_references():
            line = entry["line"]
            assert line is not None, (
                f"{entry['file']} no longer contains {entry['needle']!r}"
            )
            with open(
                os.path.join(PROJECT_ROOT, entry["file"]), encoding="utf-8"
            ) as handle:
                text = handle.read().splitlines()[line - 1]
            assert re.search(rf"\b{re.escape(entry['needle'])}\b", text), (
                f"{entry['file']}:{line} does not contain {entry['needle']!r} as a "
                f"whole identifier, so the table would cite a longer name: "
                f"{text.strip()!r}"
            )


class TestDocumentedTestCount:
    """
    ``ARCHITECTURE.md`` advertises the suite size, and it silently went stale
    (it read 525 while the suite had grown past 560), which makes the docs look
    unmaintained and hides how much is actually covered.

    The band is deliberately wide because this very test is part of the count it
    measures, so an exact-equality assertion would fail the moment it is added.
    A 39-test drift is what this catches; adding one test is not drift.
    """

    TOLERANCE = 12

    def test_the_advertised_suite_size_is_believable(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        match = re.search(r"(\d+)\s+tests?\s+collected", result.stdout)
        assert match, f"could not count the collected tests:\n{result.stdout[-800:]}"
        actual = int(match.group(1))

        documented = re.search(
            r"pytest tests/ -q\s*#\s*(\d+)\s+tests?", _read("ARCHITECTURE.md")
        )
        assert documented, "ARCHITECTURE.md no longer advertises a test count"
        claimed = int(documented.group(1))

        assert abs(claimed - actual) <= self.TOLERANCE, (
            f"ARCHITECTURE.md claims {claimed} tests but {actual} are collected; "
            f"update the count (tolerance ±{self.TOLERANCE})"
        )


# Tooling installed for development but never imported by the project's own
# modules, so it cannot be discovered from the source.
DEV_TOOLING: Set[str] = {
    "black",
    "flake8",
    "mypy",
    "pytest",
    "pytest-cov",
    "sphinx",
    "tzdata",  # Windows platform dependency for stdlib zoneinfo, not directly imported
}

_SOURCE_DIRS = ("tools", "agent", "core", "web", "utils", "scripts", "scheduling")
_ROOT_MODULES = ("hospital_setup.py", "demo.py")


def _project_sources() -> List[str]:
    files: List[str] = []
    for directory in _SOURCE_DIRS:
        for dirpath, _dirs, names in os.walk(os.path.join(PROJECT_ROOT, directory)):
            files += [os.path.join(dirpath, n) for n in names if n.endswith(".py")]
    files += [
        os.path.join(PROJECT_ROOT, name)
        for name in _ROOT_MODULES
        if os.path.exists(os.path.join(PROJECT_ROOT, name))
    ]
    return files


def _third_party_by_scope() -> Tuple[Dict[str, Set[str]], Set[str]]:
    """
    Return ``(all_imports, module_scope_imports)``.

    ``module_scope_imports`` holds only imports executed on import of the file --
    a direct child of the module body. The project deliberately keeps boto3,
    anthropic, certifi and openpyxl *inside* functions or ``try`` blocks so the
    offline test suite and demo run without them, and those land in the first
    mapping but not the second.
    """
    import ast

    stdlib = set(sys.stdlib_module_names)
    local = set(_SOURCE_DIRS)
    for directory in _SOURCE_DIRS:
        path = os.path.join(PROJECT_ROOT, directory)
        if os.path.isdir(path):
            for name in os.listdir(path):
                if name.endswith(".py"):
                    local.add(name[:-3])
    # Root-level modules (see _ROOT_MODULES/_project_sources above) are also
    # local, not third-party - e.g. web/app.py importing hospital_setup for
    # its load_dotenv() helper must not be flagged as an unpinned dependency.
    for name in _ROOT_MODULES:
        if name.endswith(".py"):
            local.add(name[:-3])

    def top_level(module: str) -> str:
        return module.split(".")[0]

    def keep(module: str, seen: Set[str]) -> None:
        top = top_level(module)
        if top and top not in stdlib and top not in local:
            seen.add(top)

    everything: Set[str] = set()
    hard: Set[str] = set()
    for path in _project_sources():
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    keep(alias.name, everything)
            elif isinstance(node, ast.ImportFrom):
                if not node.level and node.module:
                    keep(node.module, everything)

        # ``tree.body`` is the module's own body, so this excludes anything
        # nested in a function, a try/except or a conditional import.
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    keep(alias.name, hard)
            elif isinstance(node, ast.ImportFrom):
                if not node.level and node.module:
                    keep(node.module, hard)
    return {module: set() for module in everything}, hard


def _declared_packages() -> List[str]:
    return [
        line.split("==")[0].strip()
        for line in _read("requirements.txt").splitlines()
        if "==" in line and not line.strip().startswith("#")
    ]


class TestRequirementsTemplate:
    """
    ``requirements.txt`` is documentation a user acts on: ``pip install -r
    requirements.txt`` is step 2 of the README.

    It silently omitted ``botocore`` even though ``scripts/ses_domain_setup.py``
    imports ``botocore.exceptions`` by name, leaving that pin to arrive
    transitively through boto3 -- fragile for a module the code names itself.

    Two rules, because the project has two kinds of dependency:

    * An import that runs when the file is imported must be an installable pin.
    * An optional import (boto3, anthropic, certifi, openpyxl are all deliberately
      lazy) only has to be *acknowledged*, so the commented-out ``anthropic``
      line -- the agent falls back to its rule engine without it -- is correct.
    """

    def test_every_third_party_import_is_acknowledged(self):
        imported, _hard = _third_party_by_scope()
        template = _read("requirements.txt")
        unacknowledged = sorted(
            module
            for module in imported
            if not re.search(rf"\b{re.escape(module)}\b", template)
        )
        assert unacknowledged == [], (
            "these modules are imported by the code but appear nowhere in "
            f"requirements.txt: {unacknowledged}"
        )

    def test_every_module_scope_import_is_a_real_pin(self):
        """A hard import cannot be satisfied by a comment."""
        _imported, hard = _third_party_by_scope()
        declared = set(_declared_packages())
        missing = sorted(
            module
            for module in hard
            if module not in declared and module not in DEV_TOOLING
        )
        assert missing == [], (
            "these modules are imported at module scope, so they are required "
            f"just to start the app, but are not pinned: {missing}"
        )

    def test_every_declared_package_is_imported_or_required_by_another(self):
        """
        A pin nothing needs is a dead pin. ``python-dateutil`` and ``werkzeug``
        are legitimate: each is required by another declared package (botocore
        needs python-dateutil, flask needs Werkzeug), which is why they are
        pinned explicitly instead of left to the resolver.
        """
        import importlib.metadata as metadata

        imported, _hard = _third_party_by_scope()
        needed_by_others: Set[str] = set()
        for package in _declared_packages():
            try:
                requirements = metadata.requires(package) or []
            except Exception:  # not installed in this interpreter
                continue
            for requirement in requirements:
                match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
                if match:
                    needed_by_others.add(match.group(1).lower())

        unjustified = sorted(
            package
            for package in _declared_packages()
            if package.lower() not in imported
            and package.lower() not in needed_by_others
            and package.lower() not in DEV_TOOLING
        )
        assert unjustified == [], (
            "pinned but neither imported nor required by another declared "
            f"package: {unjustified}"
        )

    def test_the_template_can_actually_be_parsed_by_pip(self):
        """Every declaration must be a real ``name==version`` pin."""
        for line in _read("requirements.txt").splitlines():
            stripped = line.split("#")[0].strip()
            if not stripped:
                continue
            assert re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9._+-]+", stripped), (
                f"unparseable requirement line: {line!r}"
            )

    def test_the_openai_pin_keeps_a_compatible_httpx(self):
        """
        ``openai`` and ``httpx`` cannot be pinned independently.

        openai 1.51.0 still passes the ``proxies=`` argument that httpx removed
        in 0.28, so an unpinned httpx resolves to a version that makes every
        request raise ``Client.__init__() got an unexpected keyword argument
        'proxies'``. Nothing crashes visibly: the callers log it and fall back
        to the rules, so the whole LLM feature goes quiet while the tests stay
        green. Pin the pair together, or lift the httpx pin with the openai one.
        """
        pins = dict(
            line.split("==")
            for line in _read("requirements.txt").splitlines()
            if "==" in line and not line.strip().startswith("#")
        )
        pins = {name.strip(): version.strip() for name, version in pins.items()}
        if "openai" not in pins or "httpx" not in pins:
            return

        openai_major_minor = tuple(int(p) for p in pins["openai"].split(".")[:2])
        httpx_version = tuple(int(p) for p in pins["httpx"].split(".")[:2])
        if openai_major_minor <= (1, 51):
            assert httpx_version < (0, 28), (
                "openai %s needs httpx<0.28; %s breaks it with the removed "
                "`proxies` argument" % (pins["openai"], pins["httpx"])
            )
