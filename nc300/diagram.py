"""Scalable, clickable NC300 drawings with honest Windows input feedback.

The diagram has no hardware connection. ``set_state`` accepts the dictionaries
returned by :mod:`nc300.xinput`; those are controller *outputs* after remapping.
Only a caller supplying ``physical_codes`` can independently identify physical
buttons. Local-only controls therefore never light up from a guessed output.
"""
from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Iterable, Mapping
from typing import Any


LIGHT = -1
SYNC = -2
SETTINGS = -3

CONTROL_INFO: dict[int, dict[str, Any]] = {
    1: {"label": "A", "input": "A"},
    2: {"label": "B", "input": "B"},
    3: {"label": "X", "input": "X"},
    4: {"label": "Y", "input": "Y"},
    5: {"label": "L / LB", "short": "L", "input": "LB"},
    6: {"label": "R / RB", "short": "R", "input": "RB"},
    7: {"label": "ZL / LT", "short": "ZL", "analog": "left_trigger"},
    8: {"label": "ZR / RT", "short": "ZR", "analog": "right_trigger"},
    11: {"label": "Left stick click", "short": "L3", "input": "L3"},
    12: {"label": "Right stick click", "short": "R3", "input": "R3"},
    13: {"label": "D-pad Down", "short": "\u25bc", "input": "D-pad Down"},
    14: {"label": "D-pad Up", "short": "\u25b2", "input": "D-pad Up"},
    15: {"label": "D-pad Right", "short": "\u25b6", "input": "D-pad Right"},
    16: {"label": "D-pad Left", "short": "\u25c0", "input": "D-pad Left"},
    17: {"label": "Home", "short": "H"},
    93: {"label": "Minus / Back", "short": "\u2212", "input": "Back"},
    94: {"label": "Plus / Start", "short": "+", "input": "Start"},
    95: {"label": "Capture", "short": "C"},
    96: {"label": "Turbo", "short": "T"},
    97: {"label": "Rear ML (other models)", "short": "ML"},
    104: {"label": "Rear M1", "short": "M1"},
    105: {"label": "Rear M2", "short": "M2"},
    LIGHT: {"label": "Light", "short": "Light"},
    SYNC: {"label": "Sync", "short": "Sync"},
    SETTINGS: {"label": "Rear setting button", "short": "M"},
}
for _info in CONTROL_INFO.values():
    _info["reported"] = "input" in _info or "analog" in _info


class ControllerDiagram(tk.Canvas):
    """Vector NC300 front/rear views, sharing live state and mapping selection.

    ``on_select(code)`` is called for any depicted control, including unsupported
    ones, so the parent can explain their limitations. ``set_available`` controls
    the remapping affordance only; live events continue to display independently.
    """

    BASE_WIDTH = 600
    BASE_HEIGHT = 360
    REST_FILL = "#272e3d"
    HELD_FILL = "#a3b1ff"
    UNREPORTED_FILL = "#20242e"

    def __init__(self, master: tk.Misc, *, view: str = "front",
                 on_select: Callable[[int], None] | None = None,
                 width: int = 600, height: int = 340,
                 bg: str = "#191c25", **kwargs: Any) -> None:
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, **kwargs)
        if view not in ("front", "rear"):
            raise ValueError("Controller view must be 'front' or 'rear'")
        self.view = view
        self.on_select = on_select
        self._selected: int | None = None
        self._available: set[int] | None = None
        self._mappings: dict[int, int] = {}
        self._state: dict[str, Any] | None = None
        self._scale = 1.0
        self._offset = (0.0, 0.0)
        self._layout: dict[int, tuple[float, float, float, float, str]] = {}
        self._parts: dict[int, dict[str, int]] = {}
        self._stick_parts: dict[int, tuple[int, int]] = {}
        self._hover: int | None = None
        self._rear_swapped = False
        self._render_pending = False
        self._render_id: str | None = None
        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self._render()

    @property
    def state_source(self) -> str:
        if self._state is None:
            return "No input"
        return self._state.get("source") or (
            "Physical buttons" if "physical_codes" in self._state else "Windows output")

    def _is_reported(self, code: int) -> bool:
        if self._state is not None and "physical_codes" in self._state:
            if "physical_available_codes" not in self._state:
                return True
            return code in set(self._state["physical_available_codes"]) | set(
                self._state.get("seen_codes", ())) | set(self._state["physical_codes"])
        return CONTROL_INFO[code]["reported"]

    @property
    def held_codes(self) -> set[int]:
        """Codes actually reported by the current event source, never inferred."""
        if self._state is None:
            return set()
        if "physical_codes" in self._state:
            return {int(code) for code in self._state["physical_codes"]
                    if int(code) in CONTROL_INFO}
        names = set(self._state.get("buttons", ()))
        held = {code for code, info in CONTROL_INFO.items()
                if info.get("input") in names}
        held.update(code for code in (7, 8) if self._trigger(code) > 0)
        return held

    def control_info(self, code: int) -> dict[str, Any]:
        """Return labels and reporting/remapping status for an inspector panel."""
        info = dict(CONTROL_INFO[code])
        info["reported"] = self._is_reported(code)
        info["code"] = code
        info["available"] = self._available is not None and code in self._available
        info["target"] = self._mappings.get(code, 0)
        info["held"] = code in self.held_codes
        info["state"] = ("pressed" if info["held"] else
                         "not reported" if not info["reported"]
                         else "released" if self._state is not None else "no input")
        return info

    def set_view(self, view: str) -> None:
        if view not in ("front", "rear"):
            raise ValueError("Controller view must be 'front' or 'rear'")
        if view != self.view:
            self.view = view
            self._render()

    def set_rear_swapped(self, swapped: bool) -> None:
        """Align the two observed rear signals to the owner's physical unit."""
        if self._rear_swapped != bool(swapped):
            self._rear_swapped = bool(swapped)
            self._render()

    def set_state(self, state: dict[str, Any] | None) -> None:
        self._state = state
        self._paint()

    def set_selected(self, code: int | None) -> None:
        self._selected = code
        self._paint()

    def set_available(self, codes: Iterable[int]) -> None:
        self._available = {int(code) for code in codes}
        self._paint()

    def set_mappings(self, mapping: Mapping[str | int, int]) -> None:
        values = {int(code): int(target) for code, target in mapping.items()}
        if values != self._mappings:
            self._mappings = values
            self._render()



    def button_center(self, code: int) -> tuple[float, float] | None:
        shape = self._layout.get(code)
        if shape is None:
            return None
        x, y, _, _, _ = shape
        return self._xy(x, y)

    def hit_test(self, x: float, y: float) -> int | None:
        """Find a visible control from widget coordinates, including resized views."""
        bx = (x - self._offset[0]) / self._scale
        by = (y - self._offset[1]) / self._scale
        for code, (cx, cy, width, height, shape) in reversed(tuple(self._layout.items())):
            if code in (11, 12):
                width = height = 78  # the complete stick well is clickable
            dx, dy = bx - cx, by - cy
            if shape == "oval":
                if (dx / (width / 2)) ** 2 + (dy / (height / 2)) ** 2 <= 1:
                    return code
            elif abs(dx) <= width / 2 and abs(dy) <= height / 2:
                return code
        return None

    def _on_configure(self, _event: tk.Event) -> None:
        if not self._render_pending:
            self._render_pending = True
            self._render_id = self.after_idle(self._render)

    def destroy(self) -> None:
        if self._render_id is not None:
            self.after_cancel(self._render_id)
            self._render_id = None
        super().destroy()

    def _xy(self, x: float, y: float) -> tuple[float, float]:
        return x * self._scale + self._offset[0], y * self._scale + self._offset[1]

    def _coords(self, *values: float) -> list[float]:
        return [value * self._scale + self._offset[index % 2]
                for index, value in enumerate(values)]

    def _text(self, x: float, y: float, text: str, *, size: int = 10,
              fill: str = "#8d94a7", bold: bool = False, **kwargs: Any) -> int:
        return self.create_text(*self._xy(x, y), text=text, fill=fill,
                                font=("Segoe UI", max(7, round(size * self._scale)),
                                      "bold" if bold else "normal"), **kwargs)


    def _rounded_rect(self, x1: float, y1: float, x2: float, y2: float, **options: Any) -> int:
        radius = min(7 * self._scale, (y2 - y1) / 3, (x2 - x1) / 3)
        points = (x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
                  x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
                  x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1)
        return self.create_polygon(*points, smooth=True, splinesteps=16, **options)

    def _button(self, code: int, x: float, y: float, width: float, height: float,
                *, shape: str = "oval", label: str | None = None) -> None:
        self._layout[code] = (x, y, width, height, shape)
        tags = ("control", f"control:{code}")
        bounds = self._coords(x - width / 2, y - height / 2,
                              x + width / 2, y + height / 2)
        creator = self.create_oval if shape == "oval" else self._rounded_rect
        body = creator(*bounds, fill=self.REST_FILL, outline="#515c72",
                       width=max(1, self._scale * 1.5), tags=tags + (f"body:{code}",))
        parts = self._parts[code] = {"body": body}
        if code in (7, 8):
            parts["analog"] = self.create_rectangle(*bounds, fill=self.HELD_FILL,
                                                     width=0, tags=tags)
        info = CONTROL_INFO[code]
        text = label or info.get("short", info["label"])
        parts["label"] = self._text(x, y, text, size=11 if width > 28 else 10,
                                    fill="#edf0f8", bold=True, tags=tags)
        parts["unreported"] = self._text(x + width / 2 - 1, y - height / 2,
                                          "\u00b7", size=18, fill="#8d94a7", tags=tags)
        target = self._mappings.get(code, 0)
        if target not in (0, code):
            tx, ty = x + width / 2 + 3, y + height / 2 - 1
            badge = self.create_oval(*self._coords(tx - 8, ty - 8, tx + 8, ty + 8),
                                     fill="#b9c5ff", outline="#162231", tags=tags)
            parts["mapping"] = badge
            target_info = CONTROL_INFO.get(target, {})
            short = target_info.get("short", target_info.get("label", "\u2197"))
            if len(short) > 2:
                short = "\u2197"
            self._text(tx, ty, short, size=8, fill="#112237", bold=True, tags=tags)

    def _stick(self, code: int, x: float, y: float) -> None:
        self.create_oval(*self._coords(x - 39, y - 39, x + 39, y + 39),
                         fill="#151922", outline="#485267", width=self._scale * 2)
        self.create_oval(*self._coords(x - 34, y - 34, x + 34, y + 34),
                         fill="#171c27", outline="#69738a", width=max(1, self._scale * 1.4))
        self.create_arc(*self._coords(x - 35, y - 35, x + 35, y + 35), start=35, extent=120,
                        style="arc", outline="#a0aabe", width=max(1, self._scale))
        self.create_oval(*self._coords(x - 28, y - 28, x + 28, y + 28),
                         fill="#222936", outline="#343d50", width=self._scale)
        self._button(code, x, y, 43, 43)
        cross = self.create_line(*self._coords(x - 5, y, x + 5, y),
                                 fill="#79869f", width=self._scale, tags=(f"stick:{code}",))
        vertical = self.create_line(*self._coords(x, y - 5, x, y + 5),
                                    fill="#79869f", width=self._scale, tags=(f"stick:{code}",))
        self._stick_parts[code] = (cross, vertical)

    def _render(self) -> None:
        if self._render_id is not None:
            self.after_cancel(self._render_id)
        self._render_pending = False
        self._render_id = None
        if not self.winfo_exists():
            return
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            width = int(self.cget("width"))
        if height <= 1:
            height = int(self.cget("height"))
        self._scale = max(0.15, min(width / self.BASE_WIDTH, height / self.BASE_HEIGHT))
        self._offset = ((width - self.BASE_WIDTH * self._scale) / 2,
                        (height - self.BASE_HEIGHT * self._scale) / 2)
        self.delete("all")
        self._layout.clear()
        self._parts.clear()
        self._stick_parts.clear()
        outline = (122, 82, 178, 72, 249, 75, 300, 77, 351, 75, 422, 72, 478, 82,
                   513, 124, 533, 194, 540, 265, 531, 309, 512, 324, 489, 319,
                   473, 288, 447, 250, 419, 246, 300, 247, 181, 246, 153, 250,
                   127, 288, 111, 319, 88, 324, 69, 309, 60, 265, 67, 194, 87, 124)
        self.create_polygon(*self._coords(*[v + (4 if n % 2 else 0)
                                             for n, v in enumerate(outline)]),
                            smooth=True, splinesteps=30, fill="#0c0e14", outline="")
        self.create_polygon(*self._coords(*outline), smooth=True, splinesteps=30,
                            fill="#282e3b", outline="#566078", width=self._scale * 1.8)
        self.create_line(*self._coords(130, 85, 185, 79, 249, 82, 300, 84, 351, 82, 415, 79, 470, 85),
                         smooth=True, fill="#788297", width=max(1, self._scale), capstyle="round")
        # Grip seams retain the proportions and offset sticks of the NC300 shell.
        for side in (-1, 1):
            seam = [300 + side * v for v in (120, 144, 167, 187, 191)]
            self.create_line(*self._coords(seam[0], 94, seam[1], 144, seam[2], 204,
                                           seam[3], 265, seam[4], 306),
                             smooth=True, fill="#363e50", width=self._scale * 1.5)
        self._text(300, 22, "FRONT" if self.view == "front" else "REAR", size=9, bold=True)
        if self.view == "front":
            self._front()
        else:
            self._rear()
        self._paint()

    def _shoulders(self, *, mirrored: bool = False) -> None:
        for x, trigger, bumper in ((173, 7, 5), (427, 8, 6)):
            if mirrored:
                x = 600 - x
            self._button(trigger, x, 45, 91, 23, shape="rect")
            self._button(bumper, x, 72, 110, 22, shape="rect")

    def _front(self) -> None:
        self._shoulders()
        self.create_oval(*self._coords(377, 92, 481, 196),
                         fill="#1c222e", outline="#3b4557", width=max(1, self._scale))
        self._stick(11, 178, 145)
        self._stick(12, 368, 219)
        self._button(93, 253, 113, 23, 23)
        self._button(94, 347, 113, 23, 23)
        self._button(96, 274, 147, 23, 23)
        self._button(95, 326, 147, 23, 23)
        self._button(17, 300, 178, 24, 24)
        for code, x, y in ((4, 429, 114), (3, 400, 144),
                           (2, 458, 144), (1, 429, 174)):
            self._button(code, x, y, 29, 29)
        self.create_oval(*self._coords(204, 189, 264, 249),
                         fill="#151922", outline="#3c4557")
        for code, x, y in ((14, 234, 198), (13, 234, 240),
                           (16, 213, 219), (15, 255, 219)):
            self._button(code, x, y, 23, 23, shape="rect")
        self.create_rectangle(*self._coords(224, 209, 244, 229), fill="#272e3d", outline="")
        for x in (285, 295, 305, 315):
            self.create_oval(*self._coords(x - 2, 239, x + 2, 243), fill="#56627a", outline="")

    def _rear(self) -> None:
        self._shoulders(mirrored=True)
        self.create_rectangle(*self._coords(245, 110, 355, 157),
                              fill="#233147", outline="#465871", width=self._scale)
        self._text(300, 133, "NC300", size=14, bold=True, fill="#5a6f8c")
        self._button(104 if self._rear_swapped else 105, 205, 190, 36, 54, shape="rect")
        self._button(105 if self._rear_swapped else 104, 395, 190, 36, 54, shape="rect")
        self._button(LIGHT, 260, 223, 36, 23)
        self._button(SYNC, 300, 223, 32, 23)
        self._button(SETTINGS, 340, 223, 25, 23)
        self._text(300, 277, "Click a control to inspect its input availability.\nDotted controls have no reported PC signal.",
                   size=9, justify="center")

    def _trigger(self, code: int) -> float:
        if self._state is None:
            return 0.0
        value = float(self._state.get(CONTROL_INFO[code]["analog"], 0))
        return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0

    def _paint(self) -> None:
        held = self.held_codes
        for code, parts in self._parts.items():
            info = CONTROL_INFO[code]
            pressed = code in held
            unreported = not self._is_reported(code)
            unsupported = self._available is not None and code not in self._available
            mapped = self._mappings.get(code, 0) not in (0, code)
            selected = code == self._selected
            fill = self.HELD_FILL if pressed else self.UNREPORTED_FILL if unreported else self.REST_FILL
            outline = ("#a3b1ff" if selected else "#b9c5ff" if mapped else
                       "#8d94a7" if code == self._hover else "#373e50" if unsupported else "#515a70")
            self.itemconfigure(parts["body"], fill=fill, outline=outline,
                               width=max(1, self._scale * (3 if selected else 1.5)),
                               dash=(2, 3) if unreported else ())
            self.itemconfigure(parts["label"], fill="#151a2f" if pressed else
                               "#8d94a7" if unreported or unsupported else "#edf0f8")
            self.itemconfigure(parts["unreported"], state="normal" if unreported else "hidden")
            if "analog" in parts:
                x, y, width, height, _ = self._layout[code]
                fraction = self._trigger(code)
                self.coords(parts["analog"], *self._coords(
                    x - width / 2 + 2, y + height / 2 - 4,
                    x - width / 2 + 2 + (width - 4) * fraction, y + height / 2 - 2))
                self.itemconfigure(parts["analog"], state="normal" if fraction else "hidden")
                self.itemconfigure(parts["label"], text=f"{info['short']}  {round(fraction * 100)}%")
        for code, (horizontal, vertical) in self._stick_parts.items():
            state_key = "left_stick" if code == 11 else "right_stick"
            pair = self._state.get(state_key, (0, 0)) if self._state else (0, 0)
            dx, dy = (max(-1.0, min(1.0, float(value))) if math.isfinite(float(value)) else 0
                      for value in pair)
            x, y, width, height, _ = self._layout[code]
            x += dx * 13
            y -= dy * 13
            body = self._parts[code]["body"]
            self.coords(body, *self._coords(x - width / 2, y - height / 2,
                                            x + width / 2, y + height / 2))
            self.coords(self._parts[code]["label"], *self._xy(x, y - 2))
            self.coords(horizontal, *self._coords(x - 5, y + 9, x + 5, y + 9))
            self.coords(vertical, *self._coords(x, y + 4, x, y + 14))

    def _click(self, event: tk.Event) -> None:
        code = self.hit_test(event.x, event.y)
        if code is not None and self.on_select is not None:
            self.on_select(code)

    def _motion(self, event: tk.Event) -> None:
        hovered = self.hit_test(event.x, event.y)
        if hovered != self._hover:
            self._hover = hovered
            self.configure(cursor="hand2" if hovered is not None and self.on_select else "")
            self._paint()

    def _leave(self, _event: tk.Event) -> None:
        self._hover = None
        self.configure(cursor="")
        self._paint()
