import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from autodeliver.config import load_config  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    c = load_config(tmp_path, require_remote=False)        # packaged defaults, no overlay
    c["phi"]["gitleaks"] = False
    return c


def make_niss(base9: str) -> str:
    """Synthetic number with valid check digits, generated at runtime (never stored as a literal)."""
    return base9 + f"{97 - int(base9) % 97:02d}"
