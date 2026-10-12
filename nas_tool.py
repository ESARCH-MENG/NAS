#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import hashlib
import argparse
import json
import logging
import os
import plistlib
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterator
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

CHUNK_SIZE = 1024 * 1024
TEXT_PREVIEW_LIMIT = 12_000
APP_VERSION = "1.0"
BG = "#ffffff"
PANEL = "#fbfaf7"
PANEL_ALT = "#f1eee8"
BORDER = "#16303b"
SOFT_BORDER = "#ded8cf"
TEXT = "#171717"
MUTED = "#6f7377"
ACCENT = "#c47f2c"
RED = "#ff5c57"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ppm", ".pgm"}
TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".csv",
    ".html",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".py",
    ".sql",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def get_app_version() -> str:
    if getattr(sys, "frozen", False):
        info_plist = Path(sys.executable).parent.parent / "Info.plist"
        try:
            metadata = plistlib.loads(info_plist.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            pass
        else:
            version = metadata.get("CFBundleShortVersionString")
            if isinstance(version, str) and version.strip():
                return version.strip()
    return APP_VERSION


HELP_TEXT = """NAS Tool User Guide

1. Bank folders
Use SELECT FOLDER in the Bank-A and Bank-B panels to choose two separate
folders. The selections are saved and restored the next time NAS Tool starts.
OPEN FOLDER opens the selected folder in the operating system.

2. Compare and preview
Click COMPARE to list files that exist only in Bank-A or only in Bank-B.
Select one or more list entries to preview the active image or text file.
Clicking a selected entry again removes it from the selection.

3. Sync
SYNC A -> B copies new files and changed same-path files from Bank-A to Bank-B.
It never deletes extra files from Bank-B.

SYNC SELECTED copies only selected comparison results to the opposite bank.
Bank-A selections go to Bank-B, and Bank-B selections go to Bank-A. Files from
both lists can be selected and copied in one operation.

4. Download
Use the top SELECT FOLDER button to choose a source folder, then click DOWNLOAD.
Its files and subfolders are copied into Bank-A. Existing same-path files are
skipped and are not overwritten.

On macOS, clicking DOWNLOAD without a source folder opens Image Capture and
sets its import destination to Bank-A. Connect and select your device first.
Allow Accessibility and Automation access when macOS requests it. If setup
fails, no import is started; select the device and click DOWNLOAD again.
Choose the photos and start importing in Image Capture yourself.

5. Delete
DELETE SELECTED permanently deletes the selected comparison results from their
current bank after confirmation. This action cannot be undone.

6. Emergency Stop
The circular red STOP button has priority over active copy operations. It stops
SYNC, SYNC SELECTED, or a folder DOWNLOAD as soon as the current copy chunk
finishes. An incomplete new file is removed, and an existing destination is
not replaced until its complete new copy is ready.

During a macOS Image Capture import, STOP quits Image Capture. When no supported
operation is active, STOP does nothing and does not close NAS Tool.

7. Scheduled Sync (macOS)
Choose a time and cycle, then enable Scheduled Sync. The background task copies
Bank-A to Bank-B at the configured calendar time. Disable the switch before
changing the schedule, then enable it again. Both bank folders must be
available. Scheduled Sync is not available on Windows.

Errors and unavailable folders are reported in dialogs. NAS volumes must be
mounted and accessible before comparing, copying, or scheduling a sync.
"""


AGENT_LABEL = "com.marvel.nas-tool.sync"
CancelCheck = Callable[[], bool]


class OperationCancelled(Exception):
    pass


def _raise_if_cancelled(cancelled: CancelCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise OperationCancelled


def default_settings_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NAS Tool" / "settings.json"
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NAS Tool" / "settings.json"


@dataclass
class Settings:
    bank_a: str = ""
    bank_b: str = ""
    schedule_enabled: bool = False
    sync_hour: int = 9
    sync_minute: int = 0
    sync_cycle: str = "daily"

    def validate(self) -> None:
        if not isinstance(self.bank_a, str) or not isinstance(self.bank_b, str):
            raise ValueError("Saved bank paths must be strings.")
        if type(self.sync_hour) is not int or not 0 <= self.sync_hour <= 23:
            raise ValueError("Sync hour must be a whole number from 00 to 23.")
        if type(self.sync_minute) is not int or not 0 <= self.sync_minute <= 59:
            raise ValueError("Sync minute must be a whole number from 00 to 59.")
        if self.sync_cycle not in ("daily", "weekly", "monthly"):
            raise ValueError("Sync cycle must be daily, weekly, or monthly.")
        if type(self.schedule_enabled) is not bool:
            raise ValueError("The scheduled sync flag must be a boolean.")
        if self.schedule_enabled and (not self.bank_a or not self.bank_b):
            raise ValueError("Scheduled sync requires both bank folders.")


def load_settings(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("Settings must be a JSON object.")
    if "interval_minutes" in data:
        interval = data.pop("interval_minutes")
        if type(interval) is not int or not 1 <= interval <= 525600:
            raise ValueError("Invalid legacy sync interval.")
        # An interval cannot be mapped to a calendar schedule without user consent.
        data["schedule_enabled"] = False
    try:
        settings = Settings(**data)
    except TypeError as error:
        raise ValueError(f"Invalid settings fields: {error}") from error
    settings.validate()
    return settings


def save_settings(path: Path, settings: Settings) -> None:
    settings.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as destination:
            json.dump(asdict(settings), destination, indent=2)
            destination.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def launch_agent_payload(settings: Settings, settings_path: Path) -> dict[str, object]:
    settings.validate()
    if not settings.schedule_enabled:
        raise ValueError("Scheduled sync is disabled.")
    script = Path(__file__).resolve()
    command = [sys.executable, str(script)]
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    calendar = {"Hour": settings.sync_hour, "Minute": settings.sync_minute}
    if settings.sync_cycle == "weekly":
        calendar["Weekday"] = 1
    elif settings.sync_cycle == "monthly":
        calendar["Day"] = 1
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": command + ["--scheduled-sync", "--settings", str(settings_path.resolve())],
        "StartCalendarInterval": calendar,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Background",
        "StandardOutPath": str(settings_path.parent / "launchd.log"),
        "StandardErrorPath": str(settings_path.parent / "launchd-error.log"),
    }


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments], capture_output=True, text=True, check=False
    )


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode:
        raise OSError(
            result.stderr.strip() or result.stdout.strip()
            or f"launchctl failed with exit code {result.returncode}."
        )


def apply_schedule(settings: Settings, settings_path: Path) -> None:
    if sys.platform != "darwin":
        raise ValueError("Background scheduled sync is available on macOS only.")
    settings.validate()
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{AGENT_LABEL}"
    agent_path = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
    status = _launchctl("print", target)
    if status.returncode == 0:
        _require_success(_launchctl("bootout", target))
    elif "Could not find service" not in status.stderr:
        _require_success(status)

    if not settings.schedule_enabled:
        agent_path.unlink(missing_ok=True)
        return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = agent_path.with_suffix(".tmp")
    try:
        with temporary.open("wb") as destination:
            plistlib.dump(launch_agent_payload(settings, settings_path), destination)
        temporary.replace(agent_path)
    finally:
        temporary.unlink(missing_ok=True)
    _require_success(_launchctl("bootstrap", domain, str(agent_path)))


@contextmanager
def sync_lock(settings_path: Path) -> Iterator[None]:
    """Serialize manual and launchd syncs within this macOS user profile."""
    if sys.platform != "darwin":
        yield
        return
    import fcntl

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with (settings_path.parent / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OSError("Another sync is already running.") from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def scheduled_sync(settings_path: Path) -> int:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nas_tool.scheduled")
    handler = logging.FileHandler(settings_path.parent / "sync.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with sync_lock(settings_path):
            settings = load_settings(settings_path)
            if not settings.schedule_enabled:
                logger.info("Scheduled sync is disabled; no files copied.")
                return 0
            copied = sync_a_to_b(Path(settings.bank_a), Path(settings.bank_b))
            logger.info("Sync complete: copied/updated %s file(s) from A to B.", len(copied))
        return 0
    except (OSError, ValueError) as error:
        logger.exception("Scheduled sync failed: %s", error)
        print(f"Scheduled sync failed: {error}", file=sys.stderr)
        return 1
    finally:
        logger.removeHandler(handler)
        handler.close()


def _path_key(relative_path: str) -> str:
    return relative_path.casefold()


def _rounded_rectangle(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **options: object,
) -> int:
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **options)


class RoundedSurface(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        fill: str = PANEL,
        outline: str = SOFT_BORDER,
        radius: int = 12,
        padding: int = 12,
        border_width: float = 1.3,
    ) -> None:
        super().__init__(master, bg=BG, highlightthickness=0, bd=0)
        self._fill = fill
        self._outline = outline
        self._radius = radius
        self._padding = padding
        self._border_width = border_width
        self.inner = tk.Frame(self, bg=fill)
        self._window_id = self.create_window(
            padding, padding, anchor="nw", window=self.inner
        )
        self.bind("<Configure>", self._resize)

    def _resize(self, event: tk.Event) -> None:
        self.delete("surface")
        _rounded_rectangle(
            self,
            1,
            1,
            event.width - 1,
            event.height - 1,
            self._radius,
            fill=self._fill,
            outline=self._outline,
            width=self._border_width,
            tags="surface",
        )
        self.tag_lower("surface")
        self.coords(self._window_id, self._padding, self._padding)
        self.itemconfigure(
            self._window_id,
            width=max(1, event.width - self._padding * 2),
            height=max(1, event.height - self._padding * 2),
        )


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: object,
        *,
        width: int = 142,
        height: int = 38,
        primary: bool = False,
        blue: bool = False,
        orange: bool = False,
        danger: bool = False,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=master.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._text = text
        self._command = command
        self._primary = primary
        self._blue = blue
        self._orange = orange
        self._danger = danger
        self._hovered = False
        self._pressed = False
        self._enabled = True
        self._draw(width, height)
        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def configure(self, **options: object) -> None:
        state = options.pop("state", None)
        if state is not None:
            self._enabled = state != "disabled"
            self.configure(cursor="hand2" if self._enabled else "arrow")
            self._draw(self.winfo_width(), self.winfo_height())
        if options:
            super().configure(**options)

    config = configure

    def _draw(self, width: int, height: int) -> None:
        if width <= 1 or height <= 1:
            return
        if not self._enabled:
            if self._orange:
                fill, foreground, outline = "#f5b66b", TEXT, ACCENT
            else:
                fill, foreground, outline = PANEL_ALT, MUTED, SOFT_BORDER
        elif self._blue:
            fill = "#1d4ed8" if self._hovered or self._pressed else "#0000ff"
            foreground, outline = BG, "#0000ff"
        elif self._orange:
            fill = "#a96720" if self._hovered or self._pressed else ACCENT
            foreground, outline = BG, ACCENT
        elif self._primary:
            fill = "#254955" if self._hovered or self._pressed else BORDER
            foreground, outline = BG, BORDER
        elif self._danger:
            fill = "#ffe8e6" if self._hovered or self._pressed else "#fff7f7"
            foreground, outline = RED, "#f4c7c3"
        else:
            fill = "#e8e3da" if self._hovered or self._pressed else PANEL_ALT
            foreground, outline = TEXT, SOFT_BORDER

        self.delete("all")
        _rounded_rectangle(
            self,
            1,
            1,
            width - 1,
            height - 1,
            7,
            fill=fill,
            outline=outline,
            width=1.2,
        )
        self.create_text(
            width / 2,
            height / 2,
            text=self._text,
            fill=foreground,
            font=("Consolas", 10, "bold"),
        )

    def _on_resize(self, event: tk.Event) -> None:
        self._draw(event.width, event.height)

    def _on_enter(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._hovered = True
        self._draw(self.winfo_width(), self.winfo_height())

    def _on_leave(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._hovered = False
        self._pressed = False
        self._draw(self.winfo_width(), self.winfo_height())

    def _on_press(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._enabled:
            self._pressed = True
            self._draw(self.winfo_width(), self.winfo_height())

    def _on_release(self, event: tk.Event) -> None:
        was_pressed = self._pressed
        self._pressed = False
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._hovered = inside
        self._draw(self.winfo_width(), self.winfo_height())
        if was_pressed and inside and self._enabled and callable(self._command):
            self._command()


class EmergencyStopButton(tk.Canvas):
    def __init__(self, master: tk.Misc, command: Callable[[], None]) -> None:
        super().__init__(
            master,
            width=44,
            height=44,
            bg=master.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._command = command
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", lambda _event: self._draw(False))
        self.bind("<Enter>", lambda _event: self._draw(True))
        self.bind("<Leave>", lambda _event: self._draw(False))
        self._draw(False)

    def _draw(self, active: bool) -> None:
        self.delete("all")
        fill, outline = (
            ("#990000", "#660000")
            if active
            else ("#cc0000", "#8b0000")
        )
        self.create_oval(
            2,
            2,
            42,
            42,
            fill=fill,
            outline=outline,
            width=2.5,
        )
        self.create_text(
            22,
            22,
            text="STOP",
            fill="#ffffff",
            font=("Consolas", 8, "bold"),
        )

    def _on_press(self, event: tk.Event) -> str:
        if event.widget is self:
            self._command()
            self._draw(True)
        return "break"


def _file_map(
    root: Path, *, cancelled: CancelCheck | None = None
) -> dict[str, Path]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Folder does not exist or cannot be accessed: {root}")

    files: dict[str, Path] = {}
    def scan_failed(error: OSError) -> None:
        raise error

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=scan_failed):
        _raise_if_cancelled(cancelled)
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            _raise_if_cancelled(cancelled)
            path = current_path / filename
            if path.is_symlink() or not path.is_file():
                continue
            relative_path = path.relative_to(root).as_posix()
            key = _path_key(relative_path)
            if key in files:
                raise ValueError(
                    f"Case-insensitive duplicate file path: {relative_path}"
                )
            files[key] = path
    return files


def _validate_roots(root_a: Path, root_b: Path) -> tuple[Path, Path]:
    resolved_a = root_a.expanduser().resolve()
    resolved_b = root_b.expanduser().resolve()
    if not resolved_a.is_dir() or not resolved_b.is_dir():
        raise ValueError("Select two valid folders.")
    if (
        resolved_a == resolved_b
        or resolved_a.is_relative_to(resolved_b)
        or resolved_b.is_relative_to(resolved_a)
    ):
        raise ValueError("Bank-A and Bank-B must be separate folders and cannot contain each other.")
    return resolved_a, resolved_b


def compare_directories(root_a: Path, root_b: Path) -> tuple[list[str], list[str]]:
    """Return files exclusive to A and files exclusive to B."""
    root_a, root_b = _validate_roots(root_a, root_b)
    files_a = _file_map(root_a)
    files_b = _file_map(root_b)
    only_a = sorted(
        (path.relative_to(root_a).as_posix() for key, path in files_a.items() if key not in files_b),
        key=str.casefold,
    )
    only_b = sorted(
        (path.relative_to(root_b).as_posix() for key, path in files_b.items() if key not in files_a),
        key=str.casefold,
    )
    return only_a, only_b


def _file_digest(path: Path, cancelled: CancelCheck | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            _raise_if_cancelled(cancelled)
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_replacing(
    source: Path, destination: Path, cancelled: CancelCheck | None
) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".nas-tool-part",
            delete=False,
        ) as target:
            temporary_path = Path(target.name)
            with source.open("rb") as incoming:
                while True:
                    _raise_if_cancelled(cancelled)
                    chunk = incoming.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    target.write(chunk)
        _raise_if_cancelled(cancelled)
        shutil.copystat(source, temporary_path)
        _raise_if_cancelled(cancelled)
        temporary_path.replace(destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def sync_a_to_b(
    root_a: Path,
    root_b: Path,
    *,
    relative_paths: list[str] | None = None,
    cancelled: CancelCheck | None = None,
) -> list[str]:
    """Copy all or selected files from the first root to the second, without deletions."""
    root_a, root_b = _validate_roots(root_a, root_b)
    files_a = _file_map(root_a, cancelled=cancelled)
    if relative_paths is not None:
        selected: dict[str, Path] = {}
        for relative_path in relative_paths:
            _raise_if_cancelled(cancelled)
            relative = Path(relative_path)
            if relative.is_absolute() or relative.drive or ".." in relative.parts:
                raise ValueError(f"Invalid relative file path: {relative_path}")
            key = _path_key(relative.as_posix())
            if key not in files_a:
                raise FileNotFoundError(f"Selected file does not exist: {root_a / relative}")
            selected[key] = files_a[key]
        files_a = selected
    files_b = _file_map(root_b, cancelled=cancelled)
    copied: list[str] = []

    for key, source in files_a.items():
        _raise_if_cancelled(cancelled)
        relative_path = source.relative_to(root_a)
        destination = root_b / relative_path
        if (
            key in files_b
            and _file_digest(source, cancelled) == _file_digest(files_b[key], cancelled)
        ):
            continue

        if destination.is_symlink() or not destination.parent.resolve().is_relative_to(root_b):
            raise ValueError(f"Unsafe destination path: {relative_path}")
        if relative_paths is not None and destination.is_dir():
            raise ValueError(f"A folder already occupies the sync destination: {relative_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.resolve().is_relative_to(root_b):
            raise ValueError(f"Destination path is outside the destination bank: {relative_path}")
        _copy_replacing(source, destination, cancelled)
        copied.append(relative_path.as_posix())
    _raise_if_cancelled(cancelled)
    return copied


def delete_selected_files(root: Path, relative_paths: list[str]) -> list[str]:
    """Delete selected files under a bank root; never delete directories."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Folder does not exist or cannot be accessed: {root}")

    targets: list[tuple[str, Path]] = []
    for relative_path in relative_paths:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid relative file path: {relative_path}")
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError(f"Target file is outside the selected folder: {relative_path}")
        if not target.is_file():
            raise FileNotFoundError(f"File does not exist: {target}")
        targets.append((relative.as_posix(), target))

    for _, target in targets:
        target.unlink()
    return [relative_path for relative_path, _ in targets]


def upload_to_bank_a(
    source_root: Path,
    bank_a: Path,
    *,
    cancelled: CancelCheck | None = None,
) -> tuple[int, int]:
    source_root, bank_a = _validate_roots(source_root, bank_a)
    sources = _file_map(source_root, cancelled=cancelled)
    existing = _file_map(bank_a, cancelled=cancelled)
    copied = skipped = 0
    for key, source in sources.items():
        _raise_if_cancelled(cancelled)
        relative = source.relative_to(source_root)
        destination = bank_a / relative
        if key in existing:
            skipped += 1
            continue
        if destination.is_symlink() or not destination.parent.resolve().is_relative_to(bank_a):
            raise ValueError(f"Unsafe upload destination: {relative}")
        if destination.is_dir():
            raise ValueError(f"A folder already occupies the upload destination: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            target = destination.open("xb")
        except FileExistsError:
            skipped += 1
            continue
        try:
            with target, source.open("rb") as incoming:
                while True:
                    _raise_if_cancelled(cancelled)
                    chunk = incoming.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    target.write(chunk)
            _raise_if_cancelled(cancelled)
            shutil.copystat(source, destination)
            _raise_if_cancelled(cancelled)
        except (OSError, OperationCancelled):
            destination.unlink(missing_ok=True)
            raise
        copied += 1
    _raise_if_cancelled(cancelled)
    return copied, skipped


IMAGE_CAPTURE_DESTINATION_SCRIPT = """
set destinationPath to system attribute "NAS_TOOL_IMAGE_CAPTURE_DESTINATION"
set destinationName to name of (info for (POSIX file destinationPath))
    tell application "System Events"
        set captureProcess to missing value
        repeat 100 times
            try
                set captureProcess to first application process whose bundle identifier is "com.apple.Image_Capture"
            on error
                set captureProcess to missing value
            end try
            if captureProcess is not missing value then exit repeat
            delay 0.1
        end repeat
        if captureProcess is missing value then
            error "Image Capture did not start."
        end if
        tell captureProcess
            set frontmost to true
            delay 0.5
            repeat 100 times
                if exists window 1 then exit repeat
                delay 0.1
            end repeat
            if not (exists window 1) then error "Image Capture has no import window."
            if exists sheet 1 of window 1 then
                error "Close the open Image Capture dialog, then click Download in NAS Tool again."
            end if
            set destinationPopup to missing value
            repeat 50 times
                set windowElements to entire contents of window 1
                repeat with candidate in windowElements
                    if class of candidate is pop up button then
                        if enabled of candidate then
                            click candidate
                            delay 0.1
                            if exists menu 1 of candidate then
                                set choices to menu items of menu 1 of candidate
                                repeat with otherChoice in choices
                                    set choiceName to name of otherChoice
                                    if choiceName ends with "..." or choiceName ends with (character id 8230) then
                                        click otherChoice
                                        set destinationPopup to contents of candidate
                                        exit repeat
                                    end if
                                end repeat
                            end if
                            if destinationPopup is not missing value then exit repeat
                            key code 53
                        end if
                    end if
                end repeat
                if destinationPopup is not missing value then exit repeat
                delay 0.1
            end repeat
            if destinationPopup is missing value then
                error "Cannot find Import To > Other in Image Capture. Connect and select your device, then click Download in NAS Tool again."
            end if
            repeat 50 times
                if exists sheet 1 of window 1 then exit repeat
                delay 0.1
            end repeat
            if not (exists sheet 1 of window 1) then error "Image Capture did not open the destination folder chooser."
            keystroke "g" using {command down, shift down}
            repeat 50 times
                if exists sheet 1 of sheet 1 of window 1 then exit repeat
                delay 0.1
            end repeat
            if not (exists sheet 1 of sheet 1 of window 1) then error "Cannot open Go to Folder in Image Capture."
            delay 0.3
            keystroke "a" using command down
            keystroke destinationPath
            key code 36
            repeat 50 times
                if not (exists sheet 1 of sheet 1 of window 1) then exit repeat
                delay 0.1
            end repeat
            if exists sheet 1 of sheet 1 of window 1 then error "Cannot navigate to the Bank-A folder."
            delay 0.3
            key code 36
            repeat 50 times
                if not (exists sheet 1 of window 1) then exit repeat
                delay 0.1
            end repeat
            if exists sheet 1 of window 1 then error "Cannot confirm the Bank-A destination folder."
            set selectedDestination to value of destinationPopup
            if selectedDestination is not destinationName and selectedDestination is not destinationPath then
                error "Image Capture did not confirm Bank-A as its import destination."
            end if
        end tell
    end tell
"""


def execute_applescript(script_source: str) -> None:
    if sys.platform != "darwin":
        raise ValueError("AppleScript is available on macOS only.")
    try:
        from Foundation import NSAppleScript
    except ImportError as error:
        raise OSError(
            "The macOS AppleScript bridge is missing. Reinstall NAS Tool from its DMG."
        ) from error

    script = NSAppleScript.alloc().initWithSource_(script_source)
    if script is None:
        raise OSError("Unable to compile the AppleScript automation script.")
    _result, error = script.executeAndReturnError_(None)
    if error:
        raise OSError(str(error.get("NSAppleScriptErrorMessage") or error))


def request_system_events_automation() -> None:
    if sys.platform != "darwin":
        raise ValueError("System Events automation is available on macOS only.")

    try:
        launch = subprocess.run(
            ["/usr/bin/open", "-a", "System Events"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("Timed out starting System Events.") from error
    if launch.returncode:
        raise OSError(
            launch.stderr.strip() or launch.stdout.strip()
            or "Unable to start System Events."
        )

    class AppleEventDescriptor(ctypes.Structure):
        _fields_ = [("descriptor_type", ctypes.c_uint32), ("data_handle", ctypes.c_void_p)]

    try:
        apple_events = ctypes.CDLL(
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/AE.framework/AE"
        )
    except OSError as error:
        raise OSError(f"Unable to load the macOS Apple Events framework: {error}") from error

    create_descriptor = apple_events.AECreateDesc
    create_descriptor.argtypes = [
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.POINTER(AppleEventDescriptor),
    ]
    create_descriptor.restype = ctypes.c_int16
    determine_permission = apple_events.AEDeterminePermissionToAutomateTarget
    determine_permission.argtypes = [
        ctypes.POINTER(AppleEventDescriptor),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint8,
    ]
    determine_permission.restype = ctypes.c_int32
    dispose_descriptor = apple_events.AEDisposeDesc
    dispose_descriptor.argtypes = [ctypes.POINTER(AppleEventDescriptor)]
    dispose_descriptor.restype = ctypes.c_int16

    system_events_bundle = b"com.apple.systemevents"
    target = AppleEventDescriptor()
    result = create_descriptor(
        int.from_bytes(b"bund", "big"),
        system_events_bundle,
        len(system_events_bundle),
        ctypes.byref(target),
    )
    if result:
        raise OSError(f"Unable to identify System Events for automation (error {result}).")

    wildcard = int.from_bytes(b"****", "big")
    try:
        deadline = time.monotonic() + 10
        while True:
            status = determine_permission(
                ctypes.byref(target), wildcard, wildcard, False
            )
            if status != -600:
                break
            if time.monotonic() >= deadline:
                raise OSError("System Events did not start in time to request automation access.")
            time.sleep(0.1)
        if status == -1744:
            status = determine_permission(
                ctypes.byref(target), wildcard, wildcard, True
            )
        if status == -1743:
            raise OSError(
                "Not authorized to send Apple events to System Events. (-1743). "
                "Enable NAS Tool in System Settings > Privacy & Security > Automation."
            )
        if status:
            raise OSError(f"Unable to request System Events automation access (error {status}).")
    finally:
        dispose_descriptor(ctypes.byref(target))


def open_image_capture(bank_a: Path) -> None:
    if sys.platform != "darwin":
        raise ValueError("Image Capture import is available on macOS only.")
    bank_a = bank_a.expanduser().resolve()
    if not bank_a.is_dir():
        raise ValueError(f"Bank-A folder does not exist or cannot be accessed: {bank_a}")
    request_system_events_automation()
    try:
        launch = subprocess.run(
            ["/usr/bin/open", "-a", "Image Capture"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("Timed out launching Image Capture.") from error
    if launch.returncode:
        raise OSError(
            launch.stderr.strip() or launch.stdout.strip()
            or f"Unable to launch Image Capture (exit {launch.returncode})."
        )
    environment_key = "NAS_TOOL_IMAGE_CAPTURE_DESTINATION"
    previous_destination = os.environ.get(environment_key)
    os.environ[environment_key] = str(bank_a)
    try:
        execute_applescript(IMAGE_CAPTURE_DESTINATION_SCRIPT)
    finally:
        if previous_destination is None:
            os.environ.pop(environment_key, None)
        else:
            os.environ[environment_key] = previous_destination


def stop_image_capture() -> None:
    if sys.platform != "darwin":
        raise ValueError("Image Capture is available on macOS only.")
    execute_applescript('tell application "Image Capture" to quit')


def open_accessibility_settings() -> None:
    if sys.platform != "darwin":
        raise ValueError("Accessibility settings are available on macOS only.")
    try:
        result = subprocess.run(
            [
                "/usr/bin/open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("Timed out opening macOS Accessibility privacy settings.") from error
    if result.returncode:
        raise OSError(
            result.stderr.strip() or result.stdout.strip()
            or "Unable to open macOS Accessibility privacy settings."
        )


def open_automation_settings() -> None:
    if sys.platform != "darwin":
        raise ValueError("Automation settings are available on macOS only.")
    try:
        result = subprocess.run(
            [
                "/usr/bin/open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("Timed out opening macOS Automation privacy settings.") from error
    if result.returncode:
        raise OSError(
            result.stderr.strip() or result.stdout.strip()
            or "Unable to open macOS Automation privacy settings."
        )


class ToggleSwitch(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        variable: tk.BooleanVar,
        command: Callable[[], None],
    ) -> None:
        super().__init__(
            master, width=46, height=26, bg=PANEL, highlightthickness=0,
            takefocus=True, cursor="hand2",
        )
        self.variable = variable
        self.command = command
        self._trace = variable.trace_add("write", self._draw)
        self.bind("<Button-1>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)
        self._draw()

    def _draw(self, *_args: object) -> None:
        self.delete("all")
        enabled = self.variable.get()
        _rounded_rectangle(
            self, 2, 3, 44, 23, 10,
            fill="#059669" if enabled else "#9ca3af",
            outline=BORDER if self.focus_get() is self else "",
            width=1,
        )
        left = 25 if enabled else 5
        self.create_oval(left, 5, left + 16, 21, fill=BG, outline="")

    def invoke(self) -> None:
        self.variable.set(not self.variable.get())
        self.command()

    def _activate(self, event: tk.Event) -> str:
        if event.widget is self:
            self.focus_set()
            self.invoke()
        return "break"

    def destroy(self) -> None:
        self.variable.trace_remove("write", self._trace)
        super().destroy()


class NasTool:
    def __init__(self, root: tk.Tk, settings_path: Path | None = None) -> None:
        self.root = root
        self.settings_path = settings_path or default_settings_path()
        self.settings = Settings()
        self.upload_folder: Path | None = None
        self.bank_paths: dict[str, Path | None] = {"A": None, "B": None}
        self.results: dict[str, list[str]] = {"A": [], "B": []}
        self.preview_images: dict[str, ImageTk.PhotoImage | None] = {"A": None, "B": None}
        self.preview_sources: dict[str, Image.Image | None] = {"A": None, "B": None}
        self.preview_scrollbars: dict[str, tk.Scrollbar] = {}
        self.result_lists: dict[str, tk.Listbox] = {}
        self.path_labels: dict[str, tk.Label] = {}
        self.result_headers: dict[str, tk.Label] = {}
        self.preview_texts: dict[str, tk.Text] = {}
        self.preview_image_labels: dict[str, tk.Canvas] = {}
        self.folder_buttons: dict[str, tuple[RoundedButton, RoundedButton]] = {}
        self._cancel_event = threading.Event()
        self._task_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._task_thread: threading.Thread | None = None
        self._task_on_success: Callable[[object], None] | None = None
        self._task_error_title = ""
        self._task_clear_on_error = False
        self._image_capture_active = False
        self._help_window: tk.Toplevel | None = None

        self._build_ui()
        self._restore_settings()

    def _build_ui(self) -> None:
        self.root.configure(background=BG)
        self.root.title(f"NAS Tool v{get_app_version()}")
        self.root.geometry("1080x780")
        self.root.minsize(920, 680)
        self._build_menu()

        shell = RoundedSurface(
            self.root,
            fill=PANEL,
            outline=BORDER,
            radius=24,
            padding=20,
            border_width=2.0,
        )
        shell.pack(fill="both", expand=True, padx=14, pady=12)
        content = shell.inner
        content.columnconfigure(0, weight=1, minsize=220)
        content.columnconfigure(1, weight=2, minsize=330)
        content.columnconfigure(2, weight=1, minsize=260)
        content.rowconfigure(2, weight=1, minsize=210)
        content.rowconfigure(3, weight=1, minsize=210)

        header = tk.Frame(content, bg=PANEL)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(5, 10))
        upload = tk.Frame(header, bg=PANEL)
        upload.pack(side="left")
        RoundedButton(
            upload, "DOWNLOAD", self._upload, width=125, height=30, primary=True,
        ).pack(side="left")
        RoundedButton(
            upload, "SELECT FOLDER", self._select_upload_folder, width=125, height=30,
        ).pack(side="left", padx=(6, 0))
        self.upload_path_label = tk.Label(
            content,
            text="No upload folder selected",
            bg=PANEL,
            fg=MUTED,
            font=("Consolas", 8),
            anchor="w",
            justify="left",
            wraplength=780,
        )
        self.upload_path_label.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8)
        )

        self._build_bank_row(content, "A", 2)
        self._build_bank_row(content, "B", 3)

        control_groups: list[tk.Frame] = []
        for column, padding in enumerate(((0, 8), (5, 5), (5, 0))):
            controls = tk.Frame(content, bg=PANEL)
            controls.grid(
                row=4, column=column, sticky="ew", padx=padding, pady=(16, 12)
            )
            group = tk.Frame(controls, bg=PANEL)
            group.pack(anchor="center")
            control_groups.append(group)
        RoundedButton(
            control_groups[0], "SYNC  A → B", self._sync,
            width=125, height=30, primary=True,
        ).pack()
        RoundedButton(
            control_groups[1], "COMPARE", self._compare,
            width=125, height=30, blue=True,
        ).pack()
        self.sync_selected_button = RoundedButton(
            control_groups[2], "SYNC SELECTED", self._sync_selected,
            width=125, height=30, primary=True,
        )
        self.sync_selected_button.pack(side="left", padx=(0, 6))
        self.delete_button = RoundedButton(
            control_groups[2], "DELETE SELECTED", self._delete,
            width=125, height=30, orange=True,
        )
        self.delete_button.pack(side="left")
        stop_controls = tk.Frame(content, bg=PANEL)
        stop_controls.grid(
            row=5, column=0, columnspan=3, sticky="e", pady=(0, 2)
        )
        self.stop_button = EmergencyStopButton(stop_controls, self._stop)
        self.stop_button.pack()

        schedule = tk.Frame(header, bg=PANEL)
        schedule.pack(side="right")
        self.schedule_enabled = tk.BooleanVar(value=False)
        self.sync_hour = tk.StringVar(value="09")
        self.sync_minute = tk.StringVar(value="00")
        self.sync_cycle = tk.StringVar(value="daily")
        tk.Label(
            schedule, text="Scheduled Sync", bg=PANEL, fg=TEXT,
            font=("Consolas", 9),
        ).pack(side="left", padx=(0, 5))
        self.schedule_switch = ToggleSwitch(
            schedule, self.schedule_enabled, self._apply_schedule,
        )
        self.schedule_switch.pack(side="left")
        tk.Label(
            schedule, text="Time", bg=PANEL, fg=MUTED, font=("Consolas", 9)
        ).pack(side="left", padx=(6, 4))
        time_fields = tk.Frame(schedule, bg=PANEL)
        time_fields.pack(side="left")
        tk.Spinbox(
            time_fields,
            from_=0,
            to=23,
            wrap=True,
            format="%02.0f",
            textvariable=self.sync_hour,
            width=3,
            bg=BG,
            fg=TEXT,
            font=("Consolas", 9),
        ).pack(side="left")
        tk.Label(
            time_fields, text=" / ", bg=PANEL, fg=MUTED, font=("Consolas", 9)
        ).pack(side="left")
        tk.Spinbox(
            time_fields, from_=0, to=59, wrap=True, format="%02.0f",
            textvariable=self.sync_minute, width=3,
            bg=BG, fg=TEXT, font=("Consolas", 9),
        ).pack(side="left")
        tk.Label(
            schedule, text="Cycle", bg=PANEL, fg=MUTED, font=("Consolas", 9)
        ).pack(side="left", padx=(8, 4))
        ttk.Combobox(
            schedule, textvariable=self.sync_cycle,
            values=("daily", "weekly", "monthly"),
            state="readonly", width=7, font=("Consolas", 9),
        ).pack(side="left")

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(
            label="User Guide",
            command=self._show_help,
            accelerator="F1",
        )
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menu)
        self.root.bind("<F1>", lambda _event: self._show_help())

    def _show_help(self) -> None:
        if self._help_window is not None and self._help_window.winfo_exists():
            self._help_window.lift()
            self._help_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self._help_window = window
        window.title("NAS Tool User Guide")
        window.geometry("720x620")
        window.minsize(560, 420)
        window.configure(bg=PANEL)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", window.destroy)

        content = tk.Frame(window, bg=PANEL)
        content.pack(fill="both", expand=True, padx=18, pady=18)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        guide = tk.Text(
            content,
            wrap="word",
            bg=BG,
            fg=TEXT,
            font=("Consolas", 10),
            padx=14,
            pady=14,
            relief="flat",
            highlightthickness=1,
            highlightbackground=SOFT_BORDER,
        )
        guide.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(content, orient="vertical", command=guide.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        guide.configure(yscrollcommand=scrollbar.set)
        guide.insert("1.0", HELP_TEXT)
        guide.configure(state="disabled")
        ttk.Button(window, text="Close", command=window.destroy).pack(
            pady=(0, 16)
        )

    def _start_task(
        self,
        worker: Callable[[CancelCheck], object],
        on_success: Callable[[object], None],
        *,
        error_title: str,
        clear_on_error: bool = False,
    ) -> None:
        if self._task_thread is not None and self._task_thread.is_alive():
            return
        self._cancel_event.clear()
        self._task_on_success = on_success
        self._task_error_title = error_title
        self._task_clear_on_error = clear_on_error

        def run() -> None:
            try:
                result = worker(self._cancel_event.is_set)
            except OperationCancelled:
                self._task_results.put(("cancelled", None))
            except (OSError, ValueError) as error:
                self._task_results.put(("error", error))
            else:
                self._task_results.put(("success", result))

        self._task_thread = threading.Thread(target=run, daemon=True)
        self._task_thread.start()
        self.root.after(25, self._poll_task)

    def _poll_task(self) -> None:
        try:
            status, payload = self._task_results.get_nowait()
        except queue.Empty:
            if self._task_thread is not None:
                self.root.after(25, self._poll_task)
            return

        on_success = self._task_on_success
        self._task_thread = None
        self._task_on_success = None
        if status == "success" and on_success is not None:
            on_success(payload)
        elif status == "error":
            if self._task_clear_on_error:
                self._clear_results()
            error_message = str(payload)
            if (
                "-1743" in error_message
                or "Not authorized to send Apple events to System Events" in error_message
            ):
                error_message += (
                    "\n\nOpen System Settings > Privacy & Security > Automation "
                    "and enable NAS Tool for System Events. This list has no "
                    "Add button; macOS lists an app after it requests access. "
                    "NAS Tool requests this access when you click Download. "
                    "Launch the newly built NAS Tool.app, click Download, and "
                    "allow the macOS Automation prompt. This permission is "
                    "separate from Accessibility."
                )
                try:
                    open_automation_settings()
                except (OSError, ValueError) as error:
                    error_message += f"\n\nCould not open settings automatically: {error}"
            if (
                "Allow NAS Tool (or its Python host)" in error_message
                or "not allowed assistive access" in error_message.lower()
            ):
                if "not allowed assistive access" in error_message.lower():
                    error_message += (
                        "\n\nOpen System Settings > Privacy & Security > Accessibility "
                        "and enable the current NAS Tool.app. If an older NAS Tool "
                        "entry is already enabled, remove that entry, click +, and "
                        "add the NAS Tool.app you are currently launching. Quit and "
                        "reopen NAS Tool, then click Download again."
                    )
                else:
                    error_message += (
                        "\n\nOpen System Settings > Privacy & Security > Accessibility "
                        "(the privacy permission list, not the main Accessibility page).\n"
                        "Enable the app that launches NAS Tool: NAS Tool for an app "
                        "bundle, Terminal/iTerm for a terminal launch, or Visual Studio "
                        "Code for a VS Code launch. Use the app named by macOS if "
                        "it identifies a different host.\n"
                        "If the app is absent, click + and add that application. "
                        "Unlock the settings if required, then quit and reopen the "
                        "launching app and NAS Tool. Click Download again.\n"
                        "Allow Automation access to System Events and Image Capture "
                        "if macOS prompts for it."
                    )
                try:
                    open_accessibility_settings()
                except (OSError, ValueError) as error:
                    error_message += f"\n\nCould not open settings automatically: {error}"
            messagebox.showerror(self._task_error_title, error_message)

    def _stop(self) -> None:
        if self._task_thread is not None and self._task_thread.is_alive():
            self._cancel_event.set()
        if self._image_capture_active:
            try:
                stop_image_capture()
            except (OSError, ValueError) as error:
                messagebox.showerror("Stop Failed", str(error))
            else:
                self._image_capture_active = False

    def _select_upload_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Select Upload Folder")
        if not chosen:
            return
        path = Path(chosen)
        if not path.is_dir():
            messagebox.showerror("Invalid Folder", "The selected path is not a valid folder.")
            return
        self.upload_folder = path
        self.upload_path_label.configure(text=str(path))

    def _upload(self) -> None:
        try:
            bank_a = self.bank_paths["A"]
            if bank_a is None:
                raise ValueError("Select a Bank-A folder first.")
            if self.upload_folder is None:
                def configure_destination(cancelled: CancelCheck) -> object:
                    _raise_if_cancelled(cancelled)
                    open_image_capture(bank_a)
                    if cancelled():
                        stop_image_capture()
                        raise OperationCancelled()
                    return None

                def destination_configured(_result: object) -> None:
                    self._image_capture_active = True
                    messagebox.showinfo(
                        "Import with Image Capture",
                        f"Image Capture's import destination is set to Bank-A:\n"
                        f"{bank_a.resolve()}\n\n"
                        "Choose the photos and click Download / Import in Image Capture.\n"
                        "If you switch devices, click Download in NAS Tool again "
                        "to set that device's destination.\n\n"
                        "Click Compare after the import finishes to refresh the results.\n"
                        "Image Capture controls duplicate handling; the folder upload "
                        "skip-existing policy does not apply here.",
                    )

                self._start_task(
                    configure_destination,
                    destination_configured,
                    error_title="Download Failed",
                    clear_on_error=True,
                )
                return
        except (OSError, ValueError) as error:
            self._clear_results()
            messagebox.showerror("Download Failed", str(error))
            return

        upload_folder = self.upload_folder
        if upload_folder is None:
            return

        def download(cancelled: CancelCheck) -> object:
            with sync_lock(self.settings_path):
                return upload_to_bank_a(
                    upload_folder, bank_a, cancelled=cancelled
                )

        def download_complete(_result: object) -> None:
            self._clear_results()
            if self.bank_paths["B"] is not None and self.bank_paths["B"].is_dir():
                self._compare()

        self._start_task(
            download,
            download_complete,
            error_title="Download Failed",
            clear_on_error=True,
        )

    def _restore_settings(self) -> None:
        try:
            self.settings = load_settings(self.settings_path)
        except (OSError, ValueError) as error:
            messagebox.showerror("Settings Error", str(error))
            return
        self.bank_paths = {
            "A": Path(self.settings.bank_a) if self.settings.bank_a else None,
            "B": Path(self.settings.bank_b) if self.settings.bank_b else None,
        }
        for bank, path in self.bank_paths.items():
            self.path_labels[bank].configure(text=str(path) if path else "No folder selected")
        self.sync_hour.set(f"{self.settings.sync_hour:02d}")
        self.sync_minute.set(f"{self.settings.sync_minute:02d}")
        self.sync_cycle.set(self.settings.sync_cycle)
        self.schedule_enabled.set(self.settings.schedule_enabled)
        unavailable = [bank for bank, path in self.bank_paths.items() if path and not path.is_dir()]
        if unavailable:
            messagebox.showwarning(
                "Folder Unavailable",
                f"Saved folder unavailable: Bank-{', Bank-'.join(unavailable)}. "
                "Mount the storage first.",
            )

    def _apply_schedule(self) -> None:
        try:
            enabled = self.schedule_enabled.get()
            if enabled:
                candidate = replace(
                    self.settings,
                    schedule_enabled=True,
                    sync_hour=int(self.sync_hour.get()),
                    sync_minute=int(self.sync_minute.get()),
                    sync_cycle=self.sync_cycle.get(),
                )
            else:
                candidate = replace(self.settings, schedule_enabled=False)
            candidate.validate()
            if enabled:
                self._get_roots()
            if sys.platform != "darwin":
                raise ValueError("Background scheduled sync requires macOS.")
            # Pause the worker until launchd registration and saving both succeed.
            with sync_lock(self.settings_path):
                paused = replace(candidate, schedule_enabled=False)
                save_settings(self.settings_path, paused)
                self.settings = paused
                apply_schedule(candidate, self.settings_path)
                save_settings(self.settings_path, candidate)
                self.settings = candidate
        except (OSError, ValueError) as error:
            self.schedule_enabled.set(self.settings.schedule_enabled)
            messagebox.showerror("Schedule Failed", str(error))
            return

    def _build_bank_row(self, parent: tk.Frame, bank: str, row: int) -> None:
        side = RoundedSurface(parent, padding=12)
        side.grid(row=row, column=0, sticky="nsew", padx=(0, 8), pady=7)
        side_content = side.inner
        side_content.columnconfigure(0, weight=1)
        side_content.rowconfigure(2, weight=1)
        tk.Label(
            side_content,
            text=f"BANK-{bank}",
            bg=PANEL,
            fg=TEXT,
            font=("Consolas", 15, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(2, 12))
        path_label = tk.Label(
            side_content,
            text="No folder selected",
            bg=PANEL,
            fg=MUTED,
            wraplength=175,
            justify="left",
            anchor="nw",
            font=("Consolas", 8),
        )
        path_label.grid(row=1, column=0, sticky="new")
        self.path_labels[bank] = path_label
        folder_buttons = tk.Frame(side_content, bg=PANEL)
        folder_buttons.grid(row=3, column=0, sticky="se")
        select_button = RoundedButton(
            folder_buttons,
            "SELECT FOLDER",
            lambda: self._select_folder(bank),
            width=114,
            height=34,
        )
        select_button.pack(side="left")
        open_button = RoundedButton(
            folder_buttons,
            "OPEN FOLDER",
            lambda: self._open_folder(bank),
            width=114,
            height=34,
        )
        open_button.pack(side="left", padx=(2, 0))
        self.folder_buttons[bank] = (select_button, open_button)

        result_frame = RoundedSurface(parent, padding=10)
        result_frame.grid(row=row, column=1, sticky="nsew", padx=5, pady=7)
        result_content = result_frame.inner
        result_content.rowconfigure(1, weight=1)
        result_content.columnconfigure(0, weight=1)
        result_header = tk.Label(
            result_content,
            text=f"ONLY IN BANK-{bank}  ·  0",
            bg=PANEL,
            fg=ACCENT,
            anchor="w",
            font=("Consolas", 10, "bold"),
        )
        result_header.grid(row=0, column=0, sticky="ew", pady=(2, 8))
        self.result_headers[bank] = result_header
        listing = tk.Listbox(
            result_content,
            selectmode=tk.EXTENDED,
            exportselection=False,
            font=("Consolas", 9),
            activestyle="none",
            bg=BG,
            fg=TEXT,
            selectbackground=BORDER,
            selectforeground=BG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=SOFT_BORDER,
            highlightcolor=ACCENT,
            borderwidth=0,
        )
        listing.grid(row=1, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            result_content,
            orient="vertical",
            command=listing.yview,
            bg=PANEL_ALT,
            troughcolor=PANEL,
            activebackground=ACCENT,
            relief="flat",
            borderwidth=0,
        )
        scrollbar.grid(row=1, column=1, sticky="ns")
        listing.configure(yscrollcommand=scrollbar.set)
        listing.bind(
            "<Button-1>",
            lambda event, selected=bank: self._toggle_result_selection(selected, event),
        )
        listing.bind("<B1-Motion>", lambda event: "break" if event.widget is listing else None)
        listing.bind(
            "<<ListboxSelect>>",
            lambda event, selected=bank: self._preview_from_event(selected, event),
        )
        self.result_lists[bank] = listing

        preview_frame = RoundedSurface(parent, padding=10)
        preview_frame.grid(row=row, column=2, sticky="nsew", padx=(5, 0), pady=7)
        preview_content = preview_frame.inner
        preview_content.columnconfigure(0, weight=1)
        preview_content.rowconfigure(1, weight=1)
        tk.Label(
            preview_content,
            text="PREVIEW",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Consolas", 10, "bold"),
        ).grid(row=0, column=0, sticky="ew", pady=(2, 8))
        preview_image = tk.Canvas(
            preview_content,
            bg=PANEL,
            width=1,
            height=1,
            highlightthickness=0,
        )
        preview_image.grid(row=1, column=0, sticky="nsew")
        preview_image.bind(
            "<Configure>",
            lambda event, selected=bank: self._resize_preview(selected, event),
        )
        preview_text = tk.Text(
            preview_content,
            wrap="word",
            state="disabled",
            font=("Consolas", 8),
            height=5,
            width=1,
            bg=BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=SOFT_BORDER,
            borderwidth=0,
        )
        preview_text.grid(row=1, column=0, sticky="nsew")
        preview_scroll = tk.Scrollbar(
            preview_content,
            orient="vertical",
            command=preview_text.yview,
            bg=PANEL_ALT,
            troughcolor=PANEL,
            activebackground=ACCENT,
            relief="flat",
            borderwidth=0,
        )
        preview_scroll.grid(row=1, column=1, sticky="ns")
        preview_text.configure(yscrollcommand=preview_scroll.set)
        self.preview_texts[bank] = preview_text
        self.preview_image_labels[bank] = preview_image
        self.preview_scrollbars[bank] = preview_scroll
        self._clear_preview(bank)

    def _select_folder(self, bank: str) -> None:
        chosen = filedialog.askdirectory(title=f"Select Bank-{bank} folder")
        if not chosen:
            return
        path = Path(chosen)
        if not path.is_dir():
            messagebox.showerror("Invalid Folder", "The selected path is not a valid folder.")
            return
        candidate = replace(
            self.settings,
            **{"bank_a" if bank == "A" else "bank_b": str(path.resolve())},
        )
        try:
            if candidate.schedule_enabled:
                _validate_roots(Path(candidate.bank_a), Path(candidate.bank_b))
            with sync_lock(self.settings_path):
                save_settings(self.settings_path, candidate)
        except (OSError, ValueError) as error:
            messagebox.showerror("Save Failed", str(error))
            return
        self.settings = candidate
        self.bank_paths[bank] = path
        self.path_labels[bank].configure(text=str(path))
        self._clear_results()

    def _open_folder(self, bank: str) -> None:
        path = self.bank_paths[bank]
        if path is None:
            messagebox.showinfo(
                "No Folder Selected", f"Select a folder for Bank-{bank} first."
            )
            return
        if not path.is_dir():
            messagebox.showerror(
                "Folder Unavailable", f"The selected Bank-{bank} folder is unavailable:\n{path}"
            )
            return
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=True)
            elif os.name == "nt":
                os.startfile(str(path))
            else:
                subprocess.run(["xdg-open", str(path)], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            messagebox.showerror("Open Folder Failed", str(error))

    def _get_roots(self) -> tuple[Path, Path]:
        root_a = self.bank_paths["A"]
        root_b = self.bank_paths["B"]
        if root_a is None or root_b is None:
            raise ValueError("Select a folder for both Bank-A and Bank-B.")
        return _validate_roots(root_a, root_b)

    def _compare(self) -> None:
        try:
            root_a, root_b = self._get_roots()
            only_a, only_b = compare_directories(root_a, root_b)
        except (OSError, ValueError) as error:
            messagebox.showerror("Compare failed", str(error))
            return

        self.results = {"A": only_a, "B": only_b}
        for bank, paths in self.results.items():
            self.result_headers[bank].configure(
                text=f"ONLY IN BANK-{bank}  ·  {len(paths)}"
            )
            listing = self.result_lists[bank]
            listing.delete(0, tk.END)
            for path in paths:
                listing.insert(tk.END, path)
            self._clear_preview(bank)

    def _sync(self) -> None:
        try:
            root_a, root_b = self._get_roots()
        except (OSError, ValueError) as error:
            messagebox.showerror("Sync failed", str(error))
            return

        def sync(cancelled: CancelCheck) -> object:
            with sync_lock(self.settings_path):
                return sync_a_to_b(root_a, root_b, cancelled=cancelled)

        self._start_task(
            sync,
            lambda _result: self._compare(),
            error_title="Sync failed",
        )

    def _sync_selected(self) -> None:
        selected = {
            bank: [self.results[bank][index] for index in self.result_lists[bank].curselection()]
            for bank in ("A", "B")
        }
        if not any(selected.values()):
            messagebox.showinfo(
                "Sync Selected", "Select one or more files from the comparison results."
            )
            return

        try:
            root_a, root_b = self._get_roots()
        except (OSError, ValueError) as error:
            self._clear_results()
            messagebox.showerror("Sync Selected failed", str(error))
            return

        def sync_selected(cancelled: CancelCheck) -> object:
            with sync_lock(self.settings_path):
                for bank, source, destination in (
                    ("A", root_a, root_b),
                    ("B", root_b, root_a),
                ):
                    _raise_if_cancelled(cancelled)
                    if selected[bank]:
                        sync_a_to_b(
                            source,
                            destination,
                            relative_paths=selected[bank],
                            cancelled=cancelled,
                        )
            return None

        self._start_task(
            sync_selected,
            lambda _result: self._compare(),
            error_title="Sync Selected failed",
            clear_on_error=True,
        )

    def _delete(self) -> None:
        selected: dict[str, list[str]] = {
            bank: [self.results[bank][index] for index in self.result_lists[bank].curselection()]
            for bank in ("A", "B")
        }
        selected = {bank: paths for bank, paths in selected.items() if paths}
        if not selected:
            messagebox.showinfo(
                "Delete Selected", "Select one or more files from the comparison results."
            )
            return

        descriptions = [
            f"Bank-{bank}:\n  " + "\n  ".join(paths)
            for bank, paths in selected.items()
        ]
        if not messagebox.askyesno(
            "Confirm deletion",
            "The following selected files will be permanently deleted:\n"
            + "\n".join(descriptions)
            + "\n\nThis action cannot be undone. Continue?",
        ):
            return

        try:
            for bank, paths in selected.items():
                root = self.bank_paths[bank]
                if root is None:
                    raise ValueError(f"No folder selected for Bank-{bank}.")
                delete_selected_files(root, paths)
        except (OSError, ValueError) as error:
            messagebox.showerror("Delete failed", str(error))
            return

        self._compare()

    def _preview(self, bank: str) -> None:
        selection = self.result_lists[bank].curselection()
        if not selection:
            self._clear_preview(bank)
            return

        root = self.bank_paths[bank]
        if root is None:
            return
        active = self.result_lists[bank].index(tk.ACTIVE)
        preview_index = active if active in selection else selection[0]
        relative_path = self.results[bank][preview_index]
        path = root / relative_path
        self._clear_preview(bank)
        try:
            if path.suffix.lower() in IMAGE_SUFFIXES:
                with Image.open(path) as source:
                    self.preview_sources[bank] = ImageOps.exif_transpose(source).convert("RGB")
                self.preview_texts[bank].grid_remove()
                self.preview_scrollbars[bank].grid_remove()
                canvas = self.preview_image_labels[bank]
                canvas.grid()
                self._render_preview_image(bank)
                return
            details = f"{relative_path}\nSize: {path.stat().st_size:,} bytes"
            if path.suffix.lower() in TEXT_SUFFIXES:
                with path.open("r", encoding="utf-8", errors="replace") as source:
                    content = source.read(TEXT_PREVIEW_LIMIT)
                details += "\n\n" + content
                if path.stat().st_size > len(content.encode("utf-8", errors="replace")):
                    details += "\n\n… (Preview truncated)"
            else:
                details += "\n\nPreview is not available for this file type."
            self._set_preview_text(bank, details)
        except (OSError, tk.TclError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            self._set_preview_text(bank, f"Unable to preview file:\n{error}")

    def _resize_preview(self, bank: str, event: tk.Event) -> None:
        if event.widget is self.preview_image_labels[bank]:
            self._render_preview_image(bank)

    def _render_preview_image(self, bank: str) -> None:
        source = self.preview_sources[bank]
        if source is None:
            return
        canvas = self.preview_image_labels[bank]
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        image = source.copy()
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=self.root)
        self.preview_images[bank] = photo
        canvas.delete("all")
        canvas.create_image(width / 2, height / 2, image=photo, anchor="center")

    def _toggle_result_selection(self, bank: str, event: tk.Event) -> str:
        listing = self.result_lists[bank]
        if event.widget is not listing or listing.size() == 0:
            return "break"
        index = listing.nearest(event.y)
        bounds = listing.bbox(index)
        if bounds is None or not bounds[1] <= event.y < bounds[1] + bounds[3]:
            return "break"
        listing.focus_set()
        listing.activate(index)
        if index in listing.curselection():
            listing.selection_clear(index)
        else:
            listing.selection_set(index)
        listing.event_generate("<<ListboxSelect>>")
        return "break"

    def _preview_from_event(self, bank: str, event: tk.Event) -> None:
        if event.widget is self.result_lists[bank]:
            self._preview(bank)

    def _set_preview_text(self, bank: str, text: str) -> None:
        self.preview_image_labels[bank].grid_remove()
        self.preview_scrollbars[bank].grid()
        widget = self.preview_texts[bank]
        widget.grid()
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _clear_preview(self, bank: str) -> None:
        self.preview_images[bank] = None
        self.preview_sources[bank] = None
        self.preview_image_labels[bank].delete("all")
        self._set_preview_text(bank, "")

    def _clear_results(self) -> None:
        self.results = {"A": [], "B": []}
        for bank in ("A", "B"):
            self.result_headers[bank].configure(text=f"ONLY IN BANK-{bank}  ·  0")
            self.result_lists[bank].delete(0, tk.END)
            self._clear_preview(bank)


def main() -> None:
    parser = argparse.ArgumentParser(description="NAS folder comparison and synchronization")
    parser.add_argument("--scheduled-sync", action="store_true", help="Run one background sync without opening the UI")
    parser.add_argument("--settings", type=Path, default=default_settings_path())
    arguments = parser.parse_args()
    if arguments.scheduled_sync:
        raise SystemExit(scheduled_sync(arguments.settings))
    window = tk.Tk()
    NasTool(window, arguments.settings)
    window.mainloop()


if __name__ == "__main__":
    main()
