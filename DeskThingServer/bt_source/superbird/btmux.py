#!/usr/bin/env python3
"""DeskThing Bluetooth mux (device side).

Listens on 127.0.0.1:<port> and tunnels every TCP connection over a single
RFCOMM channel to the Mac-side bridge, which reconnects each stream to the
DeskThing server. Frame: >BIH = type, stream id, len.
Types: 1=OPEN 2=DATA 3=CLOSE 4=PING 5=PONG.

The RFCOMM link is a real AF_BLUETOOTH socket bound to channel 3 — no
`rfcomm` binary and no /dev/rfcomm0 tty. The old tty approach leaked a
channel-3 binding after every session, so the next connect was refused
(-536870212) until the binding was cleared by hand; a socket has nothing to
leak and simply accepts the next connection.

PING/PONG is a liveness heartbeat: after a reboot a peer can hold a half-open
link that reads as connected but passes no data. A peer that stops ponging is
dead, so we drop the link and both ends reconnect cleanly.
"""
import asyncio, os, socket, struct, subprocess, sys, time

LISTEN_ADDR = '127.0.0.1'
LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8891
RFCOMM_CHANNEL = 3
CHUNK = 660  # frame + 7-byte header stays within the 667-byte RFCOMM MTU

# --- protocol v2 -----------------------------------------------------------
PROTOCOL_VERSION = 2
NS_MASK = 0x80000000        # set = the computer opened it, clear = we did
CAP_INBOUND = 1 << 0
CAP_TARGETED = 1 << 1
OUR_CAPS = CAP_INBOUND | CAP_TARGETED

KIND_SERVICE = 0x01
KIND_HOSTPORT = 0x02

ACK_OK, ACK_REFUSED, ACK_UNREACHABLE, ACK_UNKNOWN_SERVICE, ACK_BAD_NAMESPACE = range(5)

# What the COMPUTER is allowed to reach on this device. Default deny: a name
# that is not in this table is refused, so adding a service is a deliberate act
# and a hostile peer cannot address arbitrary local ports.
#
# 8891 is deliberately absent and must stay that way: it is our own listener,
# so an inbound stream to it would be handed to handle_local and forwarded
# straight back over the link — an unbounded loop that eats the whole radio.
SERVICES = {
    'cdp': ('127.0.0.1', 2222),      # chromium remote debugging
    'pairing': ('127.0.0.1', 8892),  # the pairing agent's status endpoint
}


def parse_target(payload):
    """Decode an OPEN target descriptor.

    Returns ('legacy',), ('service', name) or ('hostport', host, port).
    Raises ValueError on anything malformed — this is a trust boundary, so it
    rejects rather than guesses.
    """
    if not payload:
        return ('legacy',)
    kind = payload[0]
    if kind == KIND_SERVICE:
        if len(payload) < 2:
            raise ValueError('truncated')
        n = payload[1]
        if n == 0 or len(payload) != 2 + n:
            raise ValueError('bad length')
        name = payload[2:2 + n].decode('ascii', 'replace')
        if not all(c.islower() or c.isdigit() or c == '-' for c in name):
            raise ValueError('bad charset')
        return ('service', name)
    if kind == KIND_HOSTPORT:
        if len(payload) < 2:
            raise ValueError('truncated')
        n = payload[1]
        if n == 0 or len(payload) != 2 + n + 2:
            raise ValueError('bad length')
        host = payload[2:2 + n].decode('ascii', 'replace')
        port = struct.unpack('>H', payload[2 + n:])[0]
        if port == 0:
            raise ValueError('bad port')
        return ('hostport', host, port)
    raise ValueError('unknown kind %d' % kind)


class Mux:
    def __init__(self, sock, loop):
        self.sock = sock
        self.loop = loop
        # Streams WE opened (the DeskThing client reaching the computer). The
        # watchdog counts these, so inbound streams must not live here.
        self.streams = {}
        # Streams the COMPUTER opened into this device.
        self.inbound = {}
        # Inbound streams still connecting. Opening a local service is async,
        # and the peer sends its first payload straight after OPEN — without
        # somewhere to park those bytes they are lost and the far end waits
        # forever for a reply to a request we dropped.
        self.opening = {}
        self.next_id = 1
        self.wlock = asyncio.Lock()
        self.last_pong = time.time()
        self.peer_version = 1        # assume v1 until a HELLO says otherwise
        self.peer_caps = 0

    async def send(self, t, sid, payload=b''):
        async with self.wlock:
            await self.loop.sock_sendall(
                self.sock, struct.pack('>BIH', t, sid, len(payload)) + payload)

    async def send_hello(self):
        # The device has no real-time clock, so it advertises epoch 0 and takes
        # the computer's word for the time.
        await self.send(6, 0, struct.pack('>BHQ', PROTOCOL_VERSION, OUR_CAPS, 0))

    def apply_hello(self, payload):
        if len(payload) < 11:
            return
        version, caps, epoch = struct.unpack('>BHQ', payload[:11])
        self.peer_version, self.peer_caps = version, caps
        print('mux: peer speaks v%d caps=0x%04x' % (version, caps), flush=True)
        # Fix our clock from the computer's. Nothing else on this device sets
        # the time, and a wrong clock fails every TLS handshake later in ways
        # that look like a tunnel bug.
        if epoch and abs(time.time() - epoch) > 60:
            subprocess.call(['date', '-s', '@%d' % epoch],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('mux: clock set from peer (%d)' % epoch, flush=True)

    async def open_inbound(self, sid, payload):
        """The computer wants to reach a service on this device."""
        # IDs the computer mints must carry its namespace bit; anything else is
        # a broken or hostile peer and could collide with our own streams.
        if not (sid & NS_MASK):
            await self.send(7, sid, bytes([ACK_BAD_NAMESPACE]))
            return
        try:
            target = parse_target(payload)
        except ValueError as e:
            print('mux: rejecting inbound open: %s' % e, flush=True)
            await self.send(7, sid, bytes([ACK_UNKNOWN_SERVICE]))
            return

        if target[0] != 'service':
            # The device is not a router: it only ever exposes named services.
            await self.send(7, sid, bytes([ACK_REFUSED]))
            return
        if target[1] not in SERVICES:
            await self.send(7, sid, bytes([ACK_UNKNOWN_SERVICE]))
            return

        host, port = SERVICES[target[1]]
        # Start buffering immediately: DATA for this stream can arrive while
        # the connect below is still in flight.
        self.opening[sid] = []
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5)
        except Exception:
            print('mux: inbound %s unreachable' % target[1], flush=True)
            self.opening.pop(sid, None)
            await self.send(7, sid, bytes([ACK_UNREACHABLE]))
            return

        # The peer may have closed while we were connecting.
        if sid not in self.opening:
            try:
                w.close()
            except Exception:
                pass
            return

        early = self.opening.pop(sid)
        self.inbound[sid] = w
        await self.send(7, sid, bytes([ACK_OK]))
        print('mux: inbound stream %d -> %s' % (sid, target[1]), flush=True)
        for chunk in early:
            w.write(chunk)
        if early:
            try:
                await w.drain()
            except Exception:
                pass
        asyncio.ensure_future(self.pump_inbound(sid, r, w))

    async def pump_inbound(self, sid, r, w):
        """Relay a computer-opened stream from the device service back out."""
        try:
            while True:
                data = await r.read(CHUNK)
                if not data:
                    break
                await self.send(2, sid, data)
        except Exception:
            pass
        finally:
            if self.inbound.pop(sid, None) is not None:
                try:
                    await self.send(3, sid)
                except Exception:
                    pass
            try:
                w.close()
            except Exception:
                pass

    def writer_for(self, sid):
        """Either direction — the namespaces cannot collide, so one lookup
        covering both maps is unambiguous."""
        return self.streams.get(sid) or self.inbound.get(sid)

    def drop(self, sid):
        self.opening.pop(sid, None)
        w = self.streams.pop(sid, None) or self.inbound.pop(sid, None)
        if w is not None:
            try:
                w.close()
            except Exception:
                pass

    async def handle_local(self, r, w):
        # Read the first bytes before opening a tunnel stream so that a probe for
        # /__bt can be answered here, locally. The client uses that to tell whether
        # this port is served by the Bluetooth link or by USB adb-reverse: over USB
        # the request reaches the DeskThing server instead, which 404s.
        timed_out = False
        try:
            first = await asyncio.wait_for(r.read(CHUNK), timeout=5)
        except asyncio.TimeoutError:
            first, timed_out = b'', True
        except Exception:
            first = b''

        if not first and not timed_out:
            try:
                w.close()
            except Exception:
                pass
            return

        if first.startswith(b'GET /__bt'):
            body = b'{"transport":"bluetooth"}'
            w.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                    b'Access-Control-Allow-Origin: *\r\nCache-Control: no-store\r\n'
                    b'Content-Length: ' + str(len(body)).encode() +
                    b'\r\nConnection: close\r\n\r\n' + body)
            try:
                await w.drain()
            except Exception:
                pass
            try:
                w.close()
            except Exception:
                pass
            return

        sid = self.next_id
        self.next_id += 1
        self.streams[sid] = w
        await self.send(1, sid)
        if first:
            await self.send(2, sid, first)
        try:
            while True:
                data = await r.read(CHUNK)
                if not data:
                    break
                await self.send(2, sid, data)
        except Exception:
            pass
        finally:
            if sid in self.streams:
                del self.streams[sid]
                try:
                    await self.send(3, sid)
                except Exception:
                    pass
            try:
                w.close()
            except Exception:
                pass

    async def pump_rfcomm(self):
        buf = b''
        while True:
            data = await self.loop.sock_recv(self.sock, 4096)
            if not data:
                raise ConnectionError('rfcomm closed by peer')
            buf += data
            while len(buf) >= 7:
                t, sid, ln = struct.unpack('>BIH', buf[:7])
                if len(buf) < 7 + ln:
                    break
                payload = buf[7:7 + ln]
                buf = buf[7 + ln:]
                if t == 4:  # PING -> answer so the Mac knows we are alive
                    await self.send(5, 0)
                    continue
                if t == 5:  # PONG from the Mac
                    self.last_pong = time.time()
                    continue
                if t == 6:  # HELLO — capability + clock exchange
                    self.apply_hello(payload)
                    continue
                if t == 1:  # the computer is opening a stream into us
                    asyncio.ensure_future(self.open_inbound(sid, payload))
                    continue
                if t == 7:  # OPEN_ACK for a stream we opened
                    if payload and payload[0] != ACK_OK:
                        print('mux: peer refused stream %d (code %d)'
                              % (sid, payload[0]), flush=True)
                        self.drop(sid)
                    continue
                if sid in self.opening:
                    # Still connecting: park DATA, honor an early CLOSE.
                    if t == 2:
                        self.opening[sid].append(payload)
                    elif t == 3:
                        self.opening.pop(sid, None)
                    continue
                w = self.writer_for(sid)
                if t == 2 and w is not None:
                    w.write(payload)
                    try:
                        await w.drain()
                    except Exception:
                        pass
                elif t == 3 and w is not None:
                    self.drop(sid)
                # Any other type is from a newer peer: skip the frame, never
                # touch the buffer. Its bytes are already consumed above, so
                # the streams that follow stay intact.

    async def heartbeat(self):
        """Ping the Mac; if it stops ponging the link is dead — raise to end
        the session so we go back to accepting a fresh connection."""
        self.last_pong = time.time()
        while True:
            await asyncio.sleep(5)
            try:
                await self.send(4, 0)
            except Exception:
                raise ConnectionError('rfcomm write failed')
            if time.time() - self.last_pong > 15:
                raise ConnectionError('no pong from Mac in 15s')


async def session(conn):
    conn.setblocking(False)
    loop = asyncio.get_event_loop()
    mux = Mux(conn, loop)

    # Port 8891 may be held by adb reverse while USB is attached. Bind in a
    # concurrent task with retries so the rfcomm pump still notices a dead link
    # while we wait for the port to free up.
    state = {'server': None}

    async def binder():
        while state['server'] is None:
            try:
                state['server'] = await asyncio.start_server(
                    mux.handle_local, LISTEN_ADDR, LISTEN_PORT)
                print('mux: tunnel up, listening on %s:%d' % (LISTEN_ADDR, LISTEN_PORT), flush=True)
            except OSError:
                print('mux: port %d busy (USB active?), retrying in 10s' % LISTEN_PORT, flush=True)
                await asyncio.sleep(10)

    async def client_watchdog():
        # The on-device client (chromium) starts at boot, ~80s before the
        # Bluetooth link is up. Its websocket retries can wedge against a
        # server that was unreachable at boot and then not recover even once
        # the tunnel is healthy. If the tunnel has been up a while with no
        # client stream, the client is stuck — restart it once so it dials a
        # working tunnel fresh. A healthy client opens a stream in seconds, so
        # this only fires when something is actually wrong.
        while state['server'] is None:
            await asyncio.sleep(1)
        await asyncio.sleep(25)
        if not mux.streams:
            print('mux: no client stream 25s after tunnel up — restarting chromium', flush=True)
            subprocess.call(['supervisorctl', 'restart', 'chromium'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    bind_task = asyncio.ensure_future(binder())
    wd_task = asyncio.ensure_future(client_watchdog())
    hb_task = asyncio.ensure_future(mux.heartbeat())
    pump_task = asyncio.ensure_future(mux.pump_rfcomm())
    # Announce ourselves. A v1 computer ignores the unknown frame type and the
    # link keeps working exactly as before.
    try:
        await mux.send_hello()
    except Exception:
        pass
    try:
        done, _pending = await asyncio.wait(
            [pump_task, hb_task], return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
    except (OSError, ConnectionError) as e:
        print('mux: rfcomm link closed (%s)' % e, flush=True)
    finally:
        for task in (hb_task, pump_task, bind_task, wd_task):
            task.cancel()
        if state['server'] is not None:
            state['server'].close()
        for w in list(mux.streams.values()) + list(mux.inbound.values()):
            try:
                w.close()
            except Exception:
                pass
        mux.streams.clear()
        mux.inbound.clear()
        try:
            conn.close()
        except Exception:
            pass


def setup_radio():
    # One-time radio setup: power, connectable + discoverable, SPP record.
    # All idempotent; bluetoothd resets these at boot.
    for cmd in (['bluetoothctl', 'power', 'on'],
                ['hciconfig', 'hci0', 'piscan']):
        subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Advertise SPP on channel 3 so a scanning Mac can find the port. Only add
    # if missing — this service restarts and sdptool would register duplicates.
    try:
        have = subprocess.check_output(['sdptool', 'browse', 'local'],
                                       stderr=subprocess.DEVNULL).decode('utf-8', 'replace')
    except Exception:
        have = ''
    if 'Serial Port' not in have:
        subprocess.call(['sdptool', 'add', '--channel=%d' % RFCOMM_CHANNEL, 'SP'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    setup_radio()
    srv = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('00:00:00:00:00:00', RFCOMM_CHANNEL))
    srv.listen(1)
    print('mux: listening on RFCOMM channel %d' % RFCOMM_CHANNEL, flush=True)
    while True:
        print('mux: waiting for Mac to connect rfcomm...', flush=True)
        try:
            conn, addr = srv.accept()
        except OSError as e:
            print('mux: accept failed (%r), retrying' % e, flush=True)
            time.sleep(1)
            continue
        print('mux: rfcomm connected from %s' % (addr,), flush=True)
        try:
            asyncio.run(session(conn))
        except Exception as e:
            print('mux: session error: %r' % e, flush=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == '__main__':
    main()
