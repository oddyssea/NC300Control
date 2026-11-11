import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nc300 import paths, ui


class PortableDataTests(unittest.TestCase):
    def test_packaged_data_follows_executable_including_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory(prefix="NC300 portable ") as folder:
            executable = Path(folder) / "Café" / "NC300Control.exe"
            with patch.object(paths.sys, "frozen", True, create=True), \
                    patch.object(paths.sys, "executable", str(executable)):
                self.assertEqual(paths.data_directory(), executable.parent / "NC300Control-data")

    def test_source_data_keeps_existing_appdata_location(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(paths.sys, "frozen", False, create=True), \
                patch.dict(os.environ, {"LOCALAPPDATA": folder}):
            self.assertEqual(paths.data_directory(), Path(folder) / "NC300Control")

    def test_unwritable_data_is_reported_without_starting_bluetooth(self):
        with patch.object(ui, "configure_logging", side_effect=PermissionError("read only")), \
                patch.object(ui.tk, "Tk") as root, \
                patch.object(ui.messagebox, "showerror") as dialog, \
                patch.object(ui, "ControlApp") as app:
            self.assertEqual(ui.main(["--smoke-test"]), 1)
            dialog.assert_called_once()
            self.assertIn("writable folder", dialog.call_args.args[1])
            app.assert_not_called()
            root.return_value.destroy.assert_called_once()
