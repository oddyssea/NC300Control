"""Small, serialized BLE client for the measured NC300 configuration interface.

Settings changes require the exact measured identity, a fresh readable record,
an acknowledgement for every packet, and an exact record read-back. Physical
feedback uses a bounded button-test session. Firmware, reset, calibration, and
lighting writes are not exposed.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from bleak import BleakClient

from .vendor import protocol as p
from .input_monitor import PhysicalMonitor

LOG = logging.getLogger(__name__)
CONFIG_SERVICE = "d7f010e0-660d-46e9-96c3-19c4148bdab5"
CONFIG_WRITE = "d7f010e1-660d-46e9-96c3-19c4148bdab5"
CONFIG_NOTIFY = "d7f010e2-660d-46e9-96c3-19c4148bdab5"
MEASURED_IDENTITY = "10382003010656"
MAX_CHUNKS = 18
MACRO_NAMES = ("M1", "M2", "M3", "M4", "M5", "M6", "ML", "MR")


class ControllerError(RuntimeError):
    """A controller operation failed or its result could not be verified."""


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _key_list(body: p.Body, *, allow_zero: bool = False) -> list[int]:
    """Decode an entire support list, refusing defaults and trailing junk."""
    if not body.complete or not body.data:
        raise ControllerError("The controller returned an incomplete or empty key list")
    data = body.data
    expected = data[0]
    keys: list[int] = []
    consumed = 1
    for value in data[1:]:
        if len(keys) >= expected:
            break
        consumed += 1
        if value & 0x80:
            run = value & 0x7F
            if not keys or run < 2:
                raise ControllerError("Invalid run in the controller key list")
            keys.extend(range(keys[-1] + 1, keys[-1] + run))
        else:
            keys.append(value)
    if len(keys) != expected or consumed != len(data):
        raise ControllerError("The controller key list has an inconsistent length")
    minimum = 0 if allow_zero else 1
    if len(set(keys)) != len(keys) or any(not minimum <= key <= 127 for key in keys):
        raise ControllerError("The controller key list contains duplicate or invalid codes")
    # Cross-check the upstream decoder without using its fallback paths.
    if keys != p.decode_key_list(data):
        raise ControllerError("The controller key list could not be decoded consistently")
    return keys


class NC300Device:
    def __init__(self, address: str, *, timeout: float = 4.0,
                 client_factory: Callable[..., Any] | None = None):
        if not isinstance(address, str) or not address.strip():
            raise ValueError("A Bluetooth address is required")
        self.address = address
        self.timeout = timeout
        self.last_backup: dict[str, Any] | None = None
        self._factory = client_factory or BleakClient
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._serial = p.SerialCounter()
        self._save_counter = 0
        self._pending: dict[int, asyncio.Future[p.Packet]] = {}
        self._input_monitor = PhysicalMonitor(self)

    @property
    def connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected)

    async def connect(self) -> None:
        async with self._lock:
            if self.connected:
                return
            client = self._factory(self.address, timeout=12.0)
            self._client = client
            try:
                await client.connect()
                services = {service.uuid.lower() for service in client.services}
                if CONFIG_SERVICE not in services:
                    raise ControllerError("This Bluetooth device has no KeyLinker configuration service")
                await client.start_notify(CONFIG_NOTIFY, self._on_notify)
                LOG.info("Connected to NC300 configuration service")
            except Exception:
                LOG.exception("Could not connect to the controller configuration service")
                try:
                    await client.disconnect()
                except Exception:
                    LOG.warning("Disconnect after failed connection also failed", exc_info=True)
                self._client = None
                raise

    async def disconnect(self) -> None:
        async with self._lock:
            try:
                await self._input_monitor.disable_locked()
            except Exception:
                LOG.warning("Physical input could not be stopped before disconnect", exc_info=True)
            client, self._client = self._client, None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ControllerError("Controller disconnected"))
            self._pending.clear()
            if client is not None:
                if client.is_connected:
                    try:
                        await client.stop_notify(CONFIG_NOTIFY)
                    except Exception:
                        LOG.warning("Could not stop controller notifications", exc_info=True)
                await client.disconnect()
                LOG.info("Disconnected from controller")

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        try:
            if not 5 <= len(data) <= p.MAX_PACKET:
                raise ValueError(f"Unexpected frame length {len(data)}")
            packet = p.parse(bytes(data))
            if not packet.crc_valid or packet.declared_length != len(data):
                raise ValueError("Invalid controller frame checksum or length")
            if packet.opcode == 0x1F:
                self._input_monitor.feed(packet.payload)
                return
            if packet.opcode != p.Op.RESPONSE:
                LOG.debug("Ignored controller notification opcode 0x%02X", packet.opcode)
                return
            future = self._pending.get(packet.serial)
            if future is not None and not future.done():
                future.set_result(packet)
        except (ValueError, IndexError):
            LOG.warning("Rejected malformed controller notification", exc_info=True)

    async def _exchange(self, packet: bytes, serial: int, *, operation: str) -> p.Packet:
        if not self.connected:
            raise ControllerError("The controller is disconnected")
        future = asyncio.get_running_loop().create_future()
        self._pending[serial] = future
        try:
            await self._client.write_gatt_char(CONFIG_WRITE, packet, response=True)
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise ControllerError(f"The controller did not reply to {operation}") from exc
        except Exception as exc:
            if isinstance(exc, ControllerError):
                raise
            raise ControllerError(f"Bluetooth failed during {operation}: {exc}") from exc
        finally:
            self._pending.pop(serial, None)

    async def _query(self, opcode: int, payload: bytes = b"") -> p.Packet:
        serial = self._serial.next()
        return await self._exchange(p.build(opcode, payload, serial=serial), serial,
                                    operation=f"read 0x{opcode:02X}")

    async def _read(self, opcode: int, payload: bytes = b"\x00") -> p.Body:
        packet = await self._query(opcode, payload)
        try:
            first = p.unwrap(packet.payload)
        except ValueError as exc:
            raise ControllerError(f"Read 0x{opcode:02X} returned no record length") from exc
        data = bytearray(first.data)
        for index in range(1, MAX_CHUNKS):
            if len(data) >= first.declared:
                break
            # Only the first packet has a size prefix. Keep menu/macro selector.
            more = await self._query(opcode, bytes([index]) + payload[1:])
            if not more.payload:
                raise ControllerError(f"Read 0x{opcode:02X} returned an empty continuation")
            data.extend(more.payload)
        if len(data) != first.declared:
            raise ControllerError(f"Read 0x{opcode:02X} returned {len(data)} of {first.declared} bytes")
        return p.Body(first.declared, bytes(data))

    async def _identity(self) -> p.DeviceInfo:
        packet = await self._query(p.Op.READ_VID_PID_VERSION)
        body = p.unwrap(packet.payload)
        if not body.complete:
            raise ControllerError("The controller identity record is incomplete")
        return p.parse_identity_reply(packet.payload)

    async def _capabilities(self) -> p.Capabilities:
        body = await self._read(p.Op.HOST_MENU, b"\x00\x01")
        if len(body.data) != 10:
            raise ControllerError("The controller uses an unknown capability layout")
        return p.parse_capabilities(body)

    async def _support(self, kind: int) -> tuple[p.Body, list[int]]:
        body = await self._read(p.Op.HOST_MENU, bytes([0, kind]))
        return body, _key_list(body, allow_zero=kind == 7)

    async def read_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return await self._snapshot()

    @property
    def input_monitor_error(self) -> str | None:
        return self._input_monitor.error

    def physical_input(self) -> dict | None:
        return self._input_monitor.snapshot()

    async def start_input_monitor(self) -> None:
        async with self._lock:
            info = await self._identity()
            if info.record != MEASURED_IDENTITY or info.effective_generation != 2:
                raise ControllerError("Physical-button monitoring is not verified for this firmware")
            _, codes = await self._support(2)
            # Captured on this NC300 in addition to its incomplete key list.
            await self._input_monitor.enable_locked(sorted(set(codes) | {104, 105}))

    async def stop_input_monitor(self) -> None:
        async with self._lock:
            await self._input_monitor.disable_locked()

    @asynccontextmanager
    async def _edit_lock(self):
        async with self._lock:
            restart_input = self._input_monitor.enabled
            if restart_input:
                await self._input_monitor.pause_locked()
            try:
                yield
            finally:
                if restart_input and self.connected:
                    try:
                        await self._input_monitor.resume_locked()
                    except Exception as exc:
                        # Configuration read-back and input monitoring are separate
                        # results. The UI exposes input_monitor_error explicitly.
                        LOG.warning("Settings workflow ended, but physical-input monitoring could not resume: %s", exc)

    async def _snapshot(self) -> dict[str, Any]:
        if not self.connected:
            raise ControllerError("The controller is disconnected")
        result: dict[str, Any] = {
            "name": "NC300", "address": self.address, "firmware": None,
            "identity": None, "capabilities": None, "raw": {},
            "remap_sources": [], "remap_targets": [], "remappings": {},
            "errors": {}, "captured_at": datetime.now(timezone.utc).isoformat(),
        }

        async def capture(key: str, action: Callable[[], Any]) -> Any:
            try:
                return await action()
            except Exception as exc:
                LOG.warning("Controller read failed for %s: %s", key, exc)
                result["errors"][key] = str(exc)
                return None

        info = await capture("identity", self._identity)
        if info is not None:
            result["identity"] = {**asdict(info), "effective_generation": info.effective_generation}
            result["firmware"] = info.version
            result["writable"] = info.record == MEASURED_IDENTITY
        name = await capture("name", lambda: self._read(p.Op.READ_NAME))
        if name is not None:
            result["name"] = name.as_text()
            result["raw"]["name"] = name.data.hex()
        caps = await capture("capabilities", self._capabilities)
        if caps is None:
            return result
        result["capabilities"] = {
            **asdict(caps), "supported": caps.supported(),
            "motor_count": caps.motor_count, "macro_slots": caps.macro_slots,
            "lighting_zones": caps.lighting_zones,
        }
        result["raw"]["capabilities"] = caps.raw

        for section, enabled, opcode, scale in (
            ("sticks", caps.sticks, p.Op.HOST_STICK, p.STICK_MAX_PROGRESS),
            ("triggers", caps.triggers, p.Op.HOST_TRIGGER, p.TRIGGER_MAX_PROGRESS),
        ):
            if not enabled:
                continue
            body = await capture(section, lambda opcode=opcode: self._read(opcode))
            if body is not None:
                result["raw"][section] = body.data.hex()
                try:
                    if body.declared != 2 * p.CHANNEL_SIZE:
                        raise ControllerError(f"Unknown {section} record layout")
                    result[section] = [
                        {**asdict(channel),
                         "inner_percent": round(channel.inner_deadzone * 100 / scale),
                         "outer_percent": round(channel.outer_deadzone * 100 / scale)}
                        for channel in p.parse_curves(body, scale)
                    ]
                except ValueError as exc:
                    result["errors"][section] = str(exc)
                except ControllerError as exc:
                    result["errors"][section] = str(exc)

        if caps.motors:
            body = await capture("motors", lambda: self._read(p.Op.HOST_MOTOR))
            if body is not None:
                result["raw"]["motors"] = body.data.hex()
                if len(body.data) >= 2:
                    result["motors"] = {"left": round(body.data[0] * 100 / 255),
                                        "right": round(body.data[1] * 100 / 255)}
                else:
                    result["errors"]["motors"] = "The motor record has fewer than two strength bytes"

        if caps.changekey:
            sources = await capture("remap_sources", lambda: self._support(3))
            if sources is not None:
                result["raw"]["remap_sources"] = sources[0].data.hex()
                result["remap_sources"] = sources[1]
            if caps.remap_select_list:
                targets = await capture("remap_targets", lambda: self._support(7))
                if targets is not None:
                    result["raw"]["remap_targets"] = targets[0].data.hex()
                    result["remap_targets"] = targets[1]
            else:
                result["remap_targets"] = list(result["remap_sources"])
            body = await capture("remappings", lambda: self._read(p.Op.HOST_CHANGEKEY))
            if body is not None:
                result["raw"]["remappings"] = body.data.hex()
                if body.declared and sources is not None:
                    try:
                        if body.declared != len(sources[1]):
                            raise ControllerError("The remapping record does not match its source list")
                        result["remappings"] = {
                            str(source): target for source, target in
                            p.parse_changekey(sources[1], body.data).items()
                        }
                    except (ValueError, ControllerError) as exc:
                        result["errors"]["remappings"] = str(exc)

        if caps.turbo:
            body = await capture("turbo", lambda: self._read(p.Op.HOST_TURBO))
            if body is not None:
                result["raw"]["turbo"] = body.data.hex()
        if caps.macros:
            # Preserve bytes only: macro entry-count semantics differ by firmware.
            for index, label in enumerate(MACRO_NAMES):
                if caps.macros & (1 << index):
                    key = f"macro_{label}"
                    body = await capture(key, lambda index=index:
                                         self._read(p.Op.HOST_MACRO, bytes([0, index])))
                    if body is not None:
                        result["raw"][key] = body.data.hex()
        if caps.lighting:
            body = await capture("lighting", lambda: self._read(p.Op.HOST_LIGHTING))
            if body is not None:
                result["raw"]["lighting"] = body.data.hex()
        return result

    async def _before_change(self, category: str) -> tuple[p.DeviceInfo, p.Capabilities]:
        info = await self._identity()
        if info.record != MEASURED_IDENTITY:
            raise ControllerError("Settings changes are enabled only for the measured NC300 firmware 1.06")
        caps = await self._capabilities()
        self.last_backup = copy.deepcopy(await self._snapshot())
        identity = self.last_backup["identity"]
        if (identity is None or identity["record"] != MEASURED_IDENTITY
                or self.last_backup["errors"].get(category)):
            raise ControllerError(f"Could not back up the current {category} settings")
        return info, caps

    @staticmethod
    def _records_equal(category: str, expected: bytes, actual: bytes) -> bool:
        # NC300 may represent the factory remap state as a zero-length record
        # or as one zero byte per source. Both mean no remapping; no other
        # record normalization is accepted.
        if (category == "remappings" and len(actual) in (0, len(expected))
                and not any(expected) and not any(actual)):
            return True
        return expected == actual

    async def _write_record(self, write_op: int, read_op: int, data: bytes, category: str) -> None:
        if len(data) > 255:
            raise ControllerError("The controller record is too large")
        payload = bytes([len(data)]) + data
        try:
            for index, offset in enumerate(range(0, len(payload), 15)):
                if index > 15:
                    raise ControllerError("The controller record requires too many write chunks")
                self._save_counter += 1
                packet = p.build_write(write_op, payload[offset:offset + 15],
                                       slot=index, counter=self._save_counter)
                serial = p.parse(packet).serial
                await self._exchange(packet, serial, operation=f"write {category} chunk {index + 1}")
                await asyncio.sleep(0.15)
            actual = await self._read(read_op)
            if not self._records_equal(category, data, actual.data):
                raise ControllerError(f"The controller {category} read-back differs from the requested record")
        except Exception:
            LOG.exception("Controller %s change failed; saved backup is available; some chunks may have applied",
                          category)
            raise
        LOG.info("Controller %s changed and exact record read-back matched", category)

    async def set_motor_strength(self, left: int, right: int) -> dict[str, Any]:
        _integer(left, "Left motor strength", 0, 100)
        _integer(right, "Right motor strength", 0, 100)
        async with self._edit_lock():
            _, caps = await self._before_change("motors")
            if caps.motors & 3 != 3:
                raise ControllerError("The controller does not expose both grip motors")
            body = await self._read(p.Op.HOST_MOTOR)
            if len(body.data) != 8:
                raise ControllerError("The controller uses an unknown motor settings layout")
            data = bytearray(body.data)
            data[0], data[1] = round(left * 255 / 100), round(right * 255 / 100)
            if data != body.data:
                await self._write_record(p.Op.WRITE_MOTOR, p.Op.HOST_MOTOR, bytes(data), "motors")
            return await self._snapshot()

    async def set_deadzone(self, section: str, side: int, inner_percent: int) -> dict[str, Any]:
        if section not in ("sticks", "triggers"):
            raise ValueError("Section must be sticks or triggers")
        _integer(side, "Side", 0, 1)
        _integer(inner_percent, "Inner deadzone", 0, 100)
        read_op, write_op, scale = (
            (p.Op.HOST_STICK, p.Op.WRITE_STICK, p.STICK_MAX_PROGRESS)
            if section == "sticks" else
            (p.Op.HOST_TRIGGER, p.Op.WRITE_TRIGGER, p.TRIGGER_MAX_PROGRESS)
        )
        async with self._edit_lock():
            _, caps = await self._before_change(section)
            flag = caps.sticks if section == "sticks" else caps.triggers
            if not flag & (2 if side == 0 else 1):
                raise ControllerError(f"The controller does not expose this {section} channel")
            body = await self._read(read_op)
            if body.declared != 2 * p.CHANNEL_SIZE:
                raise ControllerError(f"The controller uses an unknown {section} layout")
            data = bytearray(body.data)
            data[side * p.CHANNEL_SIZE] = round(inner_percent * scale / 100)
            for channel in p.parse_curves(p.Body(len(data), bytes(data)), scale):
                channel.validate()
            if data != body.data:
                await self._write_record(write_op, read_op, bytes(data), section)
            return await self._snapshot()

    async def set_mapping(self, source: int, target: int) -> dict[str, Any]:
        _integer(source, "Source button", 1, 127)
        _integer(target, "Target button", 0, 127)
        async with self._edit_lock():
            _, caps = await self._before_change("remappings")
            if not caps.changekey:
                raise ControllerError("The controller does not expose button remapping")
            _, sources = await self._support(3)
            if source not in sources:
                raise ValueError("The selected source is not in the controller's remappable button list")
            _, targets = await self._support(7 if caps.remap_select_list else 3)
            if target not in (0, source) and target not in targets:
                raise ValueError("The selected target is not in the controller's allowed target list")
            body = await self._read(p.Op.HOST_CHANGEKEY)
            if body.declared not in (0, len(sources)):
                raise ControllerError("The current remapping record does not match the source list")
            data = bytearray(body.data if body.declared else bytes(len(sources)))
            data[sources.index(source)] = 0 if target == source else target
            if not self._records_equal("remappings", bytes(data), body.data):
                await self._write_record(p.Op.WRITE_CHANGEKEY, p.Op.HOST_CHANGEKEY,
                                         bytes(data), "remappings")
            return await self._snapshot()

    async def apply_raw(self, section: str, hex_data: str) -> dict[str, Any]:
        """Restore one saved record after validating its exact NC300 layout.

        ``hex_data`` excludes the protocol's length byte. This deliberately
        restores only the four editable categories and cannot restore firmware,
        macros, lighting, calibration, or operating modes.
        """
        opcodes = {
            "sticks": (p.Op.WRITE_STICK, p.Op.HOST_STICK),
            "triggers": (p.Op.WRITE_TRIGGER, p.Op.HOST_TRIGGER),
            "motors": (p.Op.WRITE_MOTOR, p.Op.HOST_MOTOR),
            "remappings": (p.Op.WRITE_CHANGEKEY, p.Op.HOST_CHANGEKEY),
        }
        if section not in opcodes:
            raise ValueError("Only sticks, triggers, motors, and remappings can be restored")
        if not isinstance(hex_data, str):
            raise ValueError("A saved hexadecimal record is required")
        try:
            data = bytes.fromhex(hex_data)
        except ValueError as exc:
            raise ValueError("The saved record is not valid hexadecimal") from exc
        async with self._edit_lock():
            _, caps = await self._before_change(section)
            write_op, read_op = opcodes[section]
            current = await self._read(read_op)
            if section in ("sticks", "triggers"):
                enabled = caps.sticks if section == "sticks" else caps.triggers
                if not enabled or len(data) != 2 * p.CHANNEL_SIZE or current.declared != len(data):
                    raise ControllerError(f"The saved {section} record does not match this controller")
                scale = p.STICK_MAX_PROGRESS if section == "sticks" else p.TRIGGER_MAX_PROGRESS
                for channel in p.parse_curves(p.Body(len(data), data), scale):
                    channel.validate()
            elif section == "motors":
                if caps.motors & 3 != 3 or len(data) != 8 or current.declared != 8:
                    raise ControllerError("The saved motor record does not match this controller")
                # The utility only changes grip strengths. Preserve the live
                # record's unknown bytes and idle timer when restoring them.
                if data[2:] != current.data[2:]:
                    raise ControllerError("The saved motor record changes settings this utility does not edit")
            else:
                if not caps.changekey:
                    raise ControllerError("The controller does not expose remapping")
                _, sources = await self._support(3)
                _, targets = await self._support(7 if caps.remap_select_list else 3)
                if len(data) not in (0, len(sources)) or current.declared not in (0, len(sources)):
                    raise ControllerError("The saved remapping record does not match its source list")
                if data and any(value not in targets and value not in (0, source)
                                for source, value in zip(sources, data)):
                    raise ControllerError("The saved remapping record contains unsupported target buttons")
                if not data:
                    data = bytes(len(sources))
            if not self._records_equal(section, data, current.data):
                await self._write_record(write_op, read_op, data, section)
            return await self._snapshot()
