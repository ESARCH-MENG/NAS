"""Build NAS Tool.app and package it as a macOS DMG on the Desktop."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "NAS Tool"
APP_ICON = PROJECT_ROOT / "packaging" / "assets" / "app_icon.icns"
DMG_NAME = "NAS-Tool.dmg"


def _pyinstaller_command(build_root: Path) -> list[str]:
    if importlib.util.find_spec("PyInstaller") is not None:
        return [sys.executable, "-m", "PyInstaller"]

    environment = build_root / "venv"
    venv.EnvBuilder(system_site_packages=True).create(environment)
    python = environment / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "PyInstaller"],
        check=True,
    )
    return [str(python), "-m", "PyInstaller"]


def _temporary_image_path(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".dmg", dir=output.parent
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def build_dmg(output: Path, replace_existing: bool = False) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("DMG creation is supported on macOS only.")
    if shutil.which("hdiutil") is None:
        raise RuntimeError("The macOS hdiutil command is required to create a DMG.")
    if importlib.util.find_spec("tkinter") is None:
        raise RuntimeError("Python was built without Tkinter support.")
    if importlib.util.find_spec("PIL") is None:
        raise RuntimeError("Install project dependencies first: python3 -m pip install -r requirements.txt")
    if not APP_ICON.is_file():
        raise RuntimeError(f"App icon not found: {APP_ICON}")

    output = output.expanduser().absolute()
    if output.exists() and not replace_existing:
        raise FileExistsError(f"{output} already exists; pass --force to replace it.")
    if output.is_dir():
        raise IsADirectoryError(f"DMG output path is a directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary_image: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="nas-tool-dmg-") as temporary:
            build_root = Path(temporary)
            dist = build_root / "dist"
            command = _pyinstaller_command(build_root)
            environment = os.environ.copy()
            environment["PYINSTALLER_CONFIG_DIR"] = str(build_root / "pyinstaller-config")
            subprocess.run(
                command
                + [
                    "--noconfirm",
                    "--clean",
                    "--windowed",
                    "--name",
                    APP_NAME,
                    "--icon",
                    str(APP_ICON),
                    "--osx-bundle-identifier",
                    "com.marvel.nas-tool",
                    "--distpath",
                    str(dist),
                    "--workpath",
                    str(build_root / "work"),
                    "--specpath",
                    str(build_root),
                    str(PROJECT_ROOT / "nas_tool.py"),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                check=True,
            )

            app = dist / f"{APP_NAME}.app"
            if not app.is_dir():
                raise RuntimeError(f"PyInstaller did not create the app bundle: {app}")

            staging = build_root / "staging"
            staging.mkdir()
            shutil.copytree(app, staging / app.name, symlinks=True)
            (staging / "Applications").symlink_to("/Applications", target_is_directory=True)

            temporary_image = _temporary_image_path(output)
            subprocess.run(
                [
                    "hdiutil",
                    "create",
                    "-volname",
                    APP_NAME,
                    "-srcfolder",
                    str(staging),
                    "-format",
                    "UDZO",
                    str(temporary_image),
                ],
                check=True,
            )
            subprocess.run(["hdiutil", "verify", str(temporary_image)], check=True)

            if output.exists() and not replace_existing:
                raise FileExistsError(f"{output} appeared during the build; it was not replaced.")
            os.replace(temporary_image, output)
            temporary_image = None
    finally:
        if temporary_image is not None:
            temporary_image.unlink(missing_ok=True)

    print(f"DMG created: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "Desktop" / DMG_NAME,
        help=f"Output path (default: ~/Desktop/{DMG_NAME})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace the output DMG if it already exists",
    )
    arguments = parser.parse_args()
    try:
        build_dmg(arguments.output, replace_existing=arguments.force)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from error
    except (FileExistsError, IsADirectoryError, OSError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()