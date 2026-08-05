"""Documentation guard — the anti-rot rails.

The same discipline test_demo_env.py enforces on manifests/deployables,
applied to every tracked markdown file:
  1. no literal project ids/numbers (current values from demo-environment.yaml
     AND a denylist of known-retired ids that once rotted 8 docs);
  2. no retired endpoints/files (each was removed from the system — a doc
     mentioning one is describing a world that no longer exists);
  3. every relative markdown link resolves to a real file (docs deletions
     must not leave dangling references).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ids of decommissioned projects — must never reappear anywhere
RETIRED_IDS = [
    "rick-ai-496214",
    "rick-database-project",
    "rick-serverless-solutions",
    "596474229842",
]

# removed endpoints/files/features: (pattern, what replaced it)
RETIRED_REFS = [
    (re.compile(r"/llm/prompt\b"), "the /llm-stream surface"),
    (re.compile(r"\bseed_acl\.py\b"), "the acl: manifest seed"),
    (re.compile(r"\bdeploy_all\.sh\b"), "deploy_services.py"),
    (re.compile(r"\bcreate_ilb\.sh\b"), "provision_services.py"),
    (re.compile(r"\bFC-generate-log\b"), "ML-interaction"),
    (re.compile(r"unified-llm-enpdoint-apps"), "per-user apps (users scope)"),
]


def _tracked_md() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line]


def _env_ids() -> list[str]:
    import sys

    sys.path.insert(0, str(ROOT))
    import demo_env

    env = demo_env.load_env()
    return list(env["projects"].values())


def test_docs_have_no_project_ids():
    """Docs reference demo-environment.yaml keys or placeholders, never ids."""
    bad = []
    ids = _env_ids() + RETIRED_IDS
    for md in _tracked_md():
        text = md.read_text()
        for pid in ids:
            if pid in text:
                bad.append(f"{md.relative_to(ROOT)}: literal '{pid}'")
    assert not bad, "project ids belong ONLY in demo-environment.yaml:\n" + "\n".join(bad)


def test_retired_ids_nowhere_in_the_repo():
    """RETIRED_IDS scanned across EVERY tracked file, not just markdown —
    a decommissioned project id hid for months in the services' database.py
    error hint (shipped in a user-facing traceback, found live) because this
    guard only read docs. Excludes this file (it defines the denylist)."""
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    bad = []
    for rel in out.splitlines():
        p = ROOT / rel
        if (not p.is_file() or p == Path(__file__)
                or p.suffix in {".png", ".jpg", ".ico", ".zip"}):
            continue
        text = p.read_text(errors="ignore")
        bad += [f"{rel}: literal '{pid}'" for pid in RETIRED_IDS if pid in text]
    assert not bad, ("retired project ids must not appear anywhere "
                     "(use neutral fixtures like my-ai-project):\n" + "\n".join(bad))


def test_docs_have_no_retired_references():
    bad = []
    for md in _tracked_md():
        for line_no, line in enumerate(md.read_text().splitlines(), 1):
            for pat, repl in RETIRED_REFS:
                if pat.search(line):
                    bad.append(f"{md.relative_to(ROOT)}:{line_no}: "
                               f"'{pat.pattern}' is retired (now: {repl})")
    assert not bad, "\n".join(bad)


_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)\s]*)?\)")


def test_relative_doc_links_resolve():
    bad = []
    for md in _tracked_md():
        for line_no, line in enumerate(md.read_text().splitlines(), 1):
            for target in _LINK.findall(line):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                if not (md.parent / target).exists():
                    bad.append(f"{md.relative_to(ROOT)}:{line_no}: broken link -> {target}")
    assert not bad, "\n".join(bad)


def test_guard_catches_a_planted_violation(tmp_path):
    """The retired-ref regexes actually match what they claim to."""
    assert RETIRED_REFS[0][0].search("POST /llm/prompt body")
    assert not RETIRED_REFS[0][0].search("POST /llm-stream/prompt body")
