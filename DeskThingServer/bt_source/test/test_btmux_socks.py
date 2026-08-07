"""Tests for the device's SOCKS5 on-ramp (internet sharing).

Chromium points at 127.0.0.1:1080 on the device; each CONNECT becomes a
tunneled host:port stream that the computer dials out. These drive the real
handler against fake streams, so the framing and the failure replies are
checked without hardware.
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
KIND_HOSTPORT = 0x02
ACK_OK, ACK_REFUSED, ACK_UNREACHABLE = 0, 1, 2


def load_mux():
    spec = importlib.util.spec_from_file_location('btmux', MUX)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.argv = sys.argv, [MUX]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    return mod


class FakeLoop:
    def __init__(self):
        self.sent = b''

    async def sock_sendall(self, sock, data):
        self.sent += data


class FakeReader:
    """Feeds scripted client bytes to the handler."""

    def __init__(self, data=b''):
        self.data = data

    async def readexactly(self, n):
        if len(self.data) < n:
            raise asyncio.IncompleteReadError(self.data, n)
        out, self.data = self.data[:n], self.data[n:]
        return out

    async def read(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


class FakeWriter:
    def __init__(self):
        self.buf = b''
        self.closed = False

    def write(self, data):
        self.buf += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def frames_in(blob):
    out = []
    while len(blob) >= 7:
        t, sid, ln = struct.unpack(FRAME, blob[:7])
        if len(blob) < 7 + ln:
            break
        out.append((t, sid, blob[7:7 + ln]))
        blob = blob[7 + ln:]
    return out


def socks_connect(host, port):
    """Greeting + CONNECT for a domain name."""
    return (b'\x05\x01\x00'
            + b'\x05\x01\x00\x03' + bytes([len(host)]) + host + struct.pack('>H', port))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class SocksFraming(unittest.TestCase):
    def setUp(self):
        self.mod = load_mux()
        self.loop = FakeLoop()
        self.mux = self.mod.Mux(sock=None, loop=self.loop)

    def drive(self, client_bytes, ack=ACK_OK, extra=b''):
        """Run the handler, answering its OPEN with the given ack code."""
        r, w = FakeReader(client_bytes + extra), FakeWriter()

        async def scenario():
            task = asyncio.ensure_future(self.mux.handle_socks(r, w))
            for _ in range(100):                     # let it reach the ack wait
                await asyncio.sleep(0.005)
                if self.mux.pending_ack:
                    break
            for sid, waiter in list(self.mux.pending_ack.items()):
                if not waiter.done():
                    waiter.set_result(ack)
            await asyncio.wait_for(task, timeout=5)

        run(scenario())
        return w

    def test_connect_becomes_a_hostport_open(self):
        self.drive(socks_connect(b'example.com', 443))
        opens = [(sid, p) for t, sid, p in frames_in(self.loop.sent) if t == OPEN]
        self.assertEqual(len(opens), 1)
        sid, payload = opens[0]
        self.assertEqual(payload[0], KIND_HOSTPORT)
        self.assertEqual(payload[1], len(b'example.com'))
        self.assertEqual(payload[2:2 + 11], b'example.com')
        self.assertEqual(struct.unpack('>H', payload[-2:])[0], 443)

    def test_open_uses_the_device_namespace(self):
        self.drive(socks_connect(b'example.com', 443))
        sid = [s for t, s, _ in frames_in(self.loop.sent) if t == OPEN][0]
        self.assertEqual(sid & 0x80000000, 0,
                         'device-opened streams must stay in the low half')

    def test_success_is_reported_to_the_client(self):
        w = self.drive(socks_connect(b'example.com', 443), ack=ACK_OK)
        self.assertEqual(w.buf[:2], b'\x05\x00')          # method selection
        self.assertEqual(w.buf[2:4], b'\x05\x00')          # CONNECT succeeded

    def test_refusal_is_reported_to_the_client(self):
        w = self.drive(socks_connect(b'blocked.example', 443), ack=ACK_REFUSED)
        self.assertEqual(w.buf[3], 2, 'expected SOCKS "connection not allowed"')
        self.assertTrue(w.closed)

    def test_unreachable_is_reported_to_the_client(self):
        w = self.drive(socks_connect(b'nope.example', 443), ack=ACK_UNREACHABLE)
        self.assertEqual(w.buf[3], 4, 'expected SOCKS "host unreachable"')

    def test_payload_flows_only_after_a_successful_ack(self):
        w = self.drive(socks_connect(b'example.com', 443), ack=ACK_OK,
                       extra=b'GET / HTTP/1.1\r\n\r\n')
        datas = [p for t, _, p in frames_in(self.loop.sent) if t == DATA]
        self.assertTrue(any(b'GET /' in d for d in datas))

    def test_refused_stream_sends_no_payload(self):
        self.drive(socks_connect(b'example.com', 443), ack=ACK_REFUSED,
                   extra=b'SECRET')
        datas = [p for t, _, p in frames_in(self.loop.sent) if t == DATA]
        self.assertEqual(datas, [], 'payload leaked on a refused stream')

    def test_non_connect_command_is_rejected(self):
        # BIND (0x02) must be refused — we are not a general SOCKS server.
        req = b'\x05\x01\x00' + b'\x05\x02\x00\x01' + b'\x01\x02\x03\x04' + struct.pack('>H', 80)
        r, w = FakeReader(req), FakeWriter()
        run(asyncio.wait_for(self.mux.handle_socks(r, w), timeout=5))
        self.assertEqual(w.buf[3], 7, 'expected SOCKS "command not supported"')
        self.assertEqual([f for f in frames_in(self.loop.sent) if f[0] == OPEN], [])

    def test_wrong_version_is_dropped(self):
        r, w = FakeReader(b'\x04\x01\x00'), FakeWriter()
        run(asyncio.wait_for(self.mux.handle_socks(r, w), timeout=5))
        self.assertTrue(w.closed)
        self.assertEqual(self.loop.sent, b'')

    def test_stream_is_closed_out_when_the_client_hangs_up(self):
        self.drive(socks_connect(b'example.com', 443), ack=ACK_OK)
        closes = [s for t, s, _ in frames_in(self.loop.sent) if t == CLOSE]
        self.assertEqual(len(closes), 1)
        self.assertEqual(self.mux.streams, {})

    def test_ack_waiter_is_always_cleaned_up(self):
        self.drive(socks_connect(b'example.com', 443), ack=ACK_REFUSED)
        self.assertEqual(self.mux.pending_ack, {})


class SocksConfig(unittest.TestCase):
    def setUp(self):
        self.mod = load_mux()

    def test_socks_port_matches_the_documented_chromium_flag(self):
        self.assertEqual(self.mod.SOCKS_PORT, 1080)

    def test_socks_port_is_not_the_tunnel_port(self):
        self.assertNotEqual(self.mod.SOCKS_PORT, self.mod.LISTEN_PORT)


if __name__ == '__main__':
    unittest.main()
