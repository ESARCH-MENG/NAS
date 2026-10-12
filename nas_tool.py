from __future__ import annotations

import hashlib
import argparse
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
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


AGENT_LABEL = "com.marvel.nas-tool.sync"


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


def _file_map(root: Path) -> dict[str, Path]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Folder does not exist or cannot be accessed: {root}")

    files: dict[str, Path] = {}
    def scan_failed(error: OSError) -> None:
        raise error

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=scan_failed):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
        ]
        for filename in filenames:
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


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sync_a_to_b(
    root_a: Path, root_b: Path, *, relative_paths: list[str] | None = None
) -> list[str]:
    """Copy all or selected files from the first root to the second, without deletions."""
    root_a, root_b = _validate_roots(root_a, root_b)
    files_a = _file_map(root_a)
    if relative_paths is not None:
        selected: dict[str, Path] = {}
        for relative_path in relative_paths:
            relative = Path(relative_path)
            if relative.is_absolute() or relative.drive or ".." in relative.parts:
                raise ValueError(f"Invalid relative file path: {relative_path}")
            key = _path_key(relative.as_posix())
            if key not in files_a:
                raise FileNotFoundError(f"Selected file does not exist: {root_a / relative}")
            selected[key] = files_a[key]
        files_a = selected
    files_b = _file_map(root_b)
    copied: list[str] = []

    for key, source in files_a.items():
        relative_path = source.relative_to(root_a)
        destination = root_b / relative_path
        if key in files_b and _file_digest(source) == _file_digest(files_b[key]):
            continue

        if destination.is_symlink() or not destination.parent.resolve().is_relative_to(root_b):
            raise ValueError(f"Unsafe destination path: {relative_path}")
        if relative_paths is not None and destination.is_dir():
            raise ValueError(f"A folder already occupies the sync destination: {relative_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.resolve().is_relative_to(root_b):
            raise ValueError(f"Destination path is outside the destination bank: {relative_path}")
        shutil.copy2(source, destination)
        copied.append(relative_path.as_posix())
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


def upload_to_bank_a(source_root: Path, bank_a: Path) -> tuple[int, int]:
    source_root, bank_a = _validate_roots(source_root, bank_a)
    sources = _file_map(source_root)
    existing = _file_map(bank_a)
    copied = skipped = 0
    for key, source in sources.items():
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
                shutil.copyfileobj(incoming, target)
            shutil.copystat(source, destination)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        copied += 1
    return copied, skipped


def open_image_capture(bank_a: Path) -> None:
    if sys.platform != "darwin":
        raise ValueError("Image Capture import is available on macOS only.")
    bank_a = bank_a.expanduser().resolve()
    if not bank_a.is_dir():
        raise ValueError(f"Bank-A folder does not exist or cannot be accessed: {bank_a}")
    for arguments in (
        ["/usr/bin/open", str(bank_a)],
        ["/usr/bin/open", "-b", "com.apple.Image_Capture"],
    ):
        result = subprocess.run(
            arguments, capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise OSError(
                result.stderr.strip() or result.stdout.strip()
                or f"Unable to open Image Capture or Bank-A (exit {result.returncode})."
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

        self._build_ui()
        self._restore_settings()

    def _build_ui(self) -> None:
        self.root.configure(background=BG)
        self.root.title("NAS Tool")
        self.root.geometry("1080x780")
        self.root.minsize(920, 680)

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
        content.columnconfigure(0, weight=1, minsize=190)
        content.columnconfigure(1, weight=2, minsize=330)
        content.columnconfigure(2, weight=1, minsize=260)
        content.rowconfigure(2, weight=1, minsize=210)
        content.rowconfigure(3, weight=1, minsize=210)

        header = tk.Frame(content, bg=PANEL)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(5, 10))
        upload = tk.Frame(header, bg=PANEL)
        upload.pack(side="left")
        RoundedButton(
            upload, "UPLOAD", self._upload, width=125, height=30, primary=True,
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
                open_image_capture(bank_a)
                messagebox.showinfo(
                    "Import with Image Capture",
                    "Bank-A and Image Capture have been opened.\n\n"
                    "1. Connect and select your camera or iPhone.\n"
                    "2. In Image Capture, choose Import To > Other...\n"
                    f"3. Select Bank-A:\n{bank_a.resolve()}\n"
                    "4. Choose the photos and click Download / Import.\n\n"
                    "Click Compare after the import finishes to refresh the results.\n"
                    "Image Capture controls duplicate handling; the folder upload "
                    "skip-existing policy does not apply here.",
                )
                return
            with sync_lock(self.settings_path):
                upload_to_bank_a(self.upload_folder, bank_a)
        except (OSError, ValueError) as error:
            self._clear_results()
            messagebox.showerror("Upload Failed", str(error))
            return
        self._clear_results()
        if self.bank_paths["B"] is not None and self.bank_paths["B"].is_dir():
            self._compare()

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
        tk.Label(
            side_content,
            text=f"BANK-{bank}",
            bg=PANEL,
            fg=TEXT,
            font=("Consolas", 15, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(2, 12))
        RoundedButton(
            side_content,
            "SELECT FOLDER",
            lambda: self._select_folder(bank),
            width=152,
            height=34,
        ).grid(row=1, column=0, sticky="w")
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
        path_label.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.path_labels[bank] = path_label

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
            with sync_lock(self.settings_path):
                sync_a_to_b(root_a, root_b)
        except (OSError, ValueError) as error:
            messagebox.showerror("Sync failed", str(error))
            return

        self._compare()

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
            with sync_lock(self.settings_path):
                for bank, source, destination in (("A", root_a, root_b), ("B", root_b, root_a)):
                    if selected[bank]:
                        sync_a_to_b(source, destination, relative_paths=selected[bank])
        except (OSError, ValueError) as error:
            self._clear_results()
            messagebox.showerror("Sync Selected failed", str(error))
            return

        self._compare()

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
