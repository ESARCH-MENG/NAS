import tempfile
import subprocess
import json
import plistlib
import sys
import tkinter as tk
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from nas_tool import NasTool, ToggleSwitch, compare_directories, delete_selected_files, sync_a_to_b, upload_to_bank_a, open_image_capture
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

    def test_delete_removes_only_selected_files(self) -> None:
        (self.bank_a / "remove.txt").write_text("remove", encoding="utf-8")
        (self.bank_a / "keep.txt").write_text("keep", encoding="utf-8")

        deleted = delete_selected_files(self.bank_a, ["remove.txt"])

        self.assertEqual(deleted, ["remove.txt"])
        self.assertFalse((self.bank_a / "remove.txt").exists())
        self.assertTrue((self.bank_a / "keep.txt").exists())

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

    def test_image_capture_opens_app_and_bank_a(self) -> None:
        with patch("nas_tool.sys.platform", "darwin"), patch(
            "nas_tool.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            open_image_capture(self.bank_a)
            self.assertEqual(run.call_args_list[0].args[0],
                             ["/usr/bin/open", str(self.bank_a.resolve())])
            self.assertEqual(run.call_args_list[1].args[0],
                             ["/usr/bin/open", "-b", "com.apple.Image_Capture"])

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

    def test_upload_without_source_uses_image_capture(self) -> None:
        window = tk.Tk()
        try:
            app = NasTool(window, self.base / "settings.json")
            app.bank_paths["A"] = self.bank_a
            with patch("nas_tool.open_image_capture") as capture, patch(
                "nas_tool.messagebox.showinfo"
            ) as info, patch("nas_tool.upload_to_bank_a") as upload:
                app._upload()
                capture.assert_called_once_with(self.bank_a)
                info.assert_called_once()
                upload.assert_not_called()
            self.assertIsNone(app.upload_folder)
            self.assertFalse(hasattr(app, "status"))
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
            self.assertEqual(restored.bank_paths, {"A": self.a.resolve(), "B": self.b.resolve()})
        finally:
            window.destroy()

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
