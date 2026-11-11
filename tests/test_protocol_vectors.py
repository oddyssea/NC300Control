import unittest

from nc300.vendor import protocol as p

from tests.fixtures import CAPTURED_NOTIFICATIONS, IDENTITY


class CapturedProtocolTests(unittest.TestCase):
    def test_decodes_actual_controller_notifications(self):
        for raw_hex, serial, payload in CAPTURED_NOTIFICATIONS:
            with self.subTest(serial=serial):
                packet = p.parse(bytes.fromhex(raw_hex))
                self.assertEqual(packet.opcode, p.Op.RESPONSE)
                self.assertEqual(packet.serial, serial)
                self.assertEqual(packet.payload, bytes.fromhex(payload))
                self.assertTrue(packet.crc_valid)
                self.assertEqual(packet.declared_length, len(bytes.fromhex(raw_hex)))

    def test_detects_corruption_of_actual_controller_notification(self):
        raw = bytearray.fromhex(CAPTURED_NOTIFICATIONS[0][0])
        raw[-1] ^= 1
        self.assertFalse(p.parse(bytes(raw)).crc_valid)

    def test_observed_identity_selects_generation_two(self):
        info = p.parse_identity_reply(bytes([len(IDENTITY)]) + IDENTITY)
        self.assertEqual((info.vid, info.pid, info.version, info.mode),
                         ("1038", "2003", "1.06", 6))
        self.assertEqual(info.effective_generation, 2)


if __name__ == "__main__":
    unittest.main()
