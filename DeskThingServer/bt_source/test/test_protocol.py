"""Golden vectors for the tunnel frame protocol.

Every implementation (btmux.py on the device, btbridge.swift on mac,
bt_source/linux/btbridge, bt_source/win/btbridge.c) must agree on these
bytes. If a test here has to change, every helper has to change with it.
"""
import struct
import unittest

FRAME = '>BIH'  # type(1) streamID(4 BE) len(2 BE)
OPEN, DATA, CLOSE, PING, PONG, HELLO, OPEN_ACK = 1, 2, 3, 4, 5, 6, 7

# The device drops the link after this long without a PONG, and pings this
# often. Every computer-side helper MUST answer PING or the link cannot
# survive — this is not optional behavior.
PING_INTERVAL_S = 5
PONG_DEADLINE_S = 15

# --- protocol v2 -----------------------------------------------------------
# v1 was one-directional: only the device opened streams, and every stream went
# to one hardcoded destination. v2 lets either side open, and an OPEN says what
# it wants to reach. The stream-ID space is split by the high bit so both ends
# can allocate without coordinating.
PROTOCOL_VERSION = 2
NS_MASK = 0x80000000        # set = opened by the computer, clear = by the device
CAP_INBOUND = 1 << 0        # peer accepts computer-originated streams
CAP_TARGETED = 1 << 1       # peer understands OPEN target descriptors
CAP_INET = 1 << 2           # peer will proxy host:port targets to the internet

# OPEN payload kinds. An empty payload keeps its v1 meaning.
KIND_SERVICE = 0x01         # nameLen(1) + ASCII name
KIND_HOSTPORT = 0x02        # hostLen(1) + ASCII host + port(2 BE)

# OPEN_ACK status codes
ACK_OK, ACK_REFUSED, ACK_UNREACHABLE, ACK_UNKNOWN_SERVICE, ACK_BAD_NAMESPACE = range(5)


class FrameProtocol(unittest.TestCase):
    def test_header_is_seven_bytes(self):
        self.assertEqual(struct.calcsize(FRAME), 7)

    def test_open_frame_golden(self):
        self.assertEqual(struct.pack(FRAME, OPEN, 1, 0), b'\x01\x00\x00\x00\x01\x00\x00')

    def test_data_frame_golden(self):
        frame = struct.pack(FRAME, DATA, 0x01020304, 5) + b'hello'
        self.assertEqual(frame, b'\x02\x01\x02\x03\x04\x00\x05hello')

    def test_close_frame_golden(self):
        self.assertEqual(
            struct.pack(FRAME, CLOSE, 0xFFFFFFFF, 0), b'\x03\xff\xff\xff\xff\x00\x00')

    def test_ping_frame_golden(self):
        # Heartbeat frames carry no stream and no payload.
        self.assertEqual(struct.pack(FRAME, PING, 0, 0), b'\x04\x00\x00\x00\x00\x00\x00')

    def test_pong_frame_golden(self):
        self.assertEqual(struct.pack(FRAME, PONG, 0, 0), b'\x05\x00\x00\x00\x00\x00\x00')

    def test_heartbeat_deadline_allows_missed_pings(self):
        # The deadline must span more than one ping so a single dropped frame
        # doesn't tear down a healthy link.
        self.assertGreaterEqual(PONG_DEADLINE_S, 3 * PING_INTERVAL_S)

    def test_roundtrip(self):
        for t, sid, payload in [
            (OPEN, 1, b''),
            (DATA, 2 ** 32 - 1, b'x' * 660),
            (CLOSE, 42, b''),
        ]:
            frame = struct.pack(FRAME, t, sid, len(payload)) + payload
            rt, rsid, rlen = struct.unpack(FRAME, frame[:7])
            self.assertEqual((rt, rsid, rlen), (t, sid, len(payload)))
            self.assertEqual(frame[7:7 + rlen], payload)

    def test_max_payload_fits_mtu(self):
        # macOS caps the RFCOMM MTU at 667; header + payload must fit.
        CHUNK = 660
        self.assertLessEqual(struct.calcsize(FRAME) + CHUNK, 667)


class StreamReassembly(unittest.TestCase):
    """The receive side must handle frames split at arbitrary byte boundaries."""

    def drain(self, buf):
        frames = []
        while len(buf) >= 7:
            t, sid, ln = struct.unpack(FRAME, buf[:7])
            if len(buf) < 7 + ln:
                break
            frames.append((t, sid, buf[7:7 + ln]))
            buf = buf[7 + ln:]
        return frames, buf

    def test_split_frames_reassemble(self):
        stream = (
            struct.pack(FRAME, OPEN, 7, 0)
            + struct.pack(FRAME, DATA, 7, 3) + b'abc'
            + struct.pack(FRAME, CLOSE, 7, 0)
        )
        # Feed the byte stream one byte at a time.
        buf = b''
        collected = []
        for i in range(len(stream)):
            buf += stream[i:i + 1]
            frames, buf = self.drain(buf)
            collected.extend(frames)
        self.assertEqual(collected, [(OPEN, 7, b''), (DATA, 7, b'abc'), (CLOSE, 7, b'')])
        self.assertEqual(buf, b'')


class ProtocolV2(unittest.TestCase):
    """Golden vectors for the bidirectional extension.

    Every implementation must produce and accept exactly these bytes.
    """

    # --- backward compatibility -------------------------------------------
    def test_v1_open_bytes_are_unchanged(self):
        # An empty OPEN payload still means "the DeskThing server". This is the
        # compatibility guarantee: a v1 peer and a v2 peer agree on this frame.
        self.assertEqual(struct.pack(FRAME, OPEN, 1, 0), b'\x01\x00\x00\x00\x01\x00\x00')

    # --- stream-ID namespace ----------------------------------------------
    def test_namespace_split_by_high_bit(self):
        device_first, computer_first = 0x00000001, 0x80000001
        self.assertEqual(device_first & NS_MASK, 0)
        self.assertEqual(computer_first & NS_MASK, NS_MASK)
        self.assertEqual(struct.pack('>I', computer_first), b'\x80\x00\x00\x01')

    def test_namespaces_cannot_collide(self):
        # The two allocators can never mint the same ID, which is what makes
        # coordinating them unnecessary.
        device_ids = {(i & ~NS_MASK) for i in range(1, 500)}
        computer_ids = {(i | NS_MASK) for i in range(1, 500)}
        self.assertEqual(device_ids & computer_ids, set())

    def test_device_ids_stay_in_range_when_they_wrap(self):
        # Allocation must wrap inside its own half, never into the peer's.
        for raw in (0x7FFFFFFF, 0x80000000, 0xFFFFFFFF):
            self.assertEqual((raw & ~NS_MASK) & NS_MASK, 0)
            self.assertEqual((raw | NS_MASK) & NS_MASK, NS_MASK)

    # --- HELLO -------------------------------------------------------------
    def test_hello_device_golden(self):
        caps = CAP_INBOUND | CAP_TARGETED           # 0x0003
        payload = struct.pack('>BHQ', PROTOCOL_VERSION, caps, 0)  # device sends no clock
        self.assertEqual(len(payload), 11)
        self.assertEqual(
            struct.pack(FRAME, HELLO, 0, len(payload)) + payload,
            b'\x06\x00\x00\x00\x00\x00\x0b'
            b'\x02\x00\x03\x00\x00\x00\x00\x00\x00\x00\x00')

    def test_hello_computer_carries_a_clock(self):
        # The device has no RTC. The computer's epoch lets it fix its clock,
        # which every future TLS handshake from the device depends on.
        payload = struct.pack('>BHQ', PROTOCOL_VERSION, CAP_INBOUND | CAP_TARGETED, 1785943364)
        self.assertEqual(
            struct.pack(FRAME, HELLO, 0, len(payload)) + payload,
            b'\x06\x00\x00\x00\x00\x00\x0b'
            b'\x02\x00\x03\x00\x00\x00\x00\x6a\x73\x55\x44')

    def test_hello_roundtrip(self):
        payload = struct.pack('>BHQ', PROTOCOL_VERSION, CAP_INBOUND, 42)
        version, caps, epoch = struct.unpack('>BHQ', payload)
        self.assertEqual((version, caps, epoch), (2, CAP_INBOUND, 42))

    # --- OPEN target descriptors ------------------------------------------
    def test_open_named_service_golden(self):
        payload = bytes([KIND_SERVICE, 3]) + b'cdp'
        self.assertEqual(
            struct.pack(FRAME, OPEN, 0x80000001, len(payload)) + payload,
            b'\x01\x80\x00\x00\x01\x00\x05\x01\x03cdp')

    def test_open_hostport_golden(self):
        host = b'youtu.be'
        payload = bytes([KIND_HOSTPORT, len(host)]) + host + struct.pack('>H', 443)
        self.assertEqual(
            struct.pack(FRAME, OPEN, 2, len(payload)) + payload,
            b'\x01\x00\x00\x00\x02\x00\x0c\x02\x08youtu.be\x01\xbb')

    def test_biggest_descriptor_still_fits_one_frame(self):
        biggest = 1 + 1 + 255 + 2   # kind + hostLen + host + port
        self.assertEqual(biggest, 259)
        self.assertLessEqual(biggest, CHUNK_LIMIT)

    # --- OPEN_ACK ----------------------------------------------------------
    def test_open_ack_ok_golden(self):
        self.assertEqual(
            struct.pack(FRAME, OPEN_ACK, 0x80000001, 1) + bytes([ACK_OK]),
            b'\x07\x80\x00\x00\x01\x00\x01\x00')

    def test_open_ack_refused_golden(self):
        self.assertEqual(
            struct.pack(FRAME, OPEN_ACK, 2, 1) + bytes([ACK_REFUSED]),
            b'\x07\x00\x00\x00\x02\x00\x01\x01')

    def test_ack_codes_are_distinct(self):
        codes = [ACK_OK, ACK_REFUSED, ACK_UNREACHABLE,
                 ACK_UNKNOWN_SERVICE, ACK_BAD_NAMESPACE]
        self.assertEqual(len(set(codes)), len(codes))


CHUNK_LIMIT = 660


class DescriptorParsing(unittest.TestCase):
    """The OPEN descriptor parse is a security boundary — it is the thing that
    decides what a peer is allowed to reach. Every implementation must reject
    all of these rather than guess."""

    def parse(self, payload):
        """Reference parser. Returns ('service', name) or ('hostport', host, port)
        and raises ValueError on anything malformed."""
        if not payload:
            return ('legacy',)
        kind = payload[0]
        if kind == KIND_SERVICE:
            if len(payload) < 2:
                raise ValueError('truncated')
            n = payload[1]
            if n == 0 or len(payload) != 2 + n:
                raise ValueError('bad length')
            name = payload[2:2 + n].decode('ascii')
            if not all(c.islower() or c.isdigit() or c == '-' for c in name):
                raise ValueError('bad charset')
            return ('service', name)
        if kind == KIND_HOSTPORT:
            if len(payload) < 2:
                raise ValueError('truncated')
            n = payload[1]
            if n == 0 or len(payload) != 2 + n + 2:
                raise ValueError('bad length')
            host = payload[2:2 + n].decode('ascii')
            port = struct.unpack('>H', payload[2 + n:])[0]
            if port == 0:
                raise ValueError('bad port')
            return ('hostport', host, port)
        raise ValueError('unknown kind')

    def test_accepts_the_golden_forms(self):
        self.assertEqual(self.parse(b''), ('legacy',))
        self.assertEqual(self.parse(bytes([KIND_SERVICE, 3]) + b'cdp'), ('service', 'cdp'))
        self.assertEqual(
            self.parse(bytes([KIND_HOSTPORT, 8]) + b'youtu.be' + struct.pack('>H', 443)),
            ('hostport', 'youtu.be', 443))

    def test_rejects_malformed(self):
        bad = [
            bytes([KIND_SERVICE]),                              # kind byte only
            bytes([KIND_SERVICE, 5]) + b'ab',                    # nameLen overruns
            bytes([KIND_SERVICE, 0]),                            # empty name
            bytes([KIND_SERVICE, 3]) + b'CDP',                   # uppercase: registry is exact-match
            bytes([KIND_HOSTPORT, 8]) + b'youtu.be' + b'\x01',   # port truncated
            bytes([KIND_HOSTPORT, 0]) + struct.pack('>H', 443),  # empty host
            bytes([KIND_HOSTPORT, 4]) + b'host' + b'\x00\x00',   # port 0
            bytes([0x03, 0x00]),                                 # unknown kind
        ]
        for payload in bad:
            with self.assertRaises(ValueError, msg='accepted %r' % payload):
                self.parse(payload)

    def test_name_length_is_bounded_by_the_frame(self):
        # A 255-byte name is legal by the length byte and still fits a frame.
        name = b'a' * 255
        payload = bytes([KIND_SERVICE, 255]) + name
        self.assertEqual(self.parse(payload), ('service', name.decode()))
        self.assertLessEqual(len(payload), CHUNK_LIMIT)


if __name__ == '__main__':
    unittest.main()
