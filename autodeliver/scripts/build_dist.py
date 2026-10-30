"""Build the Claude distributables from the single skill source:

  dist/autodeliver-dotnet.skill   - skill archive (Save skill in Claude)
  dist/autodeliver-dotnet.plugin  - Cowork / Claude Code plugin (.claude-plugin/plugin.json + skills/)

The plugin version is checked against the package version so the two cannot drift.
Archives are reproducible (sorted entries, fixed timestamps).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skill" / "autodeliver-dotnet"
PLUGIN = ROOT / "plugin"
FIXED = (2020, 1, 1, 0, 0, 0)


def package_version() -> str:
    text = (ROOT / "src" / "autodeliver" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def _add(z: zipfile.ZipFile, source: Path, arcname: str) -> None:
    info = zipfile.ZipInfo(arcname, FIXED)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, source.read_bytes())


def _files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store")


def validate() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", manifest.get("name", "")):
        sys.exit("plugin.json: name must be kebab-case")
    if manifest.get("version") != package_version():
        sys.exit(f"plugin.json version {manifest.get('version')} != package version {package_version()}")
    skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"^---\n(.*?)\n---\n", skill_md, re.S)
    if not front or "name: autodeliver-dotnet" not in front.group(1) or "description:" not in front.group(1):
        sys.exit("SKILL.md: frontmatter needs name and description")
    for ref in re.findall(r"`(references/[^`]+)`", skill_md):
        if not (SKILL / ref).is_file():
            sys.exit(f"SKILL.md references missing file {ref}")


def build(out: Path) -> list[Path]:
    validate()
    out.mkdir(parents=True, exist_ok=True)
    skill_zip = out / "autodeliver-dotnet.skill"
    with zipfile.ZipFile(skill_zip, "w") as z:
        for f in _files(SKILL):
            _add(z, f, f"autodeliver-dotnet/{f.relative_to(SKILL).as_posix()}")
    plugin_zip = out / "autodeliver-dotnet.plugin"
    with zipfile.ZipFile(plugin_zip, "w") as z:
        for f in _files(PLUGIN):
            _add(z, f, f.relative_to(PLUGIN).as_posix())
        for f in _files(SKILL):
            _add(z, f, f"skills/autodeliver-dotnet/{f.relative_to(SKILL).as_posix()}")
    return [skill_zip, plugin_zip]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    for path in build(ap.parse_args().out):
        print(path)
