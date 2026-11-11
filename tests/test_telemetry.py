"""Physical event decoder tests use recovered report structure, with no I/O."""
import unittest

from nc300.telemetry import PhysicalInputState


def frame(*records):
    data = bytes(value for record in records for value in record)
    return bytes([len(data)]) + data


class PhysicalInputStateTests(unittest.TestCase):
    def setUp(self):
        self.state = PhysicalInputState(reportable_codes=[1, 2, 7, 8, 18, 19, 95, 96, 97, 104])

    def test_observed_idle_frame_decodes_processed_and_raw_sticks(self):
        self.assertIsNone(self.state.snapshot())
        self.assertTrue(self.state.feed(bytes.fromhex("0c001280807d81001380807e84")))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["left_stick"], [0, 0])
        self.assertEqual(snapshot["right_stick"], [0, 0])
        self.assertEqual(snapshot["raw_axes"]["left_stick"], [125, 129])
        self.assertEqual(snapshot["raw_axes"]["right_stick"], [126, 132])
        self.assertEqual(snapshot["seen_codes"], [18, 19])
        self.assertEqual(snapshot["physical_codes"], [])
        self.assertEqual(snapshot["source"], "Bluetooth physical input")
        self.assertEqual(snapshot["report_count"], 1)

    def test_button_deltas_hold_across_reports_and_release_only_named_keys(self):
        self.state.feed(frame((1, 1), (1, 97), (1, 14)))
        self.state.feed(frame((1, 94)))
        self.assertEqual(self.state.snapshot()["physical_codes"], [1, 14, 94, 97])
        # This independent recovered example releases A, presses B and ML.
        self.state.feed(bytes.fromhex("06000101020161"))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["physical_codes"], [2, 14, 94, 97])
        self.assertEqual(snapshot["buttons"], ["B", "D-pad Up", "Start"])
        self.state.feed(frame((0, 97), (0, 14), (0, 2), (0, 94)))
        self.assertEqual(self.state.snapshot()["physical_codes"], [])

    def test_nc300_captured_a_and_both_rear_button_events(self):
        # Captured from this NC300; rear events produce no XInput output.
        for press, release, code in (("020101", "020001", 1),
                                     ("020168", "020068", 104),
                                     ("020169", "020069", 105)):
            with self.subTest(code=code):
                self.state.feed(bytes.fromhex(press))
                self.assertEqual(self.state.snapshot()["physical_codes"], [code])
                self.assertIn(code, self.state.snapshot()["physical_available_codes"])
                self.state.feed(bytes.fromhex(release))
                self.assertEqual(self.state.snapshot()["physical_codes"], [])
        self.assertEqual(self.state.snapshot()["seen_codes"], [1, 104, 105])

    def test_interleaved_sticks_triggers_and_buttons_keep_all_distinct(self):
        self.state.feed(frame((1, 11), (1, 18, 0, 0, 4, 3), (0, 7, 127, 128),
                              (1, 19, 255, 255, 249, 250), (1, 8, 255, 253), (1, 95)))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["left_stick"], [-1, 1])
        self.assertEqual(snapshot["right_stick"], [1, -1])
        self.assertEqual(snapshot["physical_codes"], [7, 8, 11, 95])
        self.assertEqual(snapshot["buttons"], ["L3"])
        self.assertEqual(snapshot["left_trigger"], 127 / 255)
        self.assertEqual(snapshot["right_trigger"], 1)
        self.assertEqual(snapshot["raw_axes"]["left_trigger"], 128)
        # State 1 with zero pressure still releases the trigger.
        self.state.feed(frame((1, 7, 0, 5), (0, 11), (0, 18, 128, 128, 127, 129)))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["physical_codes"], [8, 95])
        self.assertEqual(snapshot["left_stick"], [0, 0])
        self.assertEqual(snapshot["right_stick"], [1, -1])

    def test_axes_preserve_center_and_asymmetric_endpoints_without_false_clicks(self):
        self.state.feed(frame((1, 18, 127, 129, 0, 255), (1, 19, 255, 0, 255, 0)))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["left_stick"], [-1 / 128, -1 / 127])
        self.assertEqual(snapshot["right_stick"], [1, 1])
        self.assertEqual(snapshot["physical_codes"], [])
        self.assertEqual(snapshot["buttons"], [])

    def test_malformed_reports_reject_atomically_including_valid_prefix_events(self):
        self.state.feed(frame((1, 1), (1, 18, 0, 0, 1, 2), (1, 7, 255, 254)))
        baseline = self.state.snapshot()
        malformed = (
            b"", bytes.fromhex("030001"), bytes.fromhex("0101"),
            bytes.fromhex("03011280"), bytes.fromhex("03010780"),
            frame((0, 1), (2, 2)), frame((1, 2), (255, 3)),
            frame((0, 1), (0, 18, 128)), frame((1, 2), (0, 8, 128)),
            frame((0, 1), (0, 92, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
        )
        for payload in malformed:
            with self.subTest(payload=payload.hex()):
                with self.assertRaises(ValueError):
                    self.state.feed(payload)
                self.assertEqual(self.state.snapshot(), baseline)

    def test_bad_initial_packet_does_not_create_snapshot(self):
        with self.assertRaises(ValueError):
            self.state.feed(frame((1, 1), (9, 2)))
        self.assertIsNone(self.state.snapshot())
        with self.assertRaises(ValueError):
            self.state.feed("not bytes")
        self.assertIsNone(self.state.snapshot())

    def test_special_button_events_are_physical_and_do_not_invent_standard_outputs(self):
        self.state.feed(frame((1, 97), (1, 104), (1, 17), (1, 96), (1, 95), (1, 98)))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["physical_codes"], [17, 95, 96, 97, 98, 104])
        self.assertEqual(snapshot["buttons"], [])
        self.assertIn(98, snapshot["physical_available_codes"])
        self.assertNotIn(-1, snapshot["physical_available_codes"])

    def test_snapshots_are_independent_and_clear_preserves_reportable_capabilities(self):
        self.state.feed(frame((1, 1), (1, 18, 0, 0, 1, 2)))
        snapshot = self.state.snapshot()
        snapshot["physical_codes"].clear()
        snapshot["raw_axes"]["left_stick"][0] = 99
        self.assertEqual(self.state.snapshot()["physical_codes"], [1])
        self.assertEqual(self.state.snapshot()["raw_axes"]["left_stick"], [1, 2])
        self.state.set_reportable([2, 3])
        self.assertEqual(self.state.snapshot()["physical_available_codes"], [1, 2, 3, 18])
        self.state.clear()
        self.assertIsNone(self.state.snapshot())
        self.state.feed(b"\x00")
        self.assertEqual(self.state.snapshot()["physical_available_codes"], [2, 3])
        self.assertEqual(self.state.snapshot()["report_count"], 1)
        self.assertEqual(self.state.snapshot()["physical_codes"], [])

    def test_invalid_capabilities_leave_previous_set_unchanged(self):
        self.state.feed(b"\x00")
        before = self.state.snapshot()
        for codes in ([1, -1], [1, 256], [1, "2"], [True], [1, True]):
            with self.subTest(codes=codes):
                with self.assertRaises(ValueError):
                    self.state.set_reportable(codes)
                self.assertEqual(self.state.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
