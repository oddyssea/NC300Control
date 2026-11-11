"""Pinned data from this NC300's read-only probe, plus BLE test doubles.

These records were captured from firmware 1.06 in operating mode 6. Tests do
not import a probe file from outside the project or contact the controller.
"""

from types import SimpleNamespace

from nc300.vendor import protocol as p

IDENTITY = bytes.fromhex("10382003010656")
CAPABILITIES = bytes.fromhex("03030301c0030002c000")
ALL_KEYS_RECORD = bytes.fromhex("1701880b895d8568")
REMAP_SOURCES_RECORD = bytes.fromhex("1001880d840b825d82")
REMAP_TARGETS_RECORD = bytes.fromhex("1101880d840b825d8200")
REMAP_SOURCES = [1, 2, 3, 4, 5, 6, 7, 8, 13, 14, 15, 16, 11, 12, 93, 94]
STICKS = bytes.fromhex("0a0a5555aaaa000a0a5555aaaa00")
TRIGGERS = bytes.fromhex("0a325555aaaa000a325555aaaa00")
MOTORS = bytes.fromhex("7f7f000080a90300")

# Raw encrypted notifications, serial, payload, captured without writes.
CAPTURED_NOTIFICATIONS = (
    ("765ce65ec79c05408511888bb9", 1, "0710382003010656"),
    ("c1ada41027ac9e0cfd01f4", 130, "054e43333030"),
    ("a510370b7bdeefb234815e0ee5621d51", 3, "0a03030301c0030002c000"),
    ("6d4ce75fc78f2e2cd8a32fc625708068927bd158", 7, "0e0a0a5555aaaa000a0a5555aaaa00"),
    ("cff081bf2358fd17ef2b8e678d11", 9, "087f7f000080a90300"),
    ("6952eb67cdbe", 11, "00"),
)


class FakeBleakClient:
    """A record-based controller model, independent of the device write logic."""

    WRITE_TO_READ = {
        p.Op.WRITE_STICK: p.Op.HOST_STICK,
        p.Op.WRITE_TRIGGER: p.Op.HOST_TRIGGER,
        p.Op.WRITE_MOTOR: p.Op.HOST_MOTOR,
        p.Op.WRITE_CHANGEKEY: p.Op.HOST_CHANGEKEY,
    }

    def __init__(self, address="fake", timeout=12):
        self.address = address
        self.timeout = timeout
        self.is_connected = False
        self.services = [SimpleNamespace(uuid="d7f010e0-660d-46e9-96c3-19c4148bdab5")]
        self.callback = None
        self.requests = []
        self.settings_writes = []
        self.records = {
            (p.Op.READ_VID_PID_VERSION, None): IDENTITY,
            (p.Op.READ_NAME, None): b"NC300",
            (p.Op.HOST_MENU, 1): CAPABILITIES,
            (p.Op.HOST_MENU, 2): ALL_KEYS_RECORD,
            (p.Op.HOST_MENU, 3): REMAP_SOURCES_RECORD,
            (p.Op.HOST_MENU, 7): REMAP_TARGETS_RECORD,
            (p.Op.HOST_STICK, None): STICKS,
            (p.Op.HOST_TRIGGER, None): TRIGGERS,
            (p.Op.HOST_MOTOR, None): MOTORS,
            (p.Op.HOST_TURBO, None): bytes.fromhex("08000000000000000058025802"),
            (p.Op.HOST_CHANGEKEY, None): b"",
            (p.Op.HOST_MACRO, 6): b"",
            (p.Op.HOST_MACRO, 7): b"",
        }
        self.raw_reply_overrides = {}
        self.noise_factory = lambda packet: []
        self.silent = set()
        self.apply_writes = True
        self.normalize_default_remaps = True
        self._write_buffers = {}

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, callback):
        self.callback = callback

    async def stop_notify(self, uuid):
        self.callback = None

    def factory(self, address, timeout=12):
        self.address, self.timeout = address, timeout
        return self

    def _record_reply(self, packet):
        selector = packet.payload[1] if packet.opcode in (p.Op.HOST_MENU, p.Op.HOST_MACRO) else None
        data = self.records[(packet.opcode, selector)]
        position = packet.payload[0] if packet.payload else 0
        if position == 0:
            return bytes([len(data)]) + data[:14]
        start = 14 + (position - 1) * 15
        return data[start:start + 15]

    def _apply_chunk(self, packet):
        self.settings_writes.append(packet)
        chunk = packet.serial >> 3 & 0x0F
        if chunk == 0:
            self._write_buffers[packet.opcode] = bytearray()
        buffer = self._write_buffers[packet.opcode]
        buffer.extend(packet.payload)
        if len(buffer) != 1 + buffer[0] or not self.apply_writes:
            return
        data = bytes(buffer[1:])
        read_op = self.WRITE_TO_READ[packet.opcode]
        if read_op == p.Op.HOST_CHANGEKEY and not any(data) and self.normalize_default_remaps:
            data = b""
        self.records[(read_op, None)] = data

    async def write_gatt_char(self, uuid, wire, response=True):
        packet = p.parse(bytes(wire))
        if not packet.crc_valid:
            raise AssertionError("Application sent a frame with a bad checksum")
        if packet.declared_length != len(wire):
            raise AssertionError("Application sent an inconsistent frame length")
        self.requests.append(packet)
        if packet.opcode in self.WRITE_TO_READ:
            self._apply_chunk(packet)
            payload = b"\x00"
        else:
            payload = self.raw_reply_overrides.get((packet.opcode, packet.payload))
            if payload is None:
                payload = self._record_reply(packet)
        if packet.opcode in self.silent:
            return
        for raw in self.noise_factory(packet):
            self.callback(None, bytearray(raw))
        self.callback(None, bytearray(p.build(p.Op.RESPONSE, payload,
                                             serial=packet.serial, nonce=21)))
