"""Functional tests for the device mux's inbound (computer-originated) path.

This is the security boundary introduced by protocol v2: before it, the device
could only ever be reached at one hardcoded port by one hardcoded peer. These
tests drive the real Mux against a fake RFCOMM socket.
"""
import asyncio
import importlib.util
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MUX = os.path.join(HERE, '..', 'superbird', 'btmux.py')

FRAME = '>BIH'
OPEN, DATA, CLOSE, PING, PONG, HELLO, OPEN_ACK = 1, 2, 3, 4, 5, 6, 7
NS = 0x80000000
KIND_SERVICE, KIND_HOSTPORT = 0x01, 0x02
ACK_OK, ACK_REFUSED, ACK_UNREACHABLE, ACK_UNKNOWN, ACK_BAD_NS = range(5)


def load_mux():
    # The mux reads its listen port from argv at import time, so hide the test
    # runner's arguments while loading it.
    spec = importlib.util.spec_from_file_location('btmux', MUX)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.argv = sys.argv, [MUX]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    return mod


class FakeLoop:
    """Captures what the mux writes to the radio."""

    def __init__(self):
        self.sent = b''

    async def sock_sendall(self, sock, data):
        self.sent += data


def frames_in(blob):
    out = []
    while len(blob) >= 7:
        t, sid, ln = struct.unpack(FRAME, blob[:7])
        if len(blob) < 7 + ln:
            break
        out.append((t, sid, blob[7:7 + ln]))
        blob = blob[7 + ln:]
    return out


def acks_in(blob):
    return [(sid, p[0]) for t, sid, p in frames_in(blob) if t == OPEN_ACK and p]


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TargetParsing(unittest.TestCase):
    def setUp(self):
        self.mod = load_mux()

    def test_accepts_golden_forms(self):
        p = self.mod.parse_target
        self.assertEqual(p(b''), ('legacy',))
        self.assertEqual(p(bytes([KIND_SERVICE, 3]) + b'cdp'), ('service', 'cdp'))
        self.assertEqual(
            p(bytes([KIND_HOSTPORT, 8]) + b'youtu.be' + struct.pack('>H', 443)),
            ('hostport', 'youtu.be', 443))

    def test_rejects_malformed(self):
        p = self.mod.parse_target
        for payload in [
            bytes([KIND_SERVICE]),
            bytes([KIND_SERVICE, 5]) + b'ab',
            bytes([KIND_SERVICE, 0]),
            bytes([KIND_SERVICE, 3]) + b'CDP',
            bytes([KIND_HOSTPORT, 8]) + b'youtu.be' + b'\x01',
            bytes([KIND_HOSTPORT, 0]) + struct.pack('>H', 443),
            bytes([KIND_HOSTPORT, 4]) + b'host' + b'\x00\x00',
            bytes([0x03, 0x00]),
        ]:
            with self.assertRaises(ValueError, msg='accepted %r' % payload):
                p(payload)


class InboundPolicy(unittest.TestCase):
    def setUp(self):
        self.mod = load_mux()
        self.loop = FakeLoop()
        self.mux = self.mod.Mux(sock=None, loop=self.loop)

    def open(self, sid, payload):
        run(self.mux.open_inbound(sid, payload))
        return acks_in(self.loop.sent)

    def test_rejects_id_from_the_wrong_namespace(self):
        # An ID without the computer's namespace bit could collide with one of
        # ours and silently cross-wire two live TCP streams.
        acks = self.open(1, bytes([KIND_SERVICE, 3]) + b'cdp')
        self.assertEqual(acks, [(1, ACK_BAD_NS)])
        self.assertEqual(self.mux.inbound, {})

    def test_rejects_unknown_service(self):
        acks = self.open(NS | 1, bytes([KIND_SERVICE, 5]) + b'admin')
        self.assertEqual(acks, [(NS | 1, ACK_UNKNOWN)])

    def test_rejects_malformed_descriptor(self):
        acks = self.open(NS | 1, bytes([KIND_SERVICE, 9]) + b'ab')
        self.assertEqual(acks, [(NS | 1, ACK_UNKNOWN)])

    def test_refuses_arbitrary_host_port(self):
        """The device is not a router — only named services, even for a peer
        that asks nicely for a host:port."""
        payload = bytes([KIND_HOSTPORT, 9]) + b'127.0.0.1' + struct.pack('>H', 8891)
        acks = self.open(NS | 1, payload)
        self.assertEqual(acks, [(NS | 1, ACK_REFUSED)])

    def test_legacy_descriptor_is_refused_inbound(self):
        # An empty payload means "the DeskThing server", which only makes sense
        # in the outbound direction.
        acks = self.open(NS | 1, b'')
        self.assertEqual(acks, [(NS | 1, ACK_REFUSED)])

    def test_own_listen_port_is_not_reachable(self):
        """8891 must never be a named service: an inbound stream to it would be
        handed to handle_local and forwarded straight back over the link."""
        self.assertNotIn(
            self.mod.LISTEN_PORT,
            [port for _host, port in self.mod.SERVICES.values()])

    def test_registry_is_loopback_only(self):
        for host, _port in self.mod.SERVICES.values():
            self.assertEqual(host, '127.0.0.1')

    def test_unreachable_service_is_reported(self):
        # 'pairing' points at the agent's port, which is not running in tests.
        acks = self.open(NS | 1, bytes([KIND_SERVICE, 7]) + b'pairing')
        self.assertEqual(acks, [(NS | 1, ACK_UNREACHABLE)])
        self.assertEqual(self.mux.inbound, {})


class InboundRelay(unittest.TestCase):
    """End-to-end: a real local service, opened over the fake radio."""

    def setUp(self):
        self.mod = load_mux()
        self.loop = FakeLoop()
        self.mux = self.mod.Mux(sock=None, loop=self.loop)

    def test_relays_bytes_from_a_real_service(self):
        async def scenario():
            async def handler(reader, writer):
                writer.write(b'hello-from-device')
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(handler, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]
            self.mod.SERVICES['test'] = ('127.0.0.1', port)
            try:
                await self.mux.open_inbound(NS | 7, bytes([KIND_SERVICE, 4]) + b'test')
                for _ in range(50):          # let the relay pump run
                    await asyncio.sleep(0.02)
                    if any(t == DATA for t, _, _ in frames_in(self.loop.sent)):
                        break
            finally:
                server.close()
                self.mod.SERVICES.pop('test', None)

        run(scenario())
        frames = frames_in(self.loop.sent)
        self.assertIn((NS | 7, ACK_OK), acks_in(self.loop.sent))
        data = b''.join(p for t, _, p in frames if t == DATA)
        self.assertEqual(data, b'hello-from-device')
        # and the stream is closed out when the service hangs up
        self.assertIn(CLOSE, [t for t, _, _ in frames])


class EarlyDataRace(unittest.TestCase):
    """Opening a local service is async, and a peer sends its first payload
    straight after OPEN. Those bytes must be parked, not dropped — losing them
    leaves the far end waiting forever for a reply to a request that was
    silently discarded. Found on hardware: chromium connected but never saw
    the HTTP request."""

    def setUp(self):
        self.mod = load_mux()
        self.loop = FakeLoop()
        self.mux = self.mod.Mux(sock=None, loop=self.loop)

    def test_data_arriving_mid_connect_is_delivered(self):
        got = []

        async def scenario():
            async def handler(reader, writer):
                got.append(await reader.read(64))
                writer.write(b'ok')
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(handler, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]
            self.mod.SERVICES['test'] = ('127.0.0.1', port)
            try:
                # Start the open, then push DATA before it can finish.
                task = asyncio.ensure_future(
                    self.mux.open_inbound(NS | 5, bytes([KIND_SERVICE, 4]) + b'test'))
                await asyncio.sleep(0)          # let it register as 'opening'
                self.assertIn(NS | 5, self.mux.opening,
                              'stream should be parked while connecting')
                self.mux.opening[NS | 5].append(b'GET /hello')
                await task
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if got:
                        break
            finally:
                server.close()
                self.mod.SERVICES.pop('test', None)

        run(scenario())
        self.assertEqual(got, [b'GET /hello'],
                         'bytes buffered during connect were not delivered')

    def test_close_during_connect_abandons_the_stream(self):
        self.mux.opening[NS | 9] = []
        self.mux.drop(NS | 9)
        self.assertNotIn(NS | 9, self.mux.opening)

    def test_unreachable_service_clears_the_buffer(self):
        acks = run(self.mux.open_inbound(NS | 3, bytes([KIND_SERVICE, 7]) + b'pairing'))
        self.assertEqual(self.mux.opening, {},
                         'a failed open must not leak its buffer')


class HelloExchange(unittest.TestCase):
    def setUp(self):
        self.mod = load_mux()
        self.loop = FakeLoop()
        self.mux = self.mod.Mux(sock=None, loop=self.loop)

    def test_sends_version_and_caps(self):
        run(self.mux.send_hello())
        frames = frames_in(self.loop.sent)
        self.assertEqual(len(frames), 1)
        t, sid, payload = frames[0]
        self.assertEqual((t, sid), (HELLO, 0))
        version, caps, epoch = struct.unpack('>BHQ', payload)
        self.assertEqual(version, self.mod.PROTOCOL_VERSION)
        self.assertTrue(caps & self.mod.CAP_INBOUND)
        self.assertTrue(caps & self.mod.CAP_TARGETED)
        self.assertEqual(epoch, 0, 'the device has no clock to advertise')

    def test_records_peer_capabilities(self):
        self.mux.apply_hello(struct.pack('>BHQ', 2, 0x0003, 0))
        self.assertEqual(self.mux.peer_version, 2)
        self.assertEqual(self.mux.peer_caps, 0x0003)

    def test_defaults_to_v1_before_any_hello(self):
        self.assertEqual(self.mux.peer_version, 1)
        self.assertEqual(self.mux.peer_caps, 0)

    def test_truncated_hello_is_ignored(self):
        self.mux.apply_hello(b'\x02\x00')
        self.assertEqual(self.mux.peer_version, 1)


class StreamBookkeeping(unittest.TestCase):
    def setUp(self):
        self.mod = load_mux()
        self.mux = self.mod.Mux(sock=None, loop=FakeLoop())

    def test_watchdog_counts_only_outbound_streams(self):
        """The chromium rescue keys off client streams. An inbound debugging
        session must not make the mux look healthy."""
        class W:
            def close(self):
                pass
        self.mux.inbound[NS | 1] = W()
        self.assertFalse(self.mux.streams,
                         'inbound stream leaked into the outbound map')

    def test_writer_lookup_spans_both_directions(self):
        class W:
            def close(self):
                pass
        out, inb = W(), W()
        self.mux.streams[1] = out
        self.mux.inbound[NS | 1] = inb
        self.assertIs(self.mux.writer_for(1), out)
        self.assertIs(self.mux.writer_for(NS | 1), inb)

    def test_drop_removes_from_either_map(self):
        class W:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
        out, inb = W(), W()
        self.mux.streams[1] = out
        self.mux.inbound[NS | 1] = inb
        self.mux.drop(1)
        self.mux.drop(NS | 1)
        self.assertTrue(out.closed and inb.closed)
        self.assertEqual((self.mux.streams, self.mux.inbound), ({}, {}))


if __name__ == '__main__':
    unittest.main()
