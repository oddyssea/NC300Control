"""Bounded lifecycle for NC300 physical-button reports, before remapping."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .telemetry import PhysicalInputState
from .vendor import protocol as p

LOG = logging.getLogger(__name__)
TEST_STOP = bytes.fromhex("02dfab")
TEST_PHYSICAL = bytes.fromhex("03dfab00")
BUTTON_PAGE = bytes.fromhex("05dfab000605")


class PhysicalMonitor:
    """All lifecycle methods ending in _locked require the device's I/O lock."""

    def __init__(self, device: Any):
        self.device = device
        self.decoder = PhysicalInputState()
        self.enabled = False
        self.streaming = False
        self.error: str | None = None
        self._task: asyncio.Task | None = None

    def snapshot(self) -> dict | None:
        if not self.streaming or self.error or not self.device.connected:
            return None
        return self.decoder.snapshot()

    def feed(self, payload: bytes) -> None:
        if not self.streaming:
            return
        try:
            self.decoder.feed(payload)
        except ValueError as exc:
            if self.error is None:
                LOG.warning("Physical controller input could not be decoded: %s", exc)
            self.error = str(exc)
            self.decoder.clear()
        else:
            self.error = None

    async def _control(self, opcode: int, payload: bytes) -> None:
        if not self.device.connected:
            raise RuntimeError("Controller disconnected during physical-input monitoring")
        packet = p.build(opcode, payload, serial=self.device._serial.next())
        await self.device._client.write_gatt_char(
            "d7f010e1-660d-46e9-96c3-19c4148bdab5", packet, response=True)

    async def enable_locked(self, codes: list[int]) -> None:
        self.decoder.set_reportable(codes)
        self.enabled = True
        await self.resume_locked()

    async def resume_locked(self) -> None:
        if not self.enabled or self.streaming:
            return
        self.decoder.clear()
        self.error = None
        try:
            await self._control(0x1C, TEST_STOP)
            self.streaming = True  # notifications may arrive during the write
            await self._control(0x1B, TEST_PHYSICAL)
            await self._control(p.Op.SET_MODE, BUTTON_PAGE)
            self._task = asyncio.create_task(self._keepalive(), name="NC300 physical buttons")
            LOG.info("Physical-button monitoring started before remapping")
        except Exception as exc:
            self.error = str(exc)
            try:
                await self.pause_locked()
            except Exception:
                LOG.warning("Cleanup after physical-input startup failure also failed", exc_info=True)
            raise

    async def _keepalive(self) -> None:
        try:
            while self.enabled and self.streaming and self.device.connected:
                await asyncio.sleep(0.75)
                async with self.device._lock:
                    if self.enabled and self.streaming:
                        await self._control(p.Op.SET_MODE, BUTTON_PAGE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
            LOG.warning("Physical-button monitoring stopped: %s", exc)
            async with self.device._lock:
                try:
                    await self.pause_locked()
                except Exception:
                    LOG.warning("Physical-input cleanup after transport failure also failed", exc_info=True)

    async def pause_locked(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        errors = []
        try:
            if self.streaming and self.device.connected:
                for opcode in (0x1C, p.Op.NORMAL_MODE):
                    try:
                        await self._control(opcode, TEST_STOP)
                    except Exception as exc:
                        errors.append(str(exc))
        finally:
            self.streaming = False
            self.decoder.clear()
        if errors:
            self.error = "; ".join(errors)
            raise RuntimeError("Could not end physical-input test mode: " + self.error)
        LOG.info("Physical-button monitoring paused")

    async def disable_locked(self) -> None:
        self.enabled = False
        await self.pause_locked()
