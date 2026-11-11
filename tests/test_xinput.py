import ctypes
import unittest
from unittest.mock import patch

from nc300 import xinput


class FakeXInput:
    def __init__(self):
        self.states = {}
        self.get_errors = {}
        self.set_results = []
        self.motor_calls = []

    def XInputGetState(self, slot, pointer):
        if slot in self.get_errors:
            return self.get_errors[slot]
        if slot not in self.states:
            return 1167
        state = ctypes.cast(pointer, ctypes.POINTER(xinput.State)).contents
        packet, buttons, lt, rt, lx, ly, rx, ry = self.states[slot]
        state.packet = packet
        state.gamepad.buttons = buttons
        state.gamepad.left_trigger, state.gamepad.right_trigger = lt, rt
        state.gamepad.lx, state.gamepad.ly = lx, ly
        state.gamepad.rx, state.gamepad.ry = rx, ry
        return 0

    def XInputSetState(self, slot, pointer):
        vibration = ctypes.cast(pointer, ctypes.POINTER(xinput.Vibration)).contents
        self.motor_calls.append((slot, vibration.left, vibration.right))
        return self.set_results.pop(0) if self.set_results else 0


class XInputTests(unittest.TestCase):
    def setUp(self):
        self.dll = FakeXInput()
        self.patch_dll = patch.object(xinput, "_dll", self.dll)
        self.patch_dll.start()
        self.addCleanup(self.patch_dll.stop)
        xinput._active_slots.clear()
        self.addCleanup(xinput._active_slots.clear)

    def test_live_monitor_preserves_full_axis_range_buttons_and_connected_slots(self):
        self.dll.states[2] = (37, 0x1001, 0, 255, -32768, 32767, 0, -16384)
        result = xinput.get_states()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["slot"], 2)
        self.assertEqual(result[0]["packet"], 37)
        self.assertEqual(result[0]["buttons"], ["D-pad Up", "A"])
        self.assertEqual(result[0]["left_trigger"], 0)
        self.assertEqual(result[0]["right_trigger"], 1)
        self.assertEqual(result[0]["left_stick"], [-1, 1])
        self.assertEqual(result[0]["right_stick"], [0, -0.5])

    def test_live_monitor_surfaces_error_other_than_disconnected(self):
        self.dll.get_errors[0] = 5
        with self.assertRaisesRegex(OSError, "player 1"):
            xinput.get_states()

    def test_rumble_stops_after_requested_interval(self):
        with patch.object(xinput.time, "sleep") as sleep:
            xinput.rumble(1, 25, 75, 0.4)
        sleep.assert_called_once_with(0.4)
        self.assertEqual(self.dll.motor_calls, [(1, 16384, 49151), (1, 0, 0)])
        self.assertEqual(xinput._active_slots, set())

    def test_rumble_stops_motors_when_wait_raises(self):
        with patch.object(xinput.time, "sleep", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                xinput.rumble(0, 50, 50)
        self.assertEqual(self.dll.motor_calls[-1], (0, 0, 0))
        self.assertEqual(xinput._active_slots, set())

    def test_failed_start_still_attempts_stop_and_clears_active_slot(self):
        self.dll.set_results = [5, 0]
        with self.assertRaisesRegex(OSError, "Vibration test failed"):
            xinput.rumble(3, 100, 100)
        self.assertEqual(self.dll.motor_calls, [(3, 65535, 65535), (3, 0, 0)])
        self.assertEqual(xinput._active_slots, set())

    def test_failed_stop_is_reported_and_clears_active_slot(self):
        self.dll.set_results = [0, 5]
        with patch.object(xinput.time, "sleep"):
            with self.assertRaisesRegex(OSError, "Could not stop vibration"):
                xinput.rumble(0, 1, 1)
        self.assertEqual(xinput._active_slots, set())

    def test_invalid_rumble_input_rejected_before_motor_calls(self):
        for args in ((True, 0, 0), (4, 0, 0), (0, -1, 10), (0, 10, 101),
                     (0, 1.5, 1), (0, 1, 1, 0), (0, 1, 1, 2.01),
                     (0, 1, 1, float("nan"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                xinput.rumble(*args)
        self.assertEqual(self.dll.motor_calls, [])

    def test_stop_all_only_stops_slots_started_by_this_utility(self):
        xinput._active_slots.update((0, 3))
        xinput.stop_all_rumble()
        self.assertEqual(set(self.dll.motor_calls), {(0, 0, 0), (3, 0, 0)})


if __name__ == "__main__":
    unittest.main()
