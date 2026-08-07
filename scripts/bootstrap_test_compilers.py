"""Install and verify the non-Python compilers used by the test suite."""

from __future__ import annotations

import subprocess

import solcx
import vvm
from solcx.install import get_executable as get_solc_executable
from vvm.install import get_executable


LEGACY_VYPER_VERSION = "0.3.10"
LEGACY_VYPER_BUILD = "0.3.10+commit.91361694"
SOLC_VERSION = "0.8.12"


def _version_output(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    return (result.stdout or result.stderr).strip()


def install_compilers() -> dict[str, str]:
    """Install exact legacy compiler releases and return verified build strings."""
    vvm.install_vyper(LEGACY_VYPER_VERSION)
    vyper_executable = get_executable(LEGACY_VYPER_VERSION)
    vyper_build = _version_output(str(vyper_executable))
    if LEGACY_VYPER_BUILD not in vyper_build:
        raise RuntimeError(
            f"unexpected Vyper {LEGACY_VYPER_VERSION} build: {vyper_build}"
        )

    solcx.install_solc(SOLC_VERSION)
    solc_executable = get_solc_executable(SOLC_VERSION)
    solc_build = _version_output(str(solc_executable))
    if f"Version: {SOLC_VERSION}" not in solc_build:
        raise RuntimeError(f"unexpected solc {SOLC_VERSION} build: {solc_build}")

    return {
        "vyper": vyper_build,
        "vyper_path": str(vyper_executable),
        "solc": solc_build,
        "solc_path": str(solc_executable),
    }


if __name__ == "__main__":
    for compiler, value in install_compilers().items():
        print(f"{compiler}: {value}")
