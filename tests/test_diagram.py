"""Exercise actual Tk canvas rendering without a controller or Bluetooth writes."""
import tkinter as tk
import unittest

from nc300.diagram import CONTROL_INFO, ControllerDiagram, LIGHT, SETTINGS, SYNC


class ControllerDiagramTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.events = []
        self.diagram = ControllerDiagram(self.root, width=600, height=360,
                                         on_select=self.events.append)
        self.diagram.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self.addCleanup(self.root.destroy)

    def body(self, code):
        return self.diagram.find_withtag(f"body:{code}")[0]

    def fill(self, code):
        return self.diagram.itemcget(self.body(code), "fill")

    def test_every_standard_button_highlights_and_releases_independently(self):
        for code, info in CONTROL_INFO.items():
            if "input" not in info:
                continue
            with self.subTest(button=info["label"]):
                self.diagram.set_state({"buttons": [info["input"]]})
                self.assertEqual(self.diagram.held_codes, {code})
                self.assertEqual(self.fill(code), self.diagram.HELD_FILL)
                self.assertEqual(self.diagram.control_info(code)["state"], "pressed")
                self.diagram.set_state({"buttons": []})
                self.assertEqual(self.fill(code), self.diagram.REST_FILL)
                self.assertEqual(self.diagram.control_info(code)["state"], "released")

    def test_multiple_buttons_and_trigger_fractions_display_together(self):
        self.diagram.set_state({"buttons": ["A", "D-pad Left", "L3", "Back"],
                                "left_trigger": .25, "right_trigger": 1})
        self.assertEqual(self.diagram.held_codes, {1, 16, 11, 93, 7, 8})
        for code in (1, 16, 11, 93, 7, 8):
            self.assertEqual(self.fill(code), self.diagram.HELD_FILL)
        labels = [self.diagram.itemcget(item, "text") for item in self.diagram.find_all()
                  if self.diagram.type(item) == "text"]
        self.assertIn("ZL  25%", labels)
        self.assertIn("ZR  100%", labels)
        self.diagram.set_state(None)
        self.assertFalse(self.diagram.held_codes)
        self.assertEqual(self.fill(7), self.diagram.REST_FILL)
        self.assertEqual(self.diagram.state_source, "No input")

    def test_click_targets_match_visible_controls_and_xbox_face_layout(self):
        for code in self.diagram._layout:
            center = self.diagram.button_center(code)
            self.assertEqual(self.diagram.hit_test(*center), code)
        x_a, y_a = self.diagram.button_center(1)
        x_b, y_b = self.diagram.button_center(2)
        x_x, y_x = self.diagram.button_center(3)
        x_y, y_y = self.diagram.button_center(4)
        self.assertEqual(x_a, x_y)
        self.assertGreater(y_a, y_b)
        self.assertGreater(y_b, y_y)
        self.assertGreater(x_b, x_x)
        self.assertIsNone(self.diagram.hit_test(0, 0))
        self.diagram._click(type("Click", (), {"x": x_a, "y": y_a})())
        self.assertEqual(self.events, [1])

    def test_resized_front_and_rear_keep_correct_hit_targets(self):
        self.root.geometry("500x300")
        self.root.deiconify()
        self.root.update()
        for view in ("front", "rear"):
            self.diagram.set_view(view)
            for code in self.diagram._layout:
                with self.subTest(view=view, code=code):
                    self.assertEqual(self.diagram.hit_test(*self.diagram.button_center(code)), code)
        for code in (104, 105, LIGHT, SYNC, SETTINGS):
            self.assertIsNotNone(self.diagram.button_center(code))
        self.assertLess(self.diagram.button_center(105)[0], self.diagram.button_center(104)[0])
        self.assertIsNone(self.diagram.button_center(1))

    def test_special_controls_never_guess_their_press_from_remapped_output(self):
        self.diagram.set_mappings({"104": 1, "105": 2, "95": 3})
        self.diagram.set_state({"buttons": ["A", "B", "X", "Home", "Capture", "Turbo"]})
        self.assertEqual(self.diagram.held_codes, {1, 2, 3})
        for code in (17, 95, 96, 104, 105, LIGHT, SYNC, SETTINGS):
            self.assertEqual(self.diagram.control_info(code)["state"], "not reported")
        self.diagram.set_view("rear")
        for code in (104, 105, LIGHT, SYNC, SETTINGS):
            self.assertEqual(self.fill(code), self.diagram.UNREPORTED_FILL)
            self.assertTrue(self.diagram.itemcget(self.body(code), "dash"))
        self.assertEqual(self.diagram.state_source, "Windows output")

    def test_explicit_physical_event_codes_can_identify_special_buttons(self):
        self.diagram.set_view("rear")
        self.diagram.set_state({"physical_codes": [104, LIGHT], "buttons": ["A"]})
        self.assertEqual(self.diagram.held_codes, {104, LIGHT})
        self.assertEqual(self.fill(104), self.diagram.HELD_FILL)
        self.assertEqual(self.fill(LIGHT), self.diagram.HELD_FILL)
        self.assertEqual(self.diagram.state_source, "Physical buttons")
        self.assertEqual(self.diagram.control_info(105)["state"], "released")

    def test_bluetooth_availability_updates_special_control_status_and_outline(self):
        self.diagram.set_view("rear")
        self.diagram.set_state({"source": "Bluetooth physical input", "physical_codes": [105],
                                "physical_available_codes": [104, 105], "seen_codes": [105]})
        self.assertEqual(self.diagram.state_source, "Bluetooth physical input")
        self.assertTrue(self.diagram.control_info(104)["reported"])
        self.assertTrue(self.diagram.control_info(105)["reported"])
        self.assertFalse(self.diagram.control_info(LIGHT)["reported"])
        self.assertEqual(self.diagram.control_info(LIGHT)["state"], "not reported")
        self.assertEqual(self.fill(105), self.diagram.HELD_FILL)
        self.assertEqual(self.fill(104), self.diagram.REST_FILL)
        self.assertFalse(self.diagram.itemcget(self.body(104), "dash"))
        self.assertTrue(self.diagram.itemcget(self.body(LIGHT), "dash"))

    def test_mapping_and_selection_are_independent_of_live_press_and_support(self):
        self.diagram.set_available([1, 2, 11])
        self.diagram.set_mappings({"1": 2})
        self.diagram.set_selected(1)
        self.assertEqual(self.diagram.control_info(1)["target"], 2)
        self.assertTrue(self.diagram.control_info(1)["available"])
        self.assertFalse(self.diagram.control_info(95)["available"])
        self.assertEqual(self.diagram.itemcget(self.body(1), "outline"), self.diagram.HELD_FILL)
        self.diagram.set_state({"buttons": ["RB"]})
        self.assertEqual(self.fill(6), self.diagram.HELD_FILL)
        self.assertEqual(self.fill(1), self.diagram.REST_FILL)
        self.assertIsNotNone(self.diagram.button_center(95))

    def test_sticks_follow_full_range_and_return_to_center(self):
        before = self.diagram.coords(self.body(11))
        self.diagram.set_state({"left_stick": [-1, 1], "right_stick": [1, -1]})
        moved = self.diagram.coords(self.body(11))
        self.assertLess(moved[0], before[0])
        self.assertLess(moved[1], before[1])
        self.assertEqual(self.diagram.hit_test(moved[2], (moved[1] + moved[3]) / 2), 11)
        self.diagram.set_state(None)
        self.assertEqual(self.diagram.coords(self.body(11)), before)



    def test_invalid_view_raises_before_drawing(self):
        with self.assertRaises(ValueError):
            self.diagram.set_view("side")
        self.assertEqual(self.diagram.view, "front")


if __name__ == "__main__":
    unittest.main()
