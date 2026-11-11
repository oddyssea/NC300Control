"""Decode KeyLinker generation 2 physical input event payloads.

These are the decrypted opcode 0x1F payloads, including their byte-count prefix.
The official application's generation 2 decoder uses six-byte stick records,
four-byte trigger records, and two-byte button records. Reports are deltas: a
button omitted from a later report keeps its previous state. No I/O lives here.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


BUTTON_NAMES = {
    1: "A", 2: "B", 3: "X", 4: "Y", 5: "LB", 6: "RB",
    9: "Back", 10: "Start", 11: "L3", 12: "R3",
    13: "D-pad Down", 14: "D-pad Up", 15: "D-pad Right", 16: "D-pad Left",
    93: "Back", 94: "Start",
}


def _axis(value: int) -> float:
    """Map the controller's center 128 and both endpoints exactly to -1..1."""
    return (value - 128) / (128 if value < 128 else 127)


class PhysicalInputState:
    """Maintain physical state from validated generation 2 delta reports.

    ``feed`` returns True for an accepted frame and raises ValueError for bad
    input, without changing any prior state. The first valid frame makes
    ``snapshot`` available. ``clear`` resets events while preserving the
    controller's configured reportable key list.

    Stick Y grows down in the Bluetooth records. Snapshots invert it to match
    the existing Canvas/XInput convention, where up is positive. Stick records
    never synthesize L3/R3 clicks. Trigger pressure determines their held state,
    including a nonzero value accompanied by state byte 0.
    """

    def __init__(self, reportable_codes: Iterable[int] = ()) -> None:
        self._reportable: set[int] = set()
        self.set_reportable(reportable_codes)
        self.clear()

    def set_reportable(self, codes: Iterable[int]) -> None:
        values = list(codes)
        if any(type(code) is not int or not 0 <= code <= 255 for code in values):
            raise ValueError("Reportable key codes must be integers from 0 to 255")
        self._reportable = set(values)

    def clear(self) -> None:
        self._held: set[int] = set()
        self._seen: set[int] = set()
        self._sticks = {18: (128, 128), 19: (128, 128)}
        self._raw_sticks = dict(self._sticks)
        self._triggers = {7: 0, 8: 0}
        self._raw_triggers = dict(self._triggers)
        self._report_count = 0

    def feed(self, payload: bytes | bytearray | memoryview) -> bool:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ValueError("Physical input payload must be bytes")
        data = bytes(payload)
        if not data or data[0] != len(data) - 1:
            raise ValueError("Physical input byte count does not match payload length")
        # Parse the entire packet before committing any event, including releases.
        # A truncated final record must never leave earlier presses stuck on.
        records: list[tuple[int, int, bytes]] = []
        offset = 1
        while offset < len(data):
            if len(data) - offset < 2:
                raise ValueError("Truncated physical input record header")
            state, code = data[offset:offset + 2]
            if state not in (0, 1):
                raise ValueError(f"Unknown physical input state {state} for key {code}")
            if code == 92:
                raise ValueError("Motion sensor records are not button input records")
            size = 6 if code in (18, 19) else 4 if code in (7, 8) else 2
            if offset + size > len(data):
                raise ValueError(f"Truncated physical input record for key {code}")
            records.append((state, code, data[offset + 2:offset + size]))
            offset += size

        for state, code, extra in records:
            self._seen.add(code)
            if code in (18, 19):
                self._sticks[code] = (extra[0], extra[1])
                self._raw_sticks[code] = (extra[2], extra[3])
                continue
            if code in (7, 8):
                self._triggers[code] = extra[0]
                self._raw_triggers[code] = extra[1]
                active = extra[0] > 0
            else:
                active = state == 1
            if active:
                self._held.add(code)
            else:
                self._held.discard(code)
        self._report_count += 1
        return True

    def snapshot(self) -> dict[str, Any] | None:
        if not self._report_count:
            return None
        # Every container is fresh so a GUI or logging consumer cannot corrupt
        # the accumulated stream while editing its own copy of a snapshot.
        buttons = list(dict.fromkeys(BUTTON_NAMES[code] for code in sorted(self._held)
                                     if code in BUTTON_NAMES))
        return {
            "buttons": buttons,
            "physical_codes": sorted(self._held),
            "left_trigger": self._triggers[7] / 255,
            "right_trigger": self._triggers[8] / 255,
            "left_stick": [_axis(self._sticks[18][0]), -_axis(self._sticks[18][1])],
            "right_stick": [_axis(self._sticks[19][0]), -_axis(self._sticks[19][1])],
            "raw_axes": {
                "left_stick": list(self._raw_sticks[18]),
                "right_stick": list(self._raw_sticks[19]),
                "left_trigger": self._raw_triggers[7],
                "right_trigger": self._raw_triggers[8],
            },
            "physical_available_codes": sorted(self._reportable | self._seen),
            "seen_codes": sorted(self._seen),
            "source": "Bluetooth physical input",
            "report_count": self._report_count,
        }
