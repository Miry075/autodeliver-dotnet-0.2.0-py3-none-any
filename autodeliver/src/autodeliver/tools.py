"""Cross-platform installation and discovery of external scanners (Windows, Linux, macOS).

gitleaks is downloaded from its GitHub release and verified against the release's checksums file.
dotnet global tools are installed with `dotnet tool update --global` (installs when missing, idempotent).
"""
from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

RELEASES = "https://github.com/gitleaks/gitleaks/releases/download/v{v}/"


def install_dir(cfg: dict) -> Path:
    configured = cfg["tools"].get("install_dir")
    return Path(configured).expanduser() if configured else Path.home() / ".autodeliver" / "tools"


def gitleaks_asset(version: str, system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_name = {"windows": "windows", "linux": "linux", "darwin": "darwin"}.get(system)
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if not os_name or not arch:
        raise RuntimeError(f"no gitleaks build for {system}/{machine}")
    ext = "zip" if os_name == "windows" else "tar.gz"
    return f"gitleaks_{version}_{os_name}_{arch}.{ext}"


def expected_sha256(checksums: str, asset: str) -> str:
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset:
            return parts[0].lower()
    raise RuntimeError(f"{asset} not listed in the release checksums")


def _binary_name() -> str:
    return "gitleaks.exe" if platform.system() == "Windows" else "gitleaks"


def find_gitleaks(cfg: dict) -> str | None:
    on_path = shutil.which("gitleaks")
    if on_path:
        return on_path
    candidate = install_dir(cfg) / f"gitleaks-{cfg['tools']['gitleaks_version']}" / _binary_name()
    return str(candidate) if candidate.is_file() else None


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - fixed https release URL
        return r.read()


def install_gitleaks(cfg: dict, fetch=_download) -> Path:
    version = cfg["tools"]["gitleaks_version"]
    target_dir = install_dir(cfg) / f"gitleaks-{version}"
    binary = target_dir / _binary_name()
    if binary.is_file():
        return binary
    asset = gitleaks_asset(version)
    base = RELEASES.format(v=version)
    payload = fetch(base + asset)
    want = expected_sha256(fetch(base + f"gitleaks_{version}_checksums.txt").decode(), asset)
    got = hashlib.sha256(payload).hexdigest()
    if got != want:
        raise RuntimeError(f"gitleaks checksum mismatch for {asset}: {got} != {want}")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _binary_name()
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            data = z.read(name)                       # only the binary; no path from the archive is used
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as t:
            member = t.getmember(name)
            data = t.extractfile(member).read()       # type: ignore[union-attr]
    binary.write_bytes(data)
    if os.name != "nt":
        binary.chmod(0o755)
    return binary


def install_dotnet_tool(package: str, version: str = "") -> tuple[bool, str]:
    args = ["dotnet", "tool", "update", "--global", package]
    if version:
        args += ["--version", version]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-300:]


def dotnet_tools_dir() -> Path:
    return Path.home() / ".dotnet" / "tools"


def announce_path(directory: Path) -> None:
    """Make a directory available to later Azure Pipelines steps."""
    if os.environ.get("TF_BUILD"):
        print(f"##vso[task.prependpath]{directory}")
