#!/usr/bin/env python
"""Build ``ClaudeCodeOverlay.exe`` with PyInstaller.

Usage::

    python build.py                 # one-file, windowed, icon bundled
    python build.py --clean         # remove build/ and dist/ first
    python build.py --onedir        # folder build (faster start-up, easier to debug)
    python build.py --console       # keep a console window for debugging
    python build.py --no-upx        # disable UPX compression

The script is intentionally self-contained: CI calls it with no arguments and
expects ``dist/ClaudeCodeOverlay.exe`` to exist afterwards.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
ASSETS = ROOT / "assets"
ICON = ASSETS / "icon.ico"
ENTRY = SRC / "main.py"
APP_NAME = "ClaudeCodeOverlay"

# Qt ships far more than an overlay needs; trimming these keeps the one-file
# binary near ~35 MB instead of well past 60 MB.
EXCLUDED_MODULES = [
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuick3D",
    "PyQt6.QtMultimedia",
    "PyQt6.QtBluetooth",
    "PyQt6.QtSql",
    "PyQt6.QtTest",
    "PyQt6.QtPdf",
    "PyQt6.QtDesigner",
    "PyQt6.QtCharts",
    "tkinter",
    "pytest",
    "numpy",
    "PIL",
]
# Deliberately *not* excluded: QtNetwork and QtOpenGL. Their Python bindings are
# unused, but Qt6Gui/Qt6Widgets load the matching DLLs at runtime and excluding
# the modules has been known to break the frozen build in non-obvious ways.


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def ensure_prerequisites() -> None:
    """Fail early and loudly rather than half-way through a 60 second build."""
    if not ENTRY.exists():
        raise SystemExit(f"entry point missing: {ENTRY}")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyInstaller is not installed.\n"
            "    pip install -r requirements.txt"
        )
    try:
        import PyQt6  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyQt6 is not installed.\n"
            "    pip install -r requirements.txt"
        )
    if not ICON.exists():
        log(f"icon missing at {ICON}; regenerating")
        subprocess.run([sys.executable, str(ASSETS / "make_icon.py")], check=True)


def clean() -> None:
    for name in ("build", "dist"):
        target = ROOT / name
        if target.exists():
            log(f"removing {target}")
            shutil.rmtree(target, ignore_errors=True)
    spec = ROOT / f"{APP_NAME}.spec"
    if spec.exists():
        spec.unlink()


def build_command(args: argparse.Namespace) -> list[str]:
    # ``--add-data`` uses ';' on Windows and ':' elsewhere.
    separator = ";" if os.name == "nt" else ":"

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(ENTRY),
        "--name",
        APP_NAME,
        "--noconfirm",
        "--clean",
        "--onedir" if args.onedir else "--onefile",
        "--console" if args.console else "--windowed",
        "--paths",
        str(SRC),
        "--add-data",
        f"{ICON}{separator}assets",
        "--icon",
        str(ICON),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT),
    ]

    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]

    # Hidden imports: main.py resolves these through sys.path, not by package
    # name, so PyInstaller's analyser needs them spelled out.
    for module in ("config", "parser"):
        command += ["--hidden-import", module]

    if args.no_upx:
        command.append("--noupx")
    if args.version_file and Path(args.version_file).exists():
        command += ["--version-file", args.version_file]
    if args.extra:
        command += args.extra

    return command


def report(args: argparse.Namespace) -> None:
    if args.onedir:
        target = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe"
    else:
        target = ROOT / "dist" / f"{APP_NAME}.exe"
    # On non-Windows hosts PyInstaller drops the .exe suffix.
    if not target.exists() and target.suffix == ".exe":
        target = target.with_suffix("")

    if target.exists():
        size_mb = target.stat().st_size / (1024 * 1024)
        log(f"built {target} ({size_mb:.1f} MB)")
    else:
        raise SystemExit(f"build finished but {target} was not produced")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ClaudeCodeOverlay.exe")
    parser.add_argument("--clean", action="store_true", help="wipe build/ and dist/ first")
    parser.add_argument("--onedir", action="store_true", help="folder build instead of one-file")
    parser.add_argument("--console", action="store_true", help="keep a console window")
    parser.add_argument("--no-upx", action="store_true", help="disable UPX compression")
    parser.add_argument("--version-file", default="", help="optional PyInstaller version resource")
    parser.add_argument(
        "extra",
        nargs="*",
        help="additional arguments passed straight through to PyInstaller",
    )
    args = parser.parse_args(argv)

    ensure_prerequisites()
    if args.clean:
        clean()

    command = build_command(args)
    log(" ".join(command))
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        return result.returncode

    report(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
