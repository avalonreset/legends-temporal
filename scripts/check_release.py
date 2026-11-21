"""Offline release checks for links, private residue, image metadata hygiene, and required artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "dist", "build", "__pycache__", ".pytest_cache"}
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")
FORBIDDEN_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".ps1", ".bat", ".cmd", ".vbs", ".ahk", ".exe", ".dll", ".pyc", ".ttf", ".otf"}

REQUIRED_FILES = [
    ".gitattributes",
    ".gitignore",
    ".legends-module",
    ".legends-router-pin",
    "AGENTS.md",
    "CHANGELOG.md",
    "CITATION.cff",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "MANIFEST.in",
    "NOTICE",
    "PRIVACY.md",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "pyproject.toml",
    "setup.py",
    "assets/banner.png",
    "assets/banner.webp",
    "assets/manifest.json",
    "assets/preview.png",
    "assets/social-preview.png",
    "assets/cards/r1/slot-1-agent-handoff.svg",
    "assets/cards/r1/slot-2-three-pillars.svg",
    "assets/cards/r1/slot-3-plain-english-chains.svg",
    "assets/cards/r1/slot-4-temporal-architecture.svg",
    "assets/cards/r1/slot-5-adhoc-vs-temporal.svg",
    "assets/cards/r1/slot-7-message-avalon-reset.svg",
    "assets/cards/r1/slot-collection-star-shrine.svg",
    "docs/AGENT-RECIPE.md",
    "docs/ARCHITECTURE.md",
    "docs/CLI-CONTRACT.json",
    "docs/INTEGRATIONS.md",
    "docs/KNOWLEDGE-PACK.md",
    "docs/LIVE-CANARY.md",
    "docs/ONBOARDING.md",
    "docs/RELEASE.md",
    "docs/TROUBLESHOOTING.md",
    "docs/catalog-entry.candidate.json",
    "docs/router-provenance.json",
    "examples/jobs.example.json",
    "knowledge/temporal/pack.json",
    "knowledge/temporal/01-temporal-mental-model-and-determinism.md",
    "knowledge/temporal/02-schedules-and-durable-timers.md",
    "knowledge/temporal/03-zero-rewrite-scriptjob-and-agentjob-pattern.md",
    "knowledge/temporal/04-timeouts-retries-and-idempotency.md",
    "knowledge/temporal/05-self-hosted-postgres-and-tailnet-hardening.md",
    "knowledge/temporal/06-visibility-queries-and-failure-diagnosis.md",
    "legends_temporal/__init__.py",
    "legends_temporal/__main__.py",
    "legends_temporal/cli.py",
    "skills/cto-legends/SKILL.md",
]

FORBIDDEN_CODE_PATTERNS = [
    r"-WindowStyle\s+Hidden",
    r"-ExecutionPolicy\s+Bypass",
    r"CryptUnprotectData",
    r"GetAsyncKeyState",
    r"SetWindowsHookEx",
    r"IndexedDB",
    r"LevelDB",
    r"Login Data",
    r"(?i)\.tail[a-z0-9]+\.ts\.net",
    r"(?i)nsec1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{30,}",
    r"(?i)sk_live_[a-z0-9]{12,}",
    r"(?i)rk_live_[a-z0-9]{12,}",
    r"(?i)whsec_[a-z0-9]{12,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
]

HOUSE_PATH_PATTERNS = [
    r"C:\\" + r"Users\\",
    r"(?<![:\w])/home/[a-z0-9_-]+/",
    r"E:\\" + r"empire",
    r"E:\\" + r"legends-temporal",
    r"E:\\" + r"ai-marketing-hub",
    r"rc" + r"col",
    r"glit" + r"chyglitch",
    r"omni" + r"gent",
    r"100\." + r"115\.",
    r"100\." + r"69\.",
    r"tail" + r"c0b97b",
    r"legends-ops-" + r"(?:ne" + r"rv|ha" + r"des)",
    r"\bNE" + r"RV\b",
    r"\bHA" + r"DES\b",
]

PNG_METADATA_CHUNKS = {"tEXt", "iTXt", "zTXt", "eXIf"}
WEBP_METADATA_CHUNKS = {b"EXIF", b"XMP "}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".cff", ".in", ".txt", ".cfg", ".ini", ".svg"}


def iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP or part.endswith(".egg-info") for part in rel.parts):
            continue
        out.append(p)
    return out


def image_metadata_issues(path: Path) -> list[str]:
    """Flag raster metadata chunks that can carry authoring residue."""
    try:
        raw = path.read_bytes()
    except OSError:
        return [f"Cannot read image: {path}"]
    issues: list[str] = []
    suffix = path.suffix.lower()
    if suffix == ".png":
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            return [f"Not a PNG file: {path}"]
        offset = 8
        while offset + 8 <= len(raw):
            length = struct.unpack(">I", raw[offset : offset + 4])[0]
            chunk = raw[offset + 4 : offset + 8]
            try:
                name = chunk.decode("latin1")
            except UnicodeError:
                break
            if name in PNG_METADATA_CHUNKS:
                issues.append(f"Image metadata chunk {name} in {path.name}; strip authoring residue.")
            if name == "IEND":
                break
            offset += 12 + length
    elif suffix == ".webp":
        if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
            return [f"Not a WebP file: {path}"]
        offset = 12
        while offset + 8 <= len(raw):
            tag = raw[offset : offset + 4]
            length = struct.unpack("<I", raw[offset + 4 : offset + 8])[0]
            if tag in WEBP_METADATA_CHUNKS:
                issues.append(f"Image metadata chunk {tag!r} in {path.name}; strip authoring residue.")
                break
            offset += 8 + length + (length % 2)
    return issues


def check_release(root: Path = ROOT) -> list[str]:
    issues: list[str] = []

    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            issues.append(f"Missing required file: {rel}")

    mod_file = root / ".legends-module"
    if mod_file.is_file() and mod_file.read_text(encoding="utf-8").strip() != "legends-temporal":
        issues.append(".legends-module must equal 'legends-temporal'")

    pin_file = root / ".legends-router-pin"
    if pin_file.is_file():
        pin = pin_file.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", pin):
            issues.append(".legends-router-pin must be a 40-char lowercase hex SHA")

    ver_file = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").is_file() else None
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").is_file() else ""
    m_py = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    py_ver = m_py.group(1) if m_py else None
    init_text = (root / "legends_temporal" / "__init__.py").read_text(encoding="utf-8")
    m_init = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.M)
    init_ver = m_init.group(1) if m_init else None
    contract = json.loads((root / "docs" / "CLI-CONTRACT.json").read_text(encoding="utf-8"))
    contract_ver = contract.get("version")
    pack = json.loads((root / "knowledge" / "temporal" / "pack.json").read_text(encoding="utf-8"))
    pack_ver = pack.get("version")
    cff_text = (root / "CITATION.cff").read_text(encoding="utf-8") if (root / "CITATION.cff").is_file() else ""
    m_cff = re.search(r'^version:\s*"([^"]+)"', cff_text, re.M)
    cff_ver = m_cff.group(1) if m_cff else None
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m_chg = re.search(r"^#{1,3}\s+\[?v?(\d+\.\d+\.\d+)\]?", changelog, re.M)
    chg_ver = m_chg.group(1) if m_chg else None
    vers = {ver_file, py_ver, init_ver, contract_ver, pack_ver, cff_ver, chg_ver}
    if None in vers or len(vers) != 1:
        issues.append(f"Version mismatch across metadata files: {vers}")

    router = root / "skills" / "cto-legends" / "SKILL.md"
    prov = root / "docs" / "router-provenance.json"
    if router.is_file() and prov.is_file():
        try:
            expected = json.loads(prov.read_text(encoding="utf-8"))["sha256"]
            if hashlib.sha256(router.read_bytes()).hexdigest() != expected:
                issues.append("Router SKILL.md differs from docs/router-provenance.json sha256.")
        except (ValueError, KeyError):
            issues.append("docs/router-provenance.json is invalid.")

    manifest_path = root / "assets" / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in manifest_data.get("files", []):
                asset_path = root / "assets" / entry["name"]
                if not asset_path.is_file():
                    issues.append(f"Manifest asset missing: {entry['name']}")
                    continue
                raw_asset = asset_path.read_bytes()
                if len(raw_asset) != entry.get("bytes"):
                    issues.append(f"Manifest byte mismatch for {entry['name']}")
                if hashlib.sha256(raw_asset).hexdigest() != entry.get("sha256"):
                    issues.append(f"Manifest sha256 mismatch for {entry['name']}")
        except Exception as exc:
            issues.append(f"Invalid assets/manifest.json: {exc}")

    files = iter_files(root)
    skill_files = [p.relative_to(root).as_posix() for p in files if p.name == "SKILL.md"]
    if skill_files != ["skills/cto-legends/SKILL.md"]:
        issues.append(f"Expected only skills/cto-legends/SKILL.md, found: {skill_files}")

    for p in files:
        rel = p.relative_to(root).as_posix()
        if rel in {"scripts/check_release.py", "tests/test_portability_tree.py"}:
            continue
        suffix = p.suffix.lower()
        if suffix in FORBIDDEN_EXTENSIONS or p.name == ".env":
            issues.append(f"Forbidden file extension or secret file: {rel}")
            continue
        if suffix in {".png", ".webp"}:
            issues.extend(f"{rel}: {issue}" for issue in image_metadata_issues(p))
            continue
        if suffix in TEXT_SUFFIXES or p.name in {"LICENSE", "NOTICE", "VERSION"}:
            text = p.read_text(encoding="utf-8", errors="replace")
            if CONTROL_CHAR_RE.search(text):
                issues.append(f"Control character or U+FFFD replacement character in {rel}")
            for pat in FORBIDDEN_CODE_PATTERNS:
                if re.search(pat, text, re.IGNORECASE):
                    issues.append(f"Forbidden pattern '{pat}' in {rel}")
            for pat in HOUSE_PATH_PATTERNS:
                if re.search(pat, text, re.IGNORECASE):
                    issues.append(f"House/private pattern '{pat}' in {rel}")
            if suffix == ".md" and rel != "skills/cto-legends/SKILL.md":
                if "\u2014" in text or "\u2013" in text:
                    issues.append(f"Typography dash (em/en dash) found in {rel}")
                if "\\`" in text:
                    issues.append(f"Backslash-escaped backtick in Markdown copy: {rel}")
                for target_link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                    if target_link.startswith(("https://", "http://", "mailto:", "#")):
                        continue
                    cleaned = unquote(target_link.split("#", 1)[0].split("?", 1)[0]).strip()
                    if not cleaned:
                        continue
                    resolved = (p.parent / cleaned).resolve()
                    if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                        issues.append(f"Broken or external local link in {rel}: {target_link}")

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline release checks for legends-temporal.")
    parser.add_argument("--root", type=Path, default=ROOT)
    options = parser.parse_args()
    issues = check_release(options.root)
    for issue in issues:
        print(f"FAIL: {issue}")
    print(f"Release checks: {'FAIL' if issues else 'PASS'} ({len(issues)} issues)")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
