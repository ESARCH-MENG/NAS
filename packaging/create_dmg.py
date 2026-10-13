#!/usr/bin/env python3
"""Build NAS Tool.app and package it as a macOS DMG on the Desktop."""

from __future__ import annotations

import argparse
import importlib.util
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "NAS Tool"
APP_BUNDLE_IDENTIFIER = "com.marvel.nas-tool"
APP_ICON = PROJECT_ROOT / "packaging" / "assets" / "app_icon.icns"
DMG_NAME = "NAS-Tool.dmg"
DEVELOPMENT_IDENTITY_NAME = "NAS Tool Development"
DEVELOPMENT_IDENTITY_PASSWORD = "nas-tool-development"


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


def _ask_app_version() -> str:
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        version = simpledialog.askstring(
            "NAS Tool Version",
            "Enter the version number for NAS Tool:",
            initialvalue="1.0",
            parent=root,
        )
    finally:
        root.destroy()

    if version is None:
        raise RuntimeError("DMG creation was canceled.")
    return version.strip() or "1.0"


def _set_app_version(app: Path, version: str) -> None:
    info_plist = app / "Contents" / "Info.plist"
    contents = info_plist.read_bytes()
    metadata = plistlib.loads(contents)
    metadata["CFBundleShortVersionString"] = version
    metadata["CFBundleVersion"] = version
    metadata["NSAppleEventsUsageDescription"] = (
        "NAS Tool requests permission to control System Events and configure Image Capture's import destination."
    )
    plist_format = (
        plistlib.FMT_BINARY if contents.startswith(b"bplist00") else plistlib.FMT_XML
    )
    info_plist.write_bytes(plistlib.dumps(metadata, fmt=plist_format))


def _identity_names(valid_only: bool) -> list[str]:
    security = shutil.which("security")
    if security is None:
        return []
    command = [security, "find-identity"]
    if valid_only:
        command.append("-v")
    command += ["-p", "codesigning"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return re.findall(r'^\s*\d+\)\s+[A-Fa-f0-9]+\s+"([^"]+)"', result.stdout, flags=re.MULTILINE)


def _find_codesigning_identity() -> str | None:
    names = _identity_names(valid_only=False)
    if DEVELOPMENT_IDENTITY_NAME in names:
        return DEVELOPMENT_IDENTITY_NAME
    valid = _identity_names(valid_only=True)
    return valid[0] if valid else None


def _default_keychain() -> Path:
    security = shutil.which("security")
    if security is not None:
        result = subprocess.run(
            [security, "default-keychain"], capture_output=True, text=True, check=False
        )
        path = Path(result.stdout.strip().strip('"'))
        if path.is_file():
            return path
    return Path.home() / "Library" / "Keychains" / "login.keychain-db"


def _openssl_config() -> str:
    return f"""[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = {DEVELOPMENT_IDENTITY_NAME}
O = Marvel
[v3]
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
"""


def _create_development_identity() -> str:
    # macOS only keeps TCC Automation/Accessibility grants for a stable code
    # signing identity; ad-hoc signatures change every build and lose them.
    openssl = shutil.which("openssl")
    security = shutil.which("security")
    if openssl is None or security is None:
        raise RuntimeError(
            "The macOS openssl and security commands are required to create the "
            f"'{DEVELOPMENT_IDENTITY_NAME}' signing certificate."
        )
    with tempfile.TemporaryDirectory(prefix="nas-tool-identity-") as work:
        directory = Path(work)
        key = directory / "key.pem"
        certificate = directory / "cert.pem"
        archive = directory / "identity.p12"
        (directory / "openssl.cnf").write_text(_openssl_config())
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "3650",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
                "-config",
                str(directory / "openssl.cnf"),
            ],
            check=True,
        )
        subprocess.run(
            [
                openssl,
                "pkcs12",
                "-export",
                "-inkey",
                str(key),
                "-in",
                str(certificate),
                "-out",
                str(archive),
                "-passout",
                f"pass:{DEVELOPMENT_IDENTITY_PASSWORD}",
            ],
            check=True,
        )
        result = subprocess.run(
            [
                security,
                "import",
                str(archive),
                "-k",
                str(_default_keychain()),
                "-P",
                DEVELOPMENT_IDENTITY_PASSWORD,
                "-T",
                "/usr/bin/codesign",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Could not import the '{DEVELOPMENT_IDENTITY_NAME}' certificate into "
                f"the login keychain: {detail}"
            )
    identity = _find_codesigning_identity()
    if identity != DEVELOPMENT_IDENTITY_NAME:
        raise RuntimeError(
            f"The '{DEVELOPMENT_IDENTITY_NAME}' certificate was created but is not "
            "usable for code signing."
        )
    print(
        f"Created the '{identity}' signing certificate in the login keychain. "
        "The first launch of the new app asks for macOS Automation and "
        "Accessibility permissions again; after approving them once, future "
        "rebuilds keep those permissions."
    )
    return identity


def _sign_app(app: Path) -> str:
    codesign = shutil.which("codesign")
    if codesign is None:
        raise RuntimeError("The macOS codesign command is required to sign the app bundle.")
    identity = _find_codesigning_identity()
    if identity is None:
        identity = _create_development_identity()
    # Sign with the certificate identity and no custom requirement so the default
    # designated requirement anchors to the certificate, which stays identical
    # across rebuilds and keeps the granted TCC permissions valid.
    subprocess.run(
        [
            codesign,
            "--force",
            "--deep",
            "--sign",
            identity,
            "--identifier",
            APP_BUNDLE_IDENTIFIER,
            str(app),
        ],
        check=True,
    )
    subprocess.run(
        [codesign, "--verify", "--deep", "--strict", str(app)],
        check=True,
    )
    return identity


def _versioned_output_path(output: Path, version: str) -> Path:
    output = output.expanduser()
    suffix = output.suffix or ".dmg"
    stem = output.stem if output.suffix else output.name
    version_suffix = f"-{version}"
    if not stem.endswith(version_suffix):
        stem += version_suffix
    return output.with_name(f"{stem}{suffix}")


def build_dmg(output: Path, replace_existing: bool = False, app_version: str | None = None) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("DMG creation is supported on macOS only.")
    if shutil.which("hdiutil") is None:
        raise RuntimeError("The macOS hdiutil command is required to create a DMG.")
    if importlib.util.find_spec("tkinter") is None:
        raise RuntimeError("Python was built without Tkinter support.")
    if importlib.util.find_spec("PIL") is None:
        raise RuntimeError("Install project dependencies first: python3 -m pip install -r requirements.txt")
    if importlib.util.find_spec("Foundation") is None:
        raise RuntimeError(
            "Install project dependencies first: python3 -m pip install -r requirements.txt"
        )
    if not APP_ICON.is_file():
        raise RuntimeError(f"App icon not found: {APP_ICON}")

    if app_version is None or not app_version.strip():
        app_version = _ask_app_version()
    app_version = app_version.strip() or "1.0"
    output = _versioned_output_path(output, app_version).absolute()
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
                    APP_BUNDLE_IDENTIFIER,
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
            _set_app_version(app, app_version)
            _sign_app(app)

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
        help=f"Output path (version is appended; default: ~/Desktop/{DMG_NAME})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace the output DMG if it already exists",
    )
    parser.add_argument(
        "--version",
        dest="app_version",
        help="App version for the bundle and DMG file name; skips the interactive version dialog",
    )
    arguments = parser.parse_args()
    try:
        build_dmg(arguments.output, replace_existing=arguments.force, app_version=arguments.app_version)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from error
    except (FileExistsError, IsADirectoryError, OSError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()