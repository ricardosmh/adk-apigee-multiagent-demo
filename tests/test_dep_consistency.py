"""Guard against shared-dependency version drift across the agents.

Each agent has its own pyproject.toml (Agent Engine deploys read it), so the
shared stack (google-adk, a2a-sdk, mcp, aiplatform) is declared 4×. This test
fails if those pins diverge — the exact pain from the 2.3 bump, where the
version had to be changed in four places and it was easy to miss one.

Pure stdlib (tomllib) — runs even without the agent deps installed.
"""
import pathlib
import re

import tomllib

AGENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "agents"

# Packages that MUST be pinned identically everywhere they appear.
SHARED = {"google-adk", "a2a-sdk", "mcp", "google-cloud-aiplatform"}


def _deps(pyproject: pathlib.Path) -> dict[str, str]:
    """{package_name: version_spec} from a pyproject, ignoring extras."""
    data = tomllib.loads(pyproject.read_text())
    out: dict[str, str] = {}
    for spec in data["project"]["dependencies"]:
        m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?(.*)$", spec.strip())
        assert m, f"unparseable dependency: {spec!r}"
        out[m.group(1).lower()] = m.group(2).strip()
    return out


def test_shared_pins_consistent():
    pyprojects = sorted(AGENTS_DIR.glob("*/pyproject.toml"))
    assert pyprojects, f"no agent pyproject.toml found under {AGENTS_DIR}"

    by_agent = {p.parent.name: _deps(p) for p in pyprojects}

    problems = []
    for pkg in sorted(SHARED):
        specs = {a: d[pkg] for a, d in by_agent.items() if pkg in d}
        if len(set(specs.values())) > 1:
            problems.append(f"  {pkg}: " + ", ".join(f"{a}={v!r}" for a, v in specs.items()))

    assert not problems, (
        "Shared dependency version drift across agents "
        "(pin them identically in every agents/*/pyproject.toml):\n"
        + "\n".join(problems)
    )


def test_deploy_host_adk_pin_satisfies_agent_range():
    """agents/provision/requirements-deploy.txt pins google-adk for the DEPLOY HOST
    (the deploy worker imports the app locally); the pin must satisfy every agent
    pyproject's google-adk range so host and engine run the same ADK."""
    req = (AGENTS_DIR / "provision" / "requirements-deploy.txt").read_text()
    m = re.search(r"^google-adk(?:\[[^\]]*\])?==([\w.]+)$", req, re.M)
    assert m, "requirements-deploy.txt must PIN google-adk (==x.y.z)"
    pin = m.group(1)
    # extras the supervisor needs at import time must be on the host install
    extras = re.search(r"^google-adk\[([^\]]*)\]==", req, re.M)
    assert extras and {"a2a", "agent-identity"} <= {e.strip() for e in extras.group(1).split(",")}, \
        "requirements-deploy.txt google-adk needs [a2a,agent-identity] extras"
    # the app import also needs ADK's A2A layer + MCP tooling on the host,
    # with specs matching the engine pyprojects
    for pkg in ("a2a-sdk", "mcp", "nest-asyncio"):
        rm = re.search(rf"^{pkg}([~><=][^\s#]*)", req, re.M)
        assert rm, f"requirements-deploy.txt missing {pkg}"
        engine_spec = _deps(AGENTS_DIR / "my_order_agent" / "pyproject.toml").get(pkg)
        assert rm.group(1) == engine_spec, \
            f"{pkg}: deploy-host spec {rm.group(1)!r} != engine spec {engine_spec!r}"
    for p in sorted(AGENTS_DIR.glob("*/pyproject.toml")):
        spec = _deps(p).get("google-adk", "")
        for clause in filter(None, (c.strip() for c in spec.split(","))):
            op = re.match(r"(>=|<=|==|<|>|~=)\s*([\w.]+)$", clause)
            assert op, f"unparseable clause {clause!r} in {p}"
            key = lambda v: tuple(int(x) for x in re.findall(r"\d+", v))
            ok = {">=": key(pin) >= key(op.group(2)), "<=": key(pin) <= key(op.group(2)),
                  "==": key(pin) == key(op.group(2)), "<": key(pin) < key(op.group(2)),
                  ">": key(pin) > key(op.group(2)), "~=": key(pin) >= key(op.group(2))}[op.group(1)]
            assert ok, f"deploy-host pin {pin} violates {clause!r} in {p.parent.name}"
