import asyncio
import unittest

from nc300.device import NC300Device, ControllerError
from nc300.vendor import protocol as p
from fixtures import FakeBleakClient


class MonitoringController(FakeBleakClient):
    def __init__(self):
        super().__init__()
        self.controls = []
        self.fail_pages = False

    async def write_gatt_char(self, uuid, wire, response=True):
        packet = p.parse(bytes(wire))
        if packet.opcode in (0x1B, 0x1C, p.Op.SET_MODE, p.Op.NORMAL_MODE):
            self.controls.append((packet.opcode, packet.payload))
            if self.fail_pages and packet.opcode == p.Op.SET_MODE:
                raise OSError("Simulated disconnected radio")
            return
        await super().write_gatt_char(uuid, wire, response=response)


class InputMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = MonitoringController()
        self.device = NC300Device("fake", client_factory=self.fake.factory, timeout=0.02)
        await self.device.connect()

    async def asyncTearDown(self):
        await self.device.disconnect()

    async def test_physical_mode_rear_events_and_cleanup(self):
        await self.device.start_input_monitor()
        self.assertEqual(self.fake.controls[:3], [
            (0x1C, bytes.fromhex("02dfab")),
            (0x1B, bytes.fromhex("03dfab00")),
            (p.Op.SET_MODE, bytes.fromhex("05dfab000605")),
        ])
        self.assertIsNone(self.device.physical_input())
        for payload in ("020101", "020168", "020169"):
            wire = p.build(0x1F, bytes.fromhex(payload), serial=60, nonce=1)
            self.fake.callback(None, bytearray(wire))
        self.assertEqual(self.device.physical_input()["physical_codes"], [1, 104, 105])
        await self.device.stop_input_monitor()
        self.assertIsNone(self.device.physical_input())
        self.assertEqual(self.fake.controls[-2:], [
            (0x1C, bytes.fromhex("02dfab")), (p.Op.NORMAL_MODE, bytes.fromhex("02dfab"))])
        self.assertFalse(self.fake.settings_writes)

    async def test_monitor_pauses_for_configuration_and_resumes_after_verified_write(self):
        await self.device.start_input_monitor()
        await self.device.set_motor_strength(51, 49)
        self.assertTrue(self.device._input_monitor.streaming)
        self.assertIsNone(self.device.input_monitor_error)
        self.assertEqual(self.fake.controls[3:5], [
            (0x1C, bytes.fromhex("02dfab")), (p.Op.NORMAL_MODE, bytes.fromhex("02dfab"))])
        self.assertEqual(self.fake.controls[-2:], [
            (0x1B, bytes.fromhex("03dfab00")), (p.Op.SET_MODE, bytes.fromhex("05dfab000605"))])
        self.assertEqual(self.fake.records[(p.Op.HOST_MOTOR, None)][:2], bytes([130, 125]))

    async def test_unknown_firmware_cannot_enter_test_mode(self):
        self.fake.records[(p.Op.READ_VID_PID_VERSION, None)] = bytes.fromhex("10382003010756")
        with self.assertRaises(ControllerError):
            await self.device.start_input_monitor()
        self.assertFalse(self.fake.controls)

    async def test_keepalive_failure_surfaces_and_leaves_test_mode(self):
        await self.device.start_input_monitor()
        self.fake.fail_pages = True
        with self.assertLogs("nc300.input_monitor", level="WARNING"):
            await asyncio.sleep(0.9)
        self.assertIn("disconnected", self.device.input_monitor_error)
        self.assertFalse(self.device._input_monitor.streaming)
        self.assertIsNone(self.device.physical_input())
        self.assertEqual(self.fake.controls[-1], (p.Op.NORMAL_MODE, bytes.fromhex("02dfab")))

    async def test_failed_setting_readback_still_resumes_monitor(self):
        await self.device.start_input_monitor()
        self.fake.apply_writes = False
        with self.assertLogs("nc300.device", level="ERROR"):
            with self.assertRaises(ControllerError):
                await self.device.set_motor_strength(51, 49)
        self.assertTrue(self.device._input_monitor.streaming)
        self.assertIsNotNone(self.device.last_backup)


if __name__ == "__main__":
    unittest.main()
