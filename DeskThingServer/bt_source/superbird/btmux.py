#!/usr/bin/env python3
"""DeskThing Bluetooth mux (device side).

Listens on 127.0.0.1:<port> and tunnels every TCP connection over the
/dev/rfcomm0 serial link to the Mac-side bridge, which reconnects each
stream to the DeskThing server. Frame: >BIH = type, stream id, len.
Types: 1=OPEN 2=DATA 3=CLOSE.
"""
import asyncio, os, struct, subprocess, sys, time

RFCOMM_DEV = '/dev/rfcomm0'
LISTEN_ADDR = '127.0.0.1'
LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8891
CHUNK = 660  # frame + 7-byte header stays within the 667-byte RFCOMM MTU


class Mux:
    def __init__(self, ser_w):
        self.ser_w = ser_w
        self.streams = {}
        self.next_id = 1
        self.wlock = asyncio.Lock()

    async def send(self, t, sid, payload=b''):
        async with self.wlock:
            self.ser_w.write(struct.pack('>BIH', t, sid, len(payload)) + payload)
            await self.ser_w.drain()

    async def handle_local(self, r, w):
        # Read the first bytes before opening a tunnel stream so that a probe for
        # /__bt can be answered here, locally. The client uses that to tell whether
        # this port is being served by the Bluetooth link or by USB adb-reverse:
        # over USB the request reaches the DeskThing server instead, which 404s.
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

    async def pump_serial(self, ser_r):
        while True:
            hdr = await ser_r.readexactly(7)
            t, sid, ln = struct.unpack('>BIH', hdr)
            payload = await ser_r.readexactly(ln) if ln else b''
            w = self.streams.get(sid)
            if t == 2 and w is not None:
                w.write(payload)
                try:
                    await w.drain()
                except Exception:
                    pass
            elif t == 3 and w is not None:
                del self.streams[sid]
                try:
                    w.close()
                except Exception:
                    pass


async def session():
    os.system('stty raw -echo < %s' % RFCOMM_DEV)
    fd_r = os.open(RFCOMM_DEV, os.O_RDONLY | os.O_NOCTTY)
    fd_w = os.open(RFCOMM_DEV, os.O_WRONLY | os.O_NOCTTY)
    loop = asyncio.get_event_loop()
    ser_r = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(ser_r), os.fdopen(fd_r, 'rb', 0))
    w_transport, w_protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, os.fdopen(fd_w, 'wb', 0))
    ser_w = asyncio.StreamWriter(w_transport, w_protocol, None, loop)

    mux = Mux(ser_w)
    # Port 8891 may be held by adb reverse while USB is attached. Bind in a
    # concurrent task with retries so the serial pump still notices a dead
    # link while we wait for the port to free up.
    state = {'server': None}

    async def binder():
        while state['server'] is None:
            try:
                state['server'] = await asyncio.start_server(mux.handle_local, LISTEN_ADDR, LISTEN_PORT)
                print('mux: tunnel up, listening on %s:%d' % (LISTEN_ADDR, LISTEN_PORT), flush=True)
            except OSError:
                print('mux: port %d busy (USB active?), retrying in 10s' % LISTEN_PORT, flush=True)
                await asyncio.sleep(10)

    bind_task = asyncio.ensure_future(binder())
    try:
        await mux.pump_serial(ser_r)
    except (asyncio.IncompleteReadError, OSError):
        print('mux: serial link closed', flush=True)
    finally:
        bind_task.cancel()
        if state['server'] is not None:
            state['server'].close()
        for w in list(mux.streams.values()):
            try:
                w.close()
            except Exception:
                pass
        mux.streams.clear()


def main():
    # One-time radio setup: power, connectable (page scan), SPP record.
    # All idempotent; bluetoothd resets these at boot.
    for cmd in (['bluetoothctl', 'power', 'on'],
                ['hciconfig', 'hci0', 'piscan']):
        subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Only advertise SPP if it isn't already there — this service restarts, and
    # sdptool would happily register a duplicate record every time.
    try:
        have = subprocess.check_output(['sdptool', 'browse', 'local'],
                                       stderr=subprocess.DEVNULL).decode('utf-8', 'replace')
    except Exception:
        have = ''
    if 'Serial Port' not in have:
        subprocess.call(['sdptool', 'add', '--channel=3', 'SP'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while True:
        # Clear any stale binding/listener left by a previous instance.
        subprocess.call(['rfcomm', 'release', '0'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(['sh', '-c', "for p in $(pidof rfcomm); do kill -9 $p; done"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        proc = subprocess.Popen(['rfcomm', '-r', 'listen', 'hci0', '3'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('mux: waiting for Mac to connect rfcomm...', flush=True)
        while not os.path.exists(RFCOMM_DEV):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if os.path.exists(RFCOMM_DEV):
            try:
                asyncio.run(session())
            except Exception as e:
                print('mux: session error: %r' % e, flush=True)
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            pass
        time.sleep(1)


if __name__ == '__main__':
    main()
