import asyncio
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from nc300 import ui
from nc300.device import NC300Device
from nc300.telemetry import PhysicalInputState
from fixtures import FakeBleakClient


class VisualDevice:
    connected = True
    input_monitor_error = None

    def __init__(self):
        self.state = None
        self.calls = []

    def physical_input(self):
        return self.state

    async def start_input_monitor(self):
        self.calls.append("start")

    async def stop_input_monitor(self):
        self.calls.append("stop")

    async def disconnect(self):
        self.connected = False


class VisualUITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(ui, "APP_DATA", Path(self.directory.name))
        self.input_patch = patch.object(ui, "get_states", return_value=[])
        self.data_patch.start()
        self.input_patch.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.ControlApp(self.root, auto_connect=False)
        self.fake = VisualDevice()
        self.app.device = self.fake

        async def read_fixture():
            client = FakeBleakClient()
            device = NC300Device("fake", client_factory=client.factory)
            await device.connect()
            try:
                return await device.read_snapshot()
            finally:
                await device.disconnect()
        self.app._show_snapshot(asyncio.run(read_fixture()))

    def tearDown(self):
        self.app.close()
        until = time.monotonic() + 5
        while not self.app.destroyed and time.monotonic() < until:
            self.root.update()
            time.sleep(0.01)
        self.app.worker.thread.join(timeout=2)
        self.input_patch.stop()
        self.data_patch.stop()
        self.directory.cleanup()

    def test_diagram_selection_updates_assignment_and_rejects_unsupported_control(self):
        self.app._diagram_select_mapping(1)
        self.assertEqual(self.app.source_codes[self.app.source_var.get()], 1)
        self.assertIn("A", self.app.mapping_selection_label["text"])
        self.app._diagram_select_mapping(104)
        self.assertIn("disabled", self.app.remap_apply.state())
        self.assertIn("unavailable", self.app.mapping_current_label["text"].lower())
        self.app._diagram_select_mapping(2)
        self.assertEqual(self.app.source_codes[self.app.source_var.get()], 2)
        self.assertNotIn("disabled", self.app.remap_apply.state())

    def test_visible_copy_has_clean_connection_brand_and_no_tab_slogans(self):
        self.assertEqual(self.root.title(), "NC300 Control")
        self.assertEqual(self.app.connection_label["text"], "NC300 - connected")

        def visible_text(widget):
            if "text" in widget.keys():
                yield str(widget.cget("text"))
            if isinstance(widget, tk.Canvas):
                for item in widget.find_all():
                    if widget.type(item) == "text":
                        yield widget.itemcget(item, "text")
            for child in widget.winfo_children():
                yield from visible_text(child)

        decoder = PhysicalInputState((1,))
        decoder.feed(bytes.fromhex("020101"))
        for state in (None, decoder.snapshot()):
            self.fake.state = state
            self.app._poll_input()
            texts = list(visible_text(self.root))
            self.assertIn("NC300 Control", texts)
            for text in texts:
                self.assertNotIn("?", text)
                self.assertNotIn("STUDIO", text.upper())
                for slogan in ("Every input, in view.", "Make it your own.",
                               "Fine-tune your controller.", "PERSONALIZE"):
                    self.assertNotIn(slogan, text)
        self.app.snapshot["writable"] = False
        self.app._show_snapshot(self.app.snapshot)
        self.assertEqual(self.app.connection_label["text"], "NC300 - connected · read only")

    def test_physical_presses_drive_diagrams_independently_from_windows_output(self):
        decoder = PhysicalInputState((1, 104, 105))
        decoder.feed(bytes.fromhex("06010101680169"))
        self.fake.state = decoder.snapshot()
        output = {"slot": 0, "buttons": ["Y"], "left_trigger": 0., "right_trigger": 0.,
                  "left_stick": [0., 0.], "right_stick": [0., 0.]}
        with patch.object(ui, "get_states", return_value=[output]):
            self.app._poll_input()
        self.assertEqual(self.app.live_diagram.held_codes, {1, 104, 105})
        self.assertEqual(self.app.remap_diagram.held_codes, {1, 104, 105})
        self.assertIn("Y", self.app.buttons_label["text"])
        self.assertIn("before remapping", self.app.live_note["text"])

    def test_feedback_policy_excludes_background_minimized_other_tabs_and_unknown_firmware(self):
        with patch.object(self.root, "state", return_value="normal"), patch.object(self.root, "focus_get", return_value=self.root):
            self.app.tabs.select(self.app.live_tab)
            self.assertTrue(self.app._feedback_wanted())
            self.app.tabs.select(self.app.remap_tab)
            self.assertTrue(self.app._feedback_wanted())
            self.app.tabs.select(self.app.settings_tab)
            self.assertFalse(self.app._feedback_wanted())
            self.app.tabs.select(self.app.live_tab)
            self.app.snapshot["writable"] = False
            self.assertFalse(self.app._feedback_wanted())
        with patch.object(self.root, "state", return_value="iconic"):
            self.assertFalse(self.app._feedback_wanted())
        with patch.object(self.root, "focus_get", return_value=None):
            self.assertFalse(self.app._feedback_wanted())

    def test_background_transition_actually_stops_monitor(self):
        def drain_transition():
            until = time.monotonic() + 3
            while self.app.monitor_transition_pending and time.monotonic() < until:
                self.root.update()
                time.sleep(0.01)
            self.assertFalse(self.app.monitor_transition_pending)
        with patch.object(self.app, "_feedback_wanted", return_value=True):
            self.app._sync_monitor_policy()
            drain_transition()
            self.assertTrue(self.app.monitor_enabled)
        with patch.object(self.app, "_feedback_wanted", return_value=False):
            self.app._sync_monitor_policy()
            drain_transition()
            self.assertFalse(self.app.monitor_enabled)
        self.assertEqual(self.fake.calls, ["start", "stop"])

    def test_rear_alignment_updates_both_diagrams_and_persists_without_hardware_write(self):
        self.app.live_diagram.set_view("rear")
        before = self.app.live_diagram.button_center(104)
        self.app.rear_swap_var.set(True)
        self.app._swap_rear_indicators()
        after = self.app.live_diagram.button_center(104)
        self.assertNotEqual(before, after)
        self.assertTrue((Path(self.directory.name) / "rear-layout.json").is_file())
        self.assertFalse(self.fake.calls)

