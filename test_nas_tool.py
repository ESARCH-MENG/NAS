import tempfile
import subprocess
import json
import plistlib
import sys
import threading
import time
import tkinter as tk
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, call, patch

from PIL import Image

from nas_tool import (
    NasTool,
    EmergencyStopButton,
    HELP_TEXT,
    OperationCancelled,
    ToggleSwitch,
    compare_directories,
    delete_selected_files,
    open_image_capture,
    open_accessibility_settings,
    open_automation_settings,
    stop_image_capture,
    sync_a_to_b,
    upload_to_bank_a,
)
from nas_tool import (
    AGENT_LABEL,
    Settings,
    apply_schedule,
    launch_agent_payload,
    load_settings,
    save_settings,
    scheduled_sync,
    sync_lock,
)


def wait_for_task(window: tk.Misc, app: NasTool, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while app._task_thread is not None:
        window.update()
        if time.monotonic() >= deadline:
            raise AssertionError("Background task did not finish.")
        time.sleep(0.01)
    window.update()


class NasToolFileOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.bank_a = self.base / "bank_a"
        self.bank_b = self.base / "bank_b"
        self.bank_a.mkdir()
        self.bank_b.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_compare_lists_files_unique_to_each_bank(self) -> None:
        (self.bank_a / "a-only.txt").write_text("A", encoding="utf-8")
        (self.bank_a / "same.txt").write_text("same", encoding="utf-8")
        (self.bank_b / "b-only.txt").write_text("B", encoding="utf-8")
        (self.bank_b / "same.txt").write_text("different content", encoding="utf-8")

        only_a, only_b = compare_directories(self.bank_a, self.bank_b)

        self.assertEqual(only_a, ["a-only.txt"])
        self.assertEqual(only_b, ["b-only.txt"])

    def test_sync_copies_new_and_changed_files_without_deleting_extras(self) -> None:
        (self.bank_a / "new.txt").write_text("new", encoding="utf-8")
        (self.bank_a / "changed.txt").write_text("updated", encoding="utf-8")
        (self.bank_b / "changed.txt").write_text("old", encoding="utf-8")
        (self.bank_b / "extra.txt").write_text("keep", encoding="utf-8")

        copied = sync_a_to_b(self.bank_a, self.bank_b)

        self.assertCountEqual(copied, ["new.txt", "changed.txt"])
        self.assertEqual((self.bank_b / "new.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.bank_b / "changed.txt").read_text(encoding="utf-8"), "updated")
        self.assertTrue((self.bank_b / "extra.txt").exists())

    def test_sync_copies_nested_files(self) -> None:
        nested_file = self.bank_a / "subfolder" / "nested.txt"
        nested_file.parent.mkdir()
        nested_file.write_text("nested", encoding="utf-8")

        copied = sync_a_to_b(self.bank_a, self.bank_b)

        self.assertEqual(copied, ["subfolder/nested.txt"])
        self.assertEqual(
            (self.bank_b / "subfolder" / "nested.txt").read_text(encoding="utf-8"),
            "nested",
        )

    def test_cancelled_sync_preserves_existing_file_and_removes_partial_file(self) -> None:
        source = self.bank_a / "large.bin"
        destination = self.bank_b / "large.bin"
        source.write_bytes(b"new" * 100_000)
        destination.write_bytes(b"original")

        def cancelled() -> bool:
            partials = list(self.bank_b.glob(".*.nas-tool-part"))
            return bool(partials and partials[0].stat().st_size > 0)

        with patch("nas_tool.CHUNK_SIZE", 1024), self.assertRaises(
            OperationCancelled
        ):
            sync_a_to_b(
                self.bank_a,
                self.bank_b,
                cancelled=cancelled,
            )

        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual(list(self.bank_b.glob(".*.nas-tool-part")), [])

    def test_delete_removes_only_selected_files(self) -> None:
        (self.bank_a / "remove.txt").write_text("remove", encoding="utf-8")
        (self.bank_a / "keep.txt").write_text("keep", encoding="utf-8")

        deleted = delete_selected_files(self.bank_a, ["remove.txt"])

        self.assertEqual(deleted, ["remove.txt"])
        self.assertFalse((self.bank_a / "remove.txt").exists())
        self.assertTrue((self.bank_a / "keep.txt").exists())

    def test_selected_sync_copies_only_selected_files_in_either_direction(self) -> None:
        for source, destination in ((self.bank_a, self.bank_b), (self.bank_b, self.bank_a)):
            with self.subTest(source=source):
                name = source.name + ".txt"
                nested = source / "nested" / name
                nested.parent.mkdir(exist_ok=True)
                nested.write_text("selected", encoding="utf-8")
                unselected = source / ("unselected-" + name)
                unselected.write_text("keep", encoding="utf-8")
                copied = sync_a_to_b(
                    source, destination, relative_paths=[f"nested/{name}"]
                )
                self.assertEqual(copied, [f"nested/{name}"])
                self.assertEqual(
                    (destination / "nested" / name).read_text(encoding="utf-8"), "selected"
                )
                self.assertTrue(nested.exists())
                self.assertFalse((destination / unselected.name).exists())

    def test_selected_sync_validates_selections_before_copying(self) -> None:
        (self.bank_a / "selected.txt").write_text("selected", encoding="utf-8")
        for invalid in ("missing.txt", "../outside.txt", str(self.base / "outside.txt")):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    sync_a_to_b(
                        self.bank_a, self.bank_b, relative_paths=["selected.txt", invalid]
                    )
                self.assertFalse((self.bank_b / "selected.txt").exists())
        self.assertEqual(sync_a_to_b(self.bank_a, self.bank_b, relative_paths=[]), [])
        self.assertFalse((self.bank_b / "selected.txt").exists())

    def test_selected_sync_updates_changed_file_and_rejects_directory_destination(self) -> None:
        (self.bank_a / "selected.txt").write_text("new", encoding="utf-8")
        (self.bank_b / "selected.txt").write_text("old", encoding="utf-8")
        self.assertEqual(
            sync_a_to_b(self.bank_a, self.bank_b, relative_paths=["selected.txt"]),
            ["selected.txt"],
        )
        self.assertEqual((self.bank_b / "selected.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual(
            sync_a_to_b(self.bank_a, self.bank_b, relative_paths=["selected.txt"]), []
        )
        (self.bank_a / "blocked.txt").write_text("blocked", encoding="utf-8")
        (self.bank_b / "blocked.txt").mkdir()
        with self.assertRaises(ValueError):
            sync_a_to_b(self.bank_a, self.bank_b, relative_paths=["blocked.txt"])
        self.assertFalse((self.bank_b / "blocked.txt" / "blocked.txt").exists())

    def test_sync_selected_button_syncs_both_banks_and_refreshes_results(self) -> None:
        for bank in (self.bank_a, self.bank_b):
            (bank / f"{bank.name}-selected.txt").write_text("selected", encoding="utf-8")
            (bank / f"{bank.name}-unselected.txt").write_text("unselected", encoding="utf-8")
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths = {"A": self.bank_a, "B": self.bank_b}
            self.assertTrue(app.sync_selected_button._enabled)
            app._compare()
            window.update()
            self.assertTrue(app.sync_selected_button._enabled)
            self.assertIs(app.sync_selected_button.master, app.delete_button.master)
            self.assertLess(
                app.sync_selected_button.winfo_rootx(), app.delete_button.winfo_rootx()
            )
            for bank, source in (("A", self.bank_a), ("B", self.bank_b)):
                index = app.results[bank].index(f"{source.name}-selected.txt")
                app.result_lists[bank].selection_set(index)
            button = app.sync_selected_button
            with patch("nas_tool.messagebox.showerror") as error, patch(
                "nas_tool.sync_lock", return_value=nullcontext()
            ) as lock:
                button.event_generate("<ButtonPress-1>", x=10, y=10)
                button.event_generate("<ButtonRelease-1>", x=10, y=10)
                wait_for_task(window, app)
                error.assert_not_called()
                lock.assert_called_once_with(app.settings_path)
            for source, destination, bank in (
                (self.bank_a, self.bank_b, "A"), (self.bank_b, self.bank_a, "B")
            ):
                self.assertTrue((source / f"{source.name}-selected.txt").exists())
                self.assertTrue((destination / f"{source.name}-selected.txt").exists())
                self.assertFalse((destination / f"{source.name}-unselected.txt").exists())
                self.assertEqual(app.results[bank], [f"{source.name}-unselected.txt"])
                self.assertEqual(app.result_lists[bank].curselection(), ())
            app._clear_results()
            self.assertTrue(app.sync_selected_button._enabled)
        finally:
            window.destroy()

    def test_sync_selected_reports_empty_selection_and_copy_errors(self) -> None:
        (self.bank_a / "selected.txt").write_text("selected", encoding="utf-8")
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths = {"A": self.bank_a, "B": self.bank_b}
            app._compare()
            with patch("nas_tool.messagebox.showinfo") as info, patch(
                "nas_tool.sync_a_to_b"
            ) as sync:
                app._sync_selected()
                info.assert_called_once()
                sync.assert_not_called()
            app.result_lists["A"].selection_set(0)
            with patch("nas_tool.sync_a_to_b", side_effect=OSError("Copy denied")), patch(
                "nas_tool.messagebox.showerror"
            ) as error:
                app._sync_selected()
                wait_for_task(window, app)
                error.assert_called_once_with("Sync Selected failed", "Copy denied")
            self.assertEqual(app.results, {"A": [], "B": []})
            self.assertTrue(app.sync_selected_button._enabled)
            self.assertFalse((self.bank_b / "selected.txt").exists())
        finally:
            window.destroy()

    def test_bottom_controls_are_centered_under_each_column(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            content = app.sync_selected_button.master.master.master
            for width in (920, 1080, 1400):
                with self.subTest(width=width):
                    window.geometry(f"{width}x780")
                    window.update()
                    for column in range(3):
                        panel = content.grid_slaves(row=2, column=column)[0]
                        controls = content.grid_slaves(row=4, column=column)[0]
                        group = controls.winfo_children()[0]
                        panel_center = panel.winfo_rootx() + panel.winfo_width() / 2
                        group_center = group.winfo_rootx() + group.winfo_width() / 2
                        self.assertAlmostEqual(group_center, panel_center, delta=1)
                        self.assertGreaterEqual(group.winfo_rootx(), panel.winfo_rootx())
                        self.assertLessEqual(
                            group.winfo_rootx() + group.winfo_width(),
                            panel.winfo_rootx() + panel.winfo_width(),
                        )
                    self.assertLess(
                        app.sync_selected_button.winfo_rootx(),
                        app.delete_button.winfo_rootx(),
                    )
                    self.assertGreater(
                        app.stop_button.winfo_rootx(),
                        app.delete_button.winfo_rootx(),
                    )
        finally:
            window.destroy()

    def test_stop_button_is_bottom_right_and_idle_click_keeps_window_open(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            window.update()
            self.assertIsInstance(app.stop_button, EmergencyStopButton)
            self.assertEqual(
                app.stop_button.itemcget(app.stop_button.find_all()[0], "fill"),
                "#cc0000",
            )
            self.assertEqual(
                app.stop_button.itemcget(app.stop_button.find_all()[1], "fill"),
                "#ffffff",
            )
            self.assertGreater(
                app.stop_button.winfo_rooty(),
                app.delete_button.winfo_rooty(),
            )
            app.stop_button.event_generate("<ButtonPress-1>", x=10, y=10)
            self.assertEqual(
                app.stop_button.itemcget(app.stop_button.find_all()[0], "fill"),
                "#990000",
            )
            self.assertTrue(window.winfo_exists())
        finally:
            window.destroy()

    def test_folder_buttons_are_equal_sized_and_bottom_right_aligned(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            window.geometry("920x680")
            window.update()
            for bank in ("A", "B"):
                with self.subTest(bank=bank):
                    select_button, open_button = app.folder_buttons[bank]
                    panel = select_button.master.master.master
                    self.assertEqual(
                        (select_button.winfo_width(), select_button.winfo_height()),
                        (open_button.winfo_width(), open_button.winfo_height()),
                    )
                    self.assertEqual(select_button.winfo_width(), 114)
                    self.assertEqual(select_button.winfo_rooty(), open_button.winfo_rooty())
                    self.assertLess(select_button.winfo_rootx(), open_button.winfo_rootx())
                    self.assertEqual(
                        open_button.winfo_rootx() + open_button.winfo_width(),
                        panel.winfo_rootx() + panel.winfo_width() - 12,
                    )
                    self.assertEqual(
                        open_button.winfo_rooty() + open_button.winfo_height(),
                        panel.winfo_rooty() + panel.winfo_height() - 12,
                    )
                    for button in (select_button, open_button):
                        text_bounds = button.bbox(button.find_all()[-1])
                        self.assertIsNotNone(text_bounds)
                        assert text_bounds is not None
                        self.assertGreaterEqual(text_bounds[0], 10)
                        self.assertLessEqual(text_bounds[2], button.winfo_width() - 10)
        finally:
            window.destroy()

    def test_help_menu_opens_single_english_user_guide(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            menu_name = window.cget("menu")
            menu = window.nametowidget(menu_name)
            self.assertEqual(menu.entrycget(1, "label"), "Help")

            app._show_help()
            window.update()
            help_window = app._help_window
            self.assertIsNotNone(help_window)
            assert help_window is not None
            guide = next(
                child
                for frame in help_window.winfo_children()
                for child in frame.winfo_children()
                if isinstance(child, tk.Text)
            )
            self.assertEqual(guide.get("1.0", tk.END).strip(), HELP_TEXT.strip())
            self.assertIn("Emergency Stop", guide.get("1.0", tk.END))

            app._show_help()
            window.update()
            self.assertIs(app._help_window, help_window)
        finally:
            window.destroy()

    def test_selected_buttons_keep_colors_and_prompt_when_nothing_is_selected(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths = {"A": self.bank_a, "B": self.bank_b}
            for phase in ("startup", "empty_compare", "cleared"):
                with self.subTest(phase=phase):
                    if phase == "empty_compare":
                        app._compare()
                    elif phase == "cleared":
                        app._clear_results()
                    window.update()
                    for button, title, color in (
                        (app.sync_selected_button, "Sync Selected", "#16303b"),
                        (app.delete_button, "Delete Selected", "#c47f2c"),
                    ):
                        self.assertTrue(button._enabled)
                        button._hovered = False
                        button._draw(button.winfo_width(), button.winfo_height())
                        self.assertEqual(button.itemcget(button.find_all()[0], "fill"), color)
                        with patch("nas_tool.messagebox.showinfo") as info, patch(
                            "nas_tool.sync_a_to_b"
                        ) as sync, patch("nas_tool.delete_selected_files") as delete:
                            button.event_generate("<ButtonPress-1>", x=10, y=10)
                            button.event_generate("<ButtonRelease-1>", x=10, y=10)
                            window.update()
                            info.assert_called_once_with(
                                title, "Select one or more files from the comparison results."
                            )
                            sync.assert_not_called()
                            delete.assert_not_called()
        finally:
            window.destroy()

    def test_delete_validates_all_selections_before_removing_any(self) -> None:
        (self.bank_a / "keep.txt").write_text("keep", encoding="utf-8")

        with self.assertRaises(FileNotFoundError):
            delete_selected_files(self.bank_a, ["keep.txt", "missing.txt"])

        self.assertTrue((self.bank_a / "keep.txt").exists())

    def test_nested_bank_roots_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compare_directories(self.bank_a, self.bank_a / "inside")

    def test_upload_copies_nested_files_and_skips_existing_files(self) -> None:
        source = self.base / "upload"
        source.mkdir()
        (source / "same.txt").write_text("new content", encoding="utf-8")
        (self.bank_a / "same.txt").write_text("original", encoding="utf-8")
        (source / "nested").mkdir()
        (source / "nested" / "photo.txt").write_text("photo", encoding="utf-8")
        self.assertEqual(upload_to_bank_a(source, self.bank_a), (1, 1))
        self.assertEqual((self.bank_a / "same.txt").read_text(encoding="utf-8"), "original")
        self.assertEqual((self.bank_a / "nested" / "photo.txt").read_text(encoding="utf-8"), "photo")
        self.assertEqual(upload_to_bank_a(source, self.bank_a), (0, 2))
        with self.assertRaises(ValueError):
            upload_to_bank_a(self.bank_a, self.bank_a)

    def test_cancelled_folder_download_removes_partial_file(self) -> None:
        source = self.base / "upload"
        source.mkdir()
        (source / "large.bin").write_bytes(b"download" * 100_000)
        destination = self.bank_a / "large.bin"

        def cancelled() -> bool:
            return destination.exists() and destination.stat().st_size > 0

        with patch("nas_tool.CHUNK_SIZE", 1024), self.assertRaises(
            OperationCancelled
        ):
            upload_to_bank_a(
                source,
                self.bank_a,
                cancelled=cancelled,
            )

        self.assertFalse(destination.exists())

    def test_stop_button_cancels_sync_and_folder_download(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths = {"A": self.bank_a, "B": self.bank_b}
            source = self.base / "upload"
            source.mkdir()
            app.upload_folder = source

            for start, target in (
                (app._sync, "nas_tool.sync_a_to_b"),
                (app._upload, "nas_tool.upload_to_bank_a"),
            ):
                with self.subTest(target=target):
                    started = threading.Event()

                    def wait_for_stop(*_args: object, **kwargs: object) -> object:
                        cancelled = kwargs["cancelled"]
                        started.set()
                        while not cancelled():
                            time.sleep(0.005)
                        raise OperationCancelled

                    with patch(target, side_effect=wait_for_stop):
                        start()
                        self.assertTrue(started.wait(1))
                        app._stop()
                        wait_for_task(window, app)
                    self.assertTrue(window.winfo_exists())
        finally:
            window.destroy()

    def test_upload_selection_is_not_persisted_and_upload_targets_a(self) -> None:
        source = self.base / "upload"
        source.mkdir()
        (source / "new.txt").write_text("upload", encoding="utf-8")
        settings = self.base / "settings.json"
        window = tk.Tk()
        try:
            app = NasTool(window, settings)
            app.bank_paths = {"A": self.bank_a, "B": self.bank_b}
            with patch("nas_tool.filedialog.askdirectory", return_value=str(source)):
                app._select_upload_folder()
            self.assertEqual(app.upload_path_label.cget("text"), str(source))
            self.assertFalse(settings.exists())
            app._upload()
            wait_for_task(window, app)
            self.assertEqual(app.upload_path_label.cget("text"), str(source))
            self.assertTrue((self.bank_a / "new.txt").exists())
            self.assertFalse((self.bank_b / "new.txt").exists())
            self.assertIn("new.txt", app.results["A"])
            restored = NasTool(tk.Toplevel(window), settings)
            self.assertIsNone(restored.upload_folder)
            self.assertEqual(
                restored.upload_path_label.cget("text"), "No upload folder selected"
            )
        finally:
            window.destroy()

    def test_click_toggles_result_selection_and_preview(self) -> None:
        (self.bank_a / "first.txt").write_text("first", encoding="utf-8")
        (self.bank_a / "second.txt").write_text("second", encoding="utf-8")
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths = {"A": self.bank_a, "B": self.bank_b}
            app._compare()
            window.update()
            listing = app.result_lists["A"]

            def click(index: int) -> None:
                bounds = listing.bbox(index)
                self.assertIsNotNone(bounds)
                if bounds is not None:
                    listing.event_generate("<Button-1>", x=10, y=bounds[1] + 2)
                    listing.event_generate("<ButtonRelease-1>", x=10, y=bounds[1] + 2)
                    window.update()

            click(0)
            self.assertEqual(listing.curselection(), (0,))
            click(1)
            self.assertEqual(listing.curselection(), (0, 1))
            self.assertIn("second", app.preview_texts["A"].get("1.0", tk.END))
            click(1)
            self.assertEqual(listing.curselection(), (0,))
            click(0)
            self.assertEqual(listing.curselection(), ())
            self.assertEqual(app.preview_texts["A"].get("1.0", tk.END).strip(), "")
        finally:
            window.destroy()

    def test_image_capture_sets_bank_a_as_import_destination(self) -> None:
        with patch("nas_tool.sys.platform", "darwin"), patch(
            "nas_tool.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            open_image_capture(self.bank_a)
            run.assert_called_once()
            automation = run.call_args
            self.assertEqual(automation.args[0][:2], ["/usr/bin/osascript", "-e"])
            self.assertEqual(automation.args[0][3:], [str(self.bank_a.resolve())])
            self.assertIn("on run argv", automation.args[0][2])
            self.assertIn("tell application id \"com.apple.Image_Capture\" to activate", automation.args[0][2])
            self.assertIn("keystroke destinationPath", automation.args[0][2])
            self.assertEqual(automation.kwargs["timeout"], 90)

    def test_image_capture_reports_destination_errors(self) -> None:
        for failure in (
            subprocess.CompletedProcess([], 1, "", "Accessibility permission required"),
            subprocess.TimeoutExpired("osascript", 45),
        ):
            with self.subTest(failure=failure), patch("nas_tool.sys.platform", "darwin"), patch(
                "nas_tool.subprocess.run",
                side_effect=[failure],
            ):
                with self.assertRaises(OSError):
                    open_image_capture(self.bank_a)

    def test_image_capture_passes_special_paths_as_arguments(self) -> None:
        bank = self.base / "photos 'quoted'"
        bank.mkdir()
        with patch("nas_tool.sys.platform", "darwin"), patch(
            "nas_tool.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            open_image_capture(bank)
        self.assertEqual(run.call_args_list[-1].args[0][-1], str(bank.resolve()))
        self.assertNotIn(str(bank.resolve()), run.call_args_list[-1].args[0][2])

    def test_image_capture_rejects_missing_folder_and_reports_launch_errors(self) -> None:
        with patch("nas_tool.sys.platform", "darwin"), patch(
            "nas_tool.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "App unavailable"),
        ) as run:
            with self.assertRaises(ValueError):
                open_image_capture(self.base / "missing")
            run.assert_not_called()
            with self.assertRaisesRegex(OSError, "App unavailable"):
                open_image_capture(self.bank_a)
            self.assertEqual(run.call_count, 1)

    def test_image_capture_rejects_non_macos(self) -> None:
        with patch("nas_tool.sys.platform", "win32"), patch(
            "nas_tool.subprocess.run"
        ) as run:
            with self.assertRaisesRegex(ValueError, "macOS only"):
                open_image_capture(self.bank_a)
        run.assert_not_called()

    def test_stop_image_capture_quits_the_macos_app(self) -> None:
        with patch("nas_tool.sys.platform", "darwin"), patch(
            "nas_tool.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            stop_image_capture()
        run.assert_called_once_with(
            [
                "/usr/bin/osascript",
                "-e",
                'tell application "Image Capture" to quit',
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_upload_without_source_uses_image_capture(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths["A"] = self.bank_a
            with patch("nas_tool.open_image_capture") as capture, patch(
                "nas_tool.messagebox.showinfo"
            ) as info, patch("nas_tool.upload_to_bank_a") as upload:
                app._upload()
                wait_for_task(window, app)
                capture.assert_called_once_with(self.bank_a)
                info.assert_called_once()
                self.assertIn(str(self.bank_a.resolve()), info.call_args.args[1])
                self.assertNotIn("Other...", info.call_args.args[1])
                upload.assert_not_called()
            self.assertIsNone(app.upload_folder)
            self.assertTrue(app._image_capture_active)
            self.assertFalse(hasattr(app, "status"))
        finally:
            window.destroy()

    def test_stop_button_ends_image_capture_without_closing_window(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths["A"] = self.bank_a
            with patch("nas_tool.open_image_capture"), patch(
                "nas_tool.messagebox.showinfo"
            ):
                app._upload()
                wait_for_task(window, app)
            app._task_thread = Mock()
            app._task_thread.is_alive.return_value = True

            def assert_cancel_was_prioritized() -> None:
                self.assertTrue(app._cancel_event.is_set())

            with patch(
                "nas_tool.stop_image_capture",
                side_effect=assert_cancel_was_prioritized,
            ) as stop:
                app._stop()
            stop.assert_called_once_with()
            self.assertFalse(app._image_capture_active)
            self.assertTrue(window.winfo_exists())
        finally:
            window.destroy()

    def test_upload_reports_image_capture_setup_failure(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths["A"] = self.bank_a
            with patch(
                "nas_tool.open_image_capture",
                side_effect=OSError(
                    "287:381: execution error: Allow NAS Tool (or its Python host) "
                    "in System Settings > Privacy & Security > Accessibility. (-2700)"
                ),
            ), patch("nas_tool.messagebox.showinfo") as info, patch(
                "nas_tool.messagebox.showerror"
            ) as error, patch(
                "nas_tool.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as launch_settings, patch("nas_tool.sys.platform", "darwin"):
                app._upload()
                wait_for_task(window, app)
            info.assert_not_called()
            error.assert_called_once()
            self.assertEqual(error.call_args.args[0], "Download Failed")
            self.assertIn(
                "287:381: execution error: Allow NAS Tool (or its Python host) "
                "in System Settings > Privacy & Security > Accessibility. (-2700)",
                error.call_args.args[1],
            )
            self.assertIn("click +", error.call_args.args[1])
            self.assertIn("Terminal/iTerm", error.call_args.args[1])
            launch_settings.assert_called_once_with(
                [
                    "/usr/bin/open",
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertFalse(app._image_capture_active)
        finally:
            window.destroy()

    def test_accessibility_settings_reports_launch_errors(self) -> None:
        for failure in (
            subprocess.CompletedProcess([], 1, "", "Settings unavailable"),
            subprocess.TimeoutExpired("open", 10),
            OSError("Cannot launch open"),
        ):
            with self.subTest(failure=failure), patch(
                "nas_tool.sys.platform", "darwin"
            ), patch("nas_tool.subprocess.run", side_effect=[failure]):
                with self.assertRaises(OSError):
                    open_accessibility_settings()

    def test_automation_settings_opens_automation_privacy_pane(self) -> None:
        with patch("nas_tool.sys.platform", "darwin"), patch(
            "nas_tool.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            open_automation_settings()
        run.assert_called_once_with(
            [
                "/usr/bin/open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_image_capture_permission_error_preserved_when_settings_fail(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths["A"] = self.bank_a
            with patch(
                "nas_tool.open_image_capture",
                side_effect=OSError("Allow NAS Tool (or its Python host)"),
            ), patch(
                "nas_tool.open_accessibility_settings",
                side_effect=OSError("Settings unavailable"),
            ), patch("nas_tool.messagebox.showerror") as error:
                app._upload()
                wait_for_task(window, app)
            self.assertIn("Allow NAS Tool (or its Python host)", error.call_args.args[1])
            self.assertIn("Settings unavailable", error.call_args.args[1])
            self.assertFalse(app._image_capture_active)
        finally:
            window.destroy()

    def test_automation_permission_error_explains_how_to_grant_access(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths["A"] = self.bank_a
            with patch(
                "nas_tool.open_image_capture",
                side_effect=OSError(
                    "Not authorized to send Apple events to System Events. (-1743)"
                ),
            ), patch("nas_tool.open_automation_settings") as open_settings, patch(
                "nas_tool.messagebox.showerror"
            ) as error:
                app._upload()
                wait_for_task(window, app)
            open_settings.assert_called_once_with()
            message = error.call_args.args[1]
            self.assertIn("Privacy & Security > Automation", message)
            self.assertIn("enable NAS Tool for System Events", message)
            self.assertIn("no Add button", message)
            self.assertIn("NAS Tool requests this access when you click Download", message)
            self.assertIn("separate from Accessibility", message)
        finally:
            window.destroy()

    def test_stop_during_image_capture_setup_does_not_report_success(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths["A"] = self.bank_a

            def cancel_setup(_bank_a: Path) -> None:
                app._cancel_event.set()

            with patch("nas_tool.open_image_capture", side_effect=cancel_setup), patch(
                "nas_tool.stop_image_capture"
            ) as stop, patch("nas_tool.messagebox.showinfo") as info:
                app._upload()
                wait_for_task(window, app)
            stop.assert_called_once_with()
            info.assert_not_called()
            self.assertFalse(app._image_capture_active)
        finally:
            window.destroy()

    def test_selection_switches_full_area_photo_and_text_previews(self) -> None:
        Image.new("RGB", (1200, 800), "red").save(self.bank_a / "photo.jpg")
        (self.bank_a / "notes.txt").write_text("Selected text", encoding="utf-8")
        (self.bank_b / "other.txt").write_text("Bank-B text", encoding="utf-8")
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths = {"A": self.bank_a, "B": self.bank_b}
            app._compare()
            window.update()
            listing = app.result_lists["A"]
            for filename in ("photo.jpg", "notes.txt", "photo.jpg"):
                listing.selection_clear(0, tk.END)
                index = app.results["A"].index(filename)
                listing.selection_set(index)
                listing.activate(index)
                listing.event_generate("<<ListboxSelect>>")
                window.update()
                canvas = app.preview_image_labels["A"]
                text = app.preview_texts["A"]
                if filename == "photo.jpg":
                    self.assertTrue(canvas.winfo_ismapped())
                    self.assertFalse(text.winfo_ismapped())
                    photo = app.preview_images["A"]
                    self.assertIsNotNone(photo)
                    if photo is not None:
                        self.assertLessEqual(photo.width(), canvas.winfo_width())
                        self.assertLessEqual(photo.height(), canvas.winfo_height())
                else:
                    self.assertFalse(canvas.winfo_ismapped())
                    self.assertIn("Selected text", text.get("1.0", tk.END))
                    self.assertGreater(
                        text.winfo_height(), text.master.winfo_height() * 0.75
                    )
            listing.selection_set(0, tk.END)
            listing.activate(app.results["A"].index("notes.txt"))
            listing.event_generate("<<ListboxSelect>>")
            window.update()
            self.assertIn("Selected text", app.preview_texts["A"].get("1.0", tk.END))
            app.result_lists["B"].selection_set(0)
            app.result_lists["B"].event_generate("<<ListboxSelect>>")
            window.update()
            self.assertIn("Bank-B text", app.preview_texts["B"].get("1.0", tk.END))
            self.assertIn("Selected text", app.preview_texts["A"].get("1.0", tk.END))
            app._clear_results()
            window.update()
            self.assertIsNone(app.preview_sources["A"])
            self.assertEqual(app.preview_texts["A"].get("1.0", tk.END).strip(), "")
        finally:
            window.destroy()


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.path = self.base / "settings.json"
        self.a = self.base / "a"
        self.b = self.base / "b"
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schedule_time_arrows_wrap_in_both_directions(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.path)

            def descendants(widget: tk.Misc):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            fields = [widget for widget in descendants(window) if isinstance(widget, tk.Spinbox)]
            self.assertEqual(len(fields), 2)
            for field, variable, maximum in (
                (fields[0], app.sync_hour, 23),
                (fields[1], app.sync_minute, 59),
            ):
                variable.set(f"{maximum:02d}")
                field.invoke("buttonup")
                self.assertEqual(variable.get(), "00")
                field.invoke("buttondown")
                self.assertEqual(variable.get(), f"{maximum:02d}")
        finally:
            window.destroy()

    def test_round_trip_and_missing_settings(self) -> None:
        self.assertEqual(load_settings(self.path), Settings())
        settings = Settings(str(self.a), str(self.b), True, 15, 30, "weekly")
        save_settings(self.path, settings)
        self.assertEqual(load_settings(self.path), settings)

    def test_invalid_settings_are_rejected(self) -> None:
        for data in ({"sync_hour": 24}, {"sync_hour": True},
                     {"sync_minute": 60}, {"sync_cycle": "yearly"},
                     {"schedule_enabled": "yes"}, {"bank_a": 123}, []):
            with self.subTest(data=data):
                self.path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_settings(self.path)

    def test_launch_agent_payload_preserves_paths_and_calendar(self) -> None:
        settings = Settings(str(self.a), str(self.b), True, 15, 30, "weekly")
        payload = launch_agent_payload(settings, self.path)
        decoded = plistlib.loads(plistlib.dumps(payload))
        self.assertEqual(decoded["Label"], AGENT_LABEL)
        self.assertEqual(decoded["StartCalendarInterval"],
                         {"Hour": 15, "Minute": 30, "Weekday": 1})
        self.assertNotIn("StartInterval", decoded)
        self.assertEqual(decoded["ProgramArguments"][-3:],
                         ["--scheduled-sync", "--settings", str(self.path.resolve())])
        self.assertEqual(decoded["LimitLoadToSessionType"], "Aqua")
        self.assertNotIn("RunAtLoad", decoded)

    def test_worker_copies_without_ui_and_respects_disabled_flag(self) -> None:
        (self.a / "photo.txt").write_text("source", encoding="utf-8")
        settings = Settings(str(self.a), str(self.b), schedule_enabled=True)
        save_settings(self.path, settings)
        with patch("nas_tool.sys.platform", "win32"):
            self.assertEqual(scheduled_sync(self.path), 0)
            self.assertEqual((self.b / "photo.txt").read_text(encoding="utf-8"), "source")
            settings.schedule_enabled = False
            save_settings(self.path, settings)
            (self.a / "photo.txt").write_text("changed", encoding="utf-8")
            self.assertEqual(scheduled_sync(self.path), 0)
            self.assertEqual((self.b / "photo.txt").read_text(encoding="utf-8"), "source")

    def test_worker_missing_storage_returns_failure_and_logs(self) -> None:
        save_settings(self.path, Settings(str(self.base / "missing"), str(self.b), schedule_enabled=True))
        self.assertEqual(scheduled_sync(self.path), 1)
        self.assertIn("Scheduled sync failed", (self.base / "sync.log").read_text(encoding="utf-8"))

    def test_register_replace_and_disable_launch_agent(self) -> None:
        settings = Settings(str(self.a), str(self.b), schedule_enabled=True)
        absent = subprocess.CompletedProcess([], 113, "", "Could not find service")
        success = subprocess.CompletedProcess([], 0, "", "")
        with patch("nas_tool.sys.platform", "darwin"), \
                patch("nas_tool.os.getuid", return_value=501, create=True), \
                patch("nas_tool.Path.home", return_value=self.base), \
                patch("nas_tool._launchctl", side_effect=[absent, success]) as command:
            apply_schedule(settings, self.path)
            agent = self.base / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
            self.assertTrue(agent.exists())
            command.assert_called_with("bootstrap", "gui/501", str(agent))
        settings.schedule_enabled = False
        with patch("nas_tool.sys.platform", "darwin"), \
                patch("nas_tool.os.getuid", return_value=501, create=True), \
                patch("nas_tool.Path.home", return_value=self.base), \
                patch("nas_tool._launchctl", side_effect=[success, success]) as command:
            apply_schedule(settings, self.path)
            command.assert_called_with("bootout", f"gui/501/{AGENT_LABEL}")
            self.assertFalse(agent.exists())

    def test_launchctl_errors_are_reported(self) -> None:
        with patch("nas_tool.sys.platform", "darwin"), \
                patch("nas_tool.os.getuid", return_value=501, create=True), \
                patch("nas_tool._launchctl",
                      return_value=subprocess.CompletedProcess([], 1, "", "Permission denied")):
            with self.assertRaisesRegex(OSError, "Permission denied"):
                apply_schedule(Settings(str(self.a), str(self.b), schedule_enabled=True), self.path)

    def test_folder_selection_is_saved_and_restored(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.path)
            with patch("nas_tool.filedialog.askdirectory", side_effect=[str(self.a), str(self.b)]):
                app._select_folder("A")
                app._select_folder("B")
            self.assertEqual(load_settings(self.path).bank_a, str(self.a.resolve()))
            restored = NasTool(tk.Toplevel(window), self.path)
            self.assertEqual(restored.bank_paths, {"A": self.a, "B": self.b})
        finally:
            window.destroy()

    def test_open_folder_uses_selected_bank_path(self) -> None:
        app = NasTool.__new__(NasTool)
        app.bank_paths = {"A": self.a, "B": self.b}
        with patch("nas_tool.sys.platform", "darwin"), patch(
            "nas_tool.subprocess.run"
        ) as open_command:
            app._open_folder("A")
            app._open_folder("B")
        self.assertEqual(
            open_command.call_args_list,
            [call(["open", str(self.a)], check=True), call(["open", str(self.b)], check=True)],
        )

        app.bank_paths["A"] = None
        with patch("nas_tool.messagebox.showinfo") as info, patch(
            "nas_tool.subprocess.run"
        ) as open_command:
            app._open_folder("A")
        info.assert_called_once_with(
            "No Folder Selected", "Select a folder for Bank-A first."
        )
        open_command.assert_not_called()

    def test_schedule_failure_does_not_leave_worker_enabled(self) -> None:
        save_settings(self.path, Settings(str(self.a), str(self.b)))
        window = tk.Tk()
        try:
            app = NasTool(window, self.path)
            app.sync_hour.set("15")
            app.sync_minute.set("30")
            app.sync_cycle.set("weekly")
            with patch("nas_tool.sys.platform", "darwin"), \
                    patch("nas_tool.sync_lock", side_effect=lambda _path: nullcontext()), \
                    patch("nas_tool.apply_schedule", side_effect=OSError("Registration failed")), \
                    patch("nas_tool.messagebox.showerror") as error:
                app.schedule_switch.invoke()
                error.assert_called_once()
            self.assertFalse(load_settings(self.path).schedule_enabled)
            self.assertFalse(app.schedule_enabled.get())
        finally:
            window.destroy()

    def test_schedule_settings_persist_after_apply(self) -> None:
        save_settings(self.path, Settings(str(self.a), str(self.b)))
        window = tk.Tk()
        try:
            app = NasTool(window, self.path)
            app.sync_hour.set("15")
            app.sync_minute.set("30")
            app.sync_cycle.set("weekly")
            with patch("nas_tool.sys.platform", "darwin"), \
                    patch("nas_tool.sync_lock", side_effect=lambda _path: nullcontext()), \
                    patch("nas_tool.apply_schedule") as register:
                app.schedule_switch.invoke()
                register.assert_called_once()
            self.assertEqual(load_settings(self.path), Settings(str(self.a), str(self.b), True, 15, 30, "weekly"))
            restored = NasTool(tk.Toplevel(window), self.path)
            self.assertEqual(restored.sync_hour.get(), "15")
            self.assertEqual(restored.sync_minute.get(), "30")
            self.assertEqual(restored.sync_cycle.get(), "weekly")
            app.sync_hour.set("invalid")
            with patch("nas_tool.sys.platform", "darwin"), \
                    patch("nas_tool.sync_lock", side_effect=lambda _path: nullcontext()), \
                    patch("nas_tool.apply_schedule"):
                app.schedule_switch.invoke()
            self.assertFalse(load_settings(self.path).schedule_enabled)
        finally:
            window.destroy()

    def test_toggle_tracks_saved_state_and_handles_mouse_click(self) -> None:
        window = tk.Tk()
        try:
            variable = tk.BooleanVar(value=False)
            calls: list[bool] = []
            toggle = ToggleSwitch(window, variable, lambda: calls.append(variable.get()))
            toggle.pack()
            window.update()
            self.assertEqual(toggle.coords(toggle.find_all()[1])[0], 5)
            variable.set(True)
            window.update()
            self.assertEqual(toggle.coords(toggle.find_all()[1])[0], 25)
            self.assertEqual(calls, [])
            toggle.event_generate("<Button-1>", x=20, y=12)
            window.update()
            self.assertEqual(calls, [False])
            self.assertEqual(toggle.coords(toggle.find_all()[1])[0], 5)
        finally:
            window.destroy()

    def test_background_cli_does_not_create_a_window(self) -> None:
        (self.a / "new.txt").write_text("background", encoding="utf-8")
        save_settings(self.path, Settings(str(self.a), str(self.b), schedule_enabled=True))
        script = Path(__file__).with_name("nas_tool.py")
        result = subprocess.run(
            [sys.executable, str(script), "--scheduled-sync", "--settings", str(self.path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.b / "new.txt").read_text(encoding="utf-8"), "background")

    @unittest.skipUnless(sys.platform == "darwin", "macOS advisory lock")
    def test_sync_lock_prevents_overlapping_runs(self) -> None:
        with sync_lock(self.path):
            with self.assertRaisesRegex(OSError, "already running"):
                with sync_lock(self.path):
                    self.fail("The second sync must not acquire the lock.")

    def test_daily_and_monthly_calendar(self) -> None:
        for cycle, extra in (("daily", {}), ("monthly", {"Day": 1})):
            with self.subTest(cycle=cycle):
                settings = Settings(str(self.a), str(self.b), True, 0, 59, cycle)
                self.assertEqual(
                    launch_agent_payload(settings, self.path)["StartCalendarInterval"],
                    {"Hour": 0, "Minute": 59, **extra},
                )

    def test_legacy_interval_keeps_folders_but_requires_reenabling(self) -> None:
        self.path.write_text(json.dumps({
            "bank_a": str(self.a), "bank_b": str(self.b),
            "interval_minutes": 60, "schedule_enabled": True,
        }), encoding="utf-8")
        settings = load_settings(self.path)
        self.assertEqual(settings.bank_a, str(self.a))
        self.assertEqual(settings.bank_b, str(self.b))
        self.assertFalse(settings.schedule_enabled)
        self.assertEqual(settings.sync_cycle, "daily")


if __name__ == "__main__":
    unittest.main()
