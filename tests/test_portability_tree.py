"""Repository shape, packaging metadata and portability guards (offline)."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import legends_temporal
from legends_temporal import doctor

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "README.md", "AGENTS.md", "CHANGELOG.md", "LICENSE", "pyproject.toml", ".legends-module",
    "docs/AGENT-RECIPE.md", "docs/ONBOARDING.md", "docs/ARCHITECTURE.md", "docs/CLI-CONTRACT.json",
    "docs/KNOWLEDGE-PACK.md", "docs/INTEGRATIONS.md", "docs/TROUBLESHOOTING.md", "docs/LIVE-CANARY.md",
    "knowledge/temporal/pack.json", "skills/cto-legends/SKILL.md", "examples/jobs.example.json",
]
MODULES = ["cli", "config", "client", "schedules", "timers", "workflows", "queues", "namespaces",
           "runtime", "doctor", "mcp_server", "fleet_update", "__main__"]

# Strings that would leak one operator's machines, accounts or private network into a portable tree.
FOUNDER_MARKERS = re.compile(
    r"rccol|[A-Z]:\\\\?(empire|legends-)|100\.115\.|100\.69\.|tailc0b97b|glitchy|hetzner-ops|"
    r"legends-ops-(nerv|hades)|\bNERV\b|\bHADES\b|avalonreset@|omnigent", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".cfg", ".yaml", ".yml"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "build", "dist"}


def _tree_files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and ".egg-info" not in str(p):
            yield p


def test_required_files_exist():
    missing = [rel for rel in REQUIRED if not (ROOT / rel).is_file()]
    assert not missing, missing


def test_package_modules_exist():
    pkg = ROOT / "legends_temporal"
    assert [m for m in MODULES if not (pkg / f"{m}.py").is_file()] == []


def test_version_matches_pyproject_and_contract():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contract = json.loads((ROOT / "docs/CLI-CONTRACT.json").read_text(encoding="utf-8"))
    assert meta["project"]["name"] == "legends-temporal"
    assert meta["project"]["version"] == legends_temporal.__version__ == contract["version"] == "0.1.0"
    assert meta["project"]["scripts"]["legends-temporal"] == "legends_temporal.cli:main"
    assert (ROOT / ".legends-module").read_text(encoding="utf-8").strip().startswith("legends-temporal")


def test_knowledge_pack_manifest_matches_notes():
    pack = json.loads((ROOT / "knowledge/temporal/pack.json").read_text(encoding="utf-8"))
    assert pack["schema"] == "legends.knowledge-pack/v1"
    files = [n["file"] for n in pack["notes"]]
    assert tuple(files) == doctor.KNOWLEDGE_NOTES
    for f in files:
        text = (ROOT / "knowledge/temporal" / f).read_text(encoding="utf-8")
        assert text.startswith("# 0"), f
        assert len(text) > 1500, f


def test_resource_paths_all_exist():
    paths = doctor.resource_paths(ROOT)
    assert len(paths) == len(doctor.REQUIRED_RESOURCES) + len(doctor.KNOWLEDGE_NOTES)
    assert [k for k, v in paths.items() if not Path(v).is_file()] == []


def test_offline_doctor_pack_check_passes():
    from legends_temporal.config import Profile
    checks = {c["name"]: c for c in doctor.offline_checks(Profile(name="t"), root=ROOT)}
    assert checks["recipe_and_knowledge_pack"]["status"] == "PASS"
    assert checks["python"]["status"] == "PASS"
    assert checks["temporalio_sdk"]["status"] == "PASS"


def test_no_founder_specific_strings_in_portable_tree():
    hits = []
    for p in _tree_files():
        if p.suffix.lower() not in TEXT_SUFFIXES or p.name in {"LICENSE", Path(__file__).name, "check_release.py"}:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if FOUNDER_MARKERS.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:100]}")
    assert hits == []


def test_no_em_dashes_in_docs():
    offenders = [str(p.relative_to(ROOT)) for p in _tree_files()
                 if p.suffix.lower() == ".md" and "—" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_portable_defaults_are_generic():
    from legends_temporal.config import Profile
    p = Profile(name="default")
    assert p.address == "localhost:7233"
    assert p.namespace == "default"
    assert p.workflow_queue == p.exec_queue == "legends-temporal"
    assert p.interpreter == "python"
    assert p.timezone == "UTC"
    assert p.cwd == "" and p.agent_ctl == "" and p.ui_url == ""
    assert p.notify.kind == "stdout"


def test_skill_is_router_only():
    text = (ROOT / "skills/cto-legends/SKILL.md").read_text(encoding="utf-8")
    assert "cto-legends" in text
    assert not (ROOT / "skills" / "legends-temporal").exists()
