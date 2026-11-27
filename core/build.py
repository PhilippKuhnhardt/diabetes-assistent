"""Build the Rust core for the server and put it where Python imports it (app/diabetes_core.abi3.so).

    python3 core/build.py

Needs a Rust toolchain (rustup.rs). The Dockerfile and CI build it the same way.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
TARGET = CORE.parent / "app" / "diabetes_core.abi3.so"


def main() -> None:
    subprocess.run(["cargo", "build", "--release", "--locked", "-p", "diabetes-core-py"], cwd=CORE, check=True)
    names = {"linux": "libdiabetes_core.so", "darwin": "libdiabetes_core.dylib"}
    built = CORE / "target" / "release" / names.get(sys.platform, "diabetes_core.dll")
    shutil.copyfile(built, TARGET)
    print(f"{TARGET.relative_to(CORE.parent)} built")


if __name__ == "__main__":
    main()
