"""Windows XInput monitoring and short, bounded vibration tests."""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes


class Gamepad(ctypes.Structure):
    _fields_ = [("buttons", wintypes.WORD), ("left_trigger", ctypes.c_ubyte),
                ("right_trigger", ctypes.c_ubyte), ("lx", ctypes.c_short),
                ("ly", ctypes.c_short), ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class State(ctypes.Structure):
    _fields_ = [("packet", wintypes.DWORD), ("gamepad", Gamepad)]


class Vibration(ctypes.Structure):
    _fields_ = [("left", wintypes.WORD), ("right", wintypes.WORD)]


BUTTONS = ((0x0001, "D-pad Up"), (0x0002, "D-pad Down"),
           (0x0004, "D-pad Left"), (0x0008, "D-pad Right"),
           (0x0010, "Start"), (0x0020, "Back"), (0x0040, "L3"),
           (0x0080, "R3"), (0x0100, "LB"), (0x0200, "RB"),
           (0x1000, "A"), (0x2000, "B"), (0x4000, "X"), (0x8000, "Y"))

_dll = None
_active_slots: set[int] = set()
_active_lock = threading.Lock()
_slot_locks = [threading.Lock() for _ in range(4)]


def _get_dll():
    global _dll
    if _dll is not None:
        return _dll
    failures = []
    for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            candidate = ctypes.WinDLL(name)
            candidate.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(State)]
            candidate.XInputGetState.restype = wintypes.DWORD
            candidate.XInputSetState.argtypes = [wintypes.DWORD, ctypes.POINTER(Vibration)]
            candidate.XInputSetState.restype = wintypes.DWORD
            _dll = candidate
            return candidate
        except OSError as exc:
            failures.append(str(exc))
    raise OSError("Windows XInput is unavailable: " + "; ".join(failures))


def _axis(value: int) -> float:
    return value / (32768 if value < 0 else 32767)


def get_states() -> list[dict]:
    """Return connected XInput slots, preserving raw signed-axis asymmetry."""
    dll = _get_dll()
    states = []
    for slot in range(4):
        state = State()
        result = dll.XInputGetState(slot, ctypes.byref(state))
        if result == 1167:  # ERROR_DEVICE_NOT_CONNECTED
            continue
        if result:
            raise OSError(result, f"XInputGetState failed for player {slot + 1}")
        pad = state.gamepad
        states.append({"slot": slot, "packet": state.packet,
                       "buttons": [name for mask, name in BUTTONS if pad.buttons & mask],
                       "left_trigger": pad.left_trigger / 255,
                       "right_trigger": pad.right_trigger / 255,
                       "left_stick": [_axis(pad.lx), _axis(pad.ly)],
                       "right_stick": [_axis(pad.rx), _axis(pad.ry)]})
    return states


def _stop(slot: int) -> None:
    result = _get_dll().XInputSetState(slot, ctypes.byref(Vibration(0, 0)))
    if result not in (0, 1167):
        raise OSError(result, f"Could not stop vibration for player {slot + 1}")


def rumble(slot: int, left_percent: int, right_percent: int,
           duration_sec: float = 0.35) -> None:
    """Run one short test; stop in finally even on cancellation or failure."""
    if type(slot) is not int or slot not in range(4):
        raise ValueError("Player slot must be 0 through 3")
    if any(type(value) is not int or not 0 <= value <= 100
           for value in (left_percent, right_percent)):
        raise ValueError("Vibration strengths must be integers from 0 to 100")
    if not 0 < duration_sec <= 2:
        raise ValueError("Vibration tests must last more than 0 and at most 2 seconds")
    with _slot_locks[slot]:
        with _active_lock:
            _active_slots.add(slot)
        try:
            vibration = Vibration(round(left_percent * 65535 / 100),
                                  round(right_percent * 65535 / 100))
            result = _get_dll().XInputSetState(slot, ctypes.byref(vibration))
            if result:
                raise OSError(result, f"Vibration test failed for player {slot + 1}")
            time.sleep(duration_sec)
        finally:
            try:
                _stop(slot)
            finally:
                with _active_lock:
                    _active_slots.discard(slot)


def stop_all_rumble() -> None:
    """Stop only tests started by this utility, without touching unrelated slots."""
    with _active_lock:
        slots = tuple(_active_slots)
    errors = []
    for slot in slots:
        try:
            _stop(slot)
        except OSError as exc:
            errors.append(str(exc))
    if errors:
        raise OSError("; ".join(errors))
