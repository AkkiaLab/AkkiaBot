"""Counter-example tests for the import-linter contracts in pyproject.toml.

Each case builds a throw-away copy of the four top-level packages under a
temporary directory, plants one forbidden import, and runs ``lint-imports``
against the repository's real contract configuration. The temporary
directory is put first on ``PYTHONPATH`` so it shadows the installed
workspace packages.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("akkiabot_server", "akkiabot_core", "akkiabot_antispam", "akkiabot_whois")


def _lint_imports(tmp_path: Path, violations: dict[str, str]) -> subprocess.CompletedProcess[str]:
    for pkg in PACKAGES:
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text(violations.get(pkg, ""))
    config = tmp_path / "importlinter.toml"
    shutil.copy(REPO_ROOT / "pyproject.toml", config)
    lint_imports = Path(sys.executable).parent / "lint-imports"
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    return subprocess.run(
        [str(lint_imports), "--config", str(config), "--no-cache"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_tree_passes(tmp_path: Path) -> None:
    result = _lint_imports(tmp_path, {"akkiabot_server": "import akkiabot_core\n"})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("package", "source", "contract"),
    [
        pytest.param("akkiabot_whois", "import akkiabot_core\n", "layers", id="whois->core"),
        pytest.param("akkiabot_antispam", "import akkiabot_core\n", "layers", id="antispam->core"),
        pytest.param("akkiabot_core", "import akkiabot_whois\n", "layers", id="core->whois"),
        pytest.param("akkiabot_core", "import akkiabot_server\n", "layers", id="core->server"),
        pytest.param(
            "akkiabot_whois",
            "import telegram\n",
            "packages/* 不依赖 Telegram 框架与 Web 框架",
            id="whois->telegram",
        ),
        pytest.param(
            "akkiabot_core",
            "from fastapi import FastAPI\n",
            "packages/* 不依赖 Telegram 框架与 Web 框架",
            id="core->fastapi",
        ),
    ],
)
def test_violation_is_rejected(tmp_path: Path, package: str, source: str, contract: str) -> None:
    result = _lint_imports(tmp_path, {package: source})
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "BROKEN" in output
    if contract != "layers":
        assert f"{contract} BROKEN" in output
