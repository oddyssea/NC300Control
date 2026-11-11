import unittest

from nc300.device import ControllerError, NC300Device
from nc300.vendor import protocol as p

from tests.fixtures import (
    CAPABILITIES, IDENTITY, MOTORS, REMAP_SOURCES, STICKS, FakeBleakClient,
)


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeBleakClient()
        self.device = NC300Device("fake", timeout=0.025, client_factory=self.client.factory)
        await self.device.connect()

    async def asyncTearDown(self):
        await self.device.disconnect()

    async def test_snapshot_decodes_observed_defaults_without_writes(self):
        snapshot = await self.device.read_snapshot()
        self.assertEqual(snapshot["name"], "NC300")
        self.assertEqual(snapshot["firmware"], "1.06")
        self.assertTrue(snapshot["writable"])
        self.assertEqual(snapshot["remap_sources"], REMAP_SOURCES)
        self.assertEqual(snapshot["remap_targets"][-1], 0)
        self.assertEqual(snapshot["raw"]["remappings"], "")
        self.assertEqual(snapshot["remappings"], {})
        self.assertEqual(snapshot["capabilities"]["lighting_zones"], 0)
        self.assertEqual(snapshot["errors"], {})
        self.assertEqual(self.client.settings_writes, [])

    async def test_invalid_frames_and_unrelated_responses_do_not_complete_query(self):
        def noise(request):
            valid = p.build(p.Op.RESPONSE, b"\x03bad", serial=request.serial, nonce=1)
            bad_crc = bytearray(valid)
            bad_crc[-1] ^= 1
            return [
                b"\x00", bytes(bad_crc),
                p.build(p.Op.RESPONSE, b"\x03bad", serial=request.serial,
                        length_field=6, nonce=1),
                p.build(p.Op.RESPONSE, b"\x03bad", serial=request.serial ^ 1, nonce=1),
                p.build(p.Op.HOST_STICK, b"\x03bad", serial=request.serial, nonce=1),
            ]
        self.client.noise_factory = noise
        with self.assertLogs("nc300.device", level="WARNING"):
            record = await self.device._read(p.Op.READ_NAME)
        self.assertEqual(record.data, b"NC300")
        self.assertEqual(self.device._pending, {})

    async def test_silent_response_reports_timeout_and_cleans_pending(self):
        self.client.silent.add(p.Op.READ_NAME)
        with self.assertRaisesRegex(ControllerError, "did not reply"):
            await self.device._read(p.Op.READ_NAME)
        self.assertEqual(self.device._pending, {})

    async def test_continuation_preserves_menu_selector_and_does_not_strip_data_byte(self):
        data = bytes(range(30))
        self.client.records[(p.Op.HOST_MENU, 3)] = data
        record = await self.device._read(p.Op.HOST_MENU, b"\x00\x03")
        self.assertEqual(record.data, data)
        self.assertTrue(record.complete)
        self.assertEqual([request.payload for request in self.client.requests],
                         [b"\x00\x03", b"\x01\x03", b"\x02\x03"])

    async def test_extra_and_truncated_record_bytes_are_rejected(self):
        for first, second in ((b"\x01ab", None), (b"\x03a", b"b"), (b"\x03a", b"")):
            with self.subTest(first=first, second=second):
                self.client.raw_reply_overrides = {(p.Op.READ_NAME, b"\x00"): first}
                if second is not None:
                    for index in range(1, 18):
                        self.client.raw_reply_overrides[(p.Op.READ_NAME, bytes([index]))] = (
                            second if index == 1 else b""
                        )
                with self.assertRaises(ControllerError):
                    await self.device._read(p.Op.READ_NAME)

    async def test_unknown_identity_and_unsupported_motor_cannot_write(self):
        self.client.records[(p.Op.READ_VID_PID_VERSION, None)] = IDENTITY[:-2] + b"\x07\x56"
        with self.assertRaisesRegex(ControllerError, "measured NC300"):
            await self.device.set_motor_strength(25, 25)
        self.client.records[(p.Op.READ_VID_PID_VERSION, None)] = IDENTITY
        caps = bytearray(CAPABILITIES)
        caps[2] = 1
        self.client.records[(p.Op.HOST_MENU, 1)] = bytes(caps)
        with self.assertRaisesRegex(ControllerError, "both grip motors"):
            await self.device.set_motor_strength(25, 25)
        self.assertEqual(self.client.settings_writes, [])

    async def test_invalid_mutation_parameters_reject_before_bluetooth_io(self):
        calls = (
            lambda: self.device.set_motor_strength(True, 20),
            lambda: self.device.set_motor_strength(-1, 20),
            lambda: self.device.set_motor_strength(20, 101),
            lambda: self.device.set_deadzone("lighting", 0, 5),
            lambda: self.device.set_deadzone("sticks", 2, 5),
            lambda: self.device.set_deadzone("triggers", 0, 1.5),
            lambda: self.device.set_mapping(0, 1),
            lambda: self.device.set_mapping(1, 128),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                await call()
        self.assertEqual(self.client.requests, [])

    async def test_motor_change_preserves_unknown_record_bytes_and_backup(self):
        original = bytes.fromhex("7f7f93e580a90300")
        self.client.records[(p.Op.HOST_MOTOR, None)] = original
        result = await self.device.set_motor_strength(25, 75)
        expected = bytes([64, 191]) + original[2:]
        self.assertEqual(self.client.records[(p.Op.HOST_MOTOR, None)], expected)
        self.assertEqual(result["raw"]["motors"], expected.hex())
        self.assertEqual(self.device.last_backup["raw"]["motors"], original.hex())
        self.assertEqual(result["raw"]["sticks"], STICKS.hex())
        self.assertEqual(result["errors"], {})

    async def test_deadzone_change_keeps_curve_flags_other_side_and_checks_travel(self):
        original = bytearray(STICKS)
        original[6] = 0xC5
        self.client.records[(p.Op.HOST_STICK, None)] = bytes(original)
        result = await self.device.set_deadzone("sticks", 0, 15)
        expected = bytearray(original)
        expected[0] = 15
        self.assertEqual(result["raw"]["sticks"], expected.hex())
        self.assertEqual(self.device.last_backup["raw"]["sticks"], original.hex())
        count_before = len(self.client.settings_writes)
        with self.assertRaises(ValueError):
            await self.device.set_deadzone("sticks", 0, 95)
        self.assertEqual(len(self.client.settings_writes), count_before)

    async def test_unknown_motor_layout_cannot_write(self):
        self.client.records[(p.Op.HOST_MOTOR, None)] = MOTORS + b"\x11"
        with self.assertRaisesRegex(ControllerError, "unknown motor settings layout"):
            await self.device.set_motor_strength(30, 30)
        self.assertEqual(self.client.settings_writes, [])

    async def test_write_ack_without_exact_readback_is_failure(self):
        self.client.apply_writes = False
        with self.assertLogs("nc300.device", level="ERROR"):
            with self.assertRaisesRegex(ControllerError, "read-back differs"):
                await self.device.set_motor_strength(20, 30)
        self.assertTrue(self.client.settings_writes)
        self.assertEqual(self.device.last_backup["raw"]["motors"], MOTORS.hex())
        self.assertEqual(self.client.records[(p.Op.HOST_MOTOR, None)], MOTORS)

    async def test_lost_write_ack_is_failure_with_backup_retained(self):
        self.client.silent.add(p.Op.WRITE_MOTOR)
        with self.assertLogs("nc300.device", level="ERROR"):
            with self.assertRaisesRegex(ControllerError, "did not reply to write motors"):
                await self.device.set_motor_strength(20, 30)
        self.assertEqual(self.device.last_backup["raw"]["motors"], MOTORS.hex())
        self.assertEqual(self.device._pending, {})

    async def test_mapping_keeps_existing_targets_and_spans_write_chunks(self):
        original = bytearray(len(REMAP_SOURCES))
        original[1] = 4
        self.client.records[(p.Op.HOST_CHANGEKEY, None)] = bytes(original)
        snapshot = await self.device.set_mapping(1, 2)
        expected = bytearray(original)
        expected[0] = 2
        self.assertEqual(self.client.records[(p.Op.HOST_CHANGEKEY, None)], bytes(expected))
        self.assertEqual(snapshot["remappings"], {"1": 2, "2": 4})
        self.assertEqual(len(self.client.settings_writes), 2)
        self.assertEqual(self.device.last_backup["raw"]["remappings"], original.hex())

    async def test_mapping_restore_accepts_observed_empty_default_normalization(self):
        current = bytearray(len(REMAP_SOURCES))
        current[0] = 2
        self.client.records[(p.Op.HOST_CHANGEKEY, None)] = bytes(current)
        snapshot = await self.device.set_mapping(1, 1)
        self.assertEqual(snapshot["remappings"], {})
        self.assertEqual(snapshot["raw"]["remappings"], "")

    async def test_restoring_an_already_default_mapping_does_not_write(self):
        await self.device.set_mapping(1, 1)
        self.assertEqual(self.client.settings_writes, [])

    async def test_restore_motor_backup_checks_layout_and_preserves_live_tail(self):
        await self.device.set_motor_strength(20, 30)
        result = await self.device.apply_raw("motors", MOTORS.hex())
        self.assertEqual(result["raw"]["motors"], MOTORS.hex())
        count_before = len(self.client.settings_writes)
        altered_tail = MOTORS[:2] + b"\x01" + MOTORS[3:]
        with self.assertRaisesRegex(ControllerError, "does not edit"):
            await self.device.apply_raw("motors", altered_tail.hex())
        with self.assertRaises(ControllerError):
            await self.device.apply_raw("motors", MOTORS[:-1].hex())
        self.assertEqual(len(self.client.settings_writes), count_before)

    async def test_restore_rejects_noneditable_category_and_malformed_hex_before_io(self):
        for section, value in (("lighting", "00"), ("firmware", ""),
                               ("motors", "xyz"), ("sticks", None)):
            with self.subTest(section=section, value=value), self.assertRaises(ValueError):
                await self.device.apply_raw(section, value)
        self.assertEqual(self.client.requests, [])

    async def test_restore_empty_default_remaps_and_refuse_invalid_target(self):
        current = bytearray(len(REMAP_SOURCES))
        current[0] = 2
        self.client.records[(p.Op.HOST_CHANGEKEY, None)] = bytes(current)
        result = await self.device.apply_raw("remappings", "")
        self.assertEqual(result["remappings"], {})
        count_before = len(self.client.settings_writes)
        unsupported = bytearray(current)
        unsupported[1] = 104
        with self.assertRaisesRegex(ControllerError, "unsupported target"):
            await self.device.apply_raw("remappings", unsupported.hex())
        self.assertEqual(len(self.client.settings_writes), count_before)

    async def test_malformed_support_list_cannot_supply_remapping_defaults(self):
        # Count says two keys but an identical literal appears twice.
        self.client.records[(p.Op.HOST_MENU, 3)] = b"\x02\x01\x01"
        with self.assertLogs("nc300.device", level="WARNING"):
            snapshot = await self.device.read_snapshot()
        self.assertEqual(snapshot["remap_sources"], [])
        self.assertIn("remap_sources", snapshot["errors"])
        with self.assertLogs("nc300.device", level="WARNING"):
            with self.assertRaises(ControllerError):
                await self.device.set_mapping(1, 2)
        self.assertEqual(self.client.settings_writes, [])

    async def test_mapping_outside_reported_lists_is_rejected(self):
        for source, target in ((104, 1), (1, 104)):
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                await self.device.set_mapping(source, target)
        self.assertEqual(self.client.settings_writes, [])

    async def test_missing_configuration_service_disconnects_failed_client(self):
        await self.device.disconnect()
        self.client.services = []
        with self.assertLogs("nc300.device", level="ERROR"):
            with self.assertRaisesRegex(ControllerError, "no KeyLinker configuration service"):
                await self.device.connect()
        self.assertFalse(self.device.connected)
        self.assertFalse(self.client.is_connected)


if __name__ == "__main__":
    unittest.main()
