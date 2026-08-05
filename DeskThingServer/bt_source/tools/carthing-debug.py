#!/usr/bin/env python3
"""Drive the Car Thing's browser from your computer — over Bluetooth, no cable.

The device runs chromium with a remote debugging port, but nothing on the
device can show you what is on screen: the firmware has no `screencap`. This
reaches that debugger through the Bluetooth tunnel's `cdp` service (or over USB
if that is what is attached) and exposes the useful parts.

    ./carthing-debug.py info                  what is running, what page is loaded
    ./carthing-debug.py shot screen.png       screenshot the display
    ./carthing-debug.py eval "location.href"  run JS in the page
    ./carthing-debug.py console               stream console output until Ctrl-C
    ./carthing-debug.py navigate <url>        point the page somewhere
    ./carthing-debug.py reload                reload the page

Transport is picked automatically: the Bluetooth forward if the link is up,
otherwise `adb forward` if the device is on USB. Use --transport to force one.

Requires only the standard library.
"""
import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

CONTROL_URL = 'http://127.0.0.1:8899'
DEVICE_CDP_PORT = 2222
ADB_CANDIDATES = [
    '/Applications/DeskThing.app/Contents/Resources/mac/adb',
    'adb',
]


# --------------------------------------------------------------- transports

def _control(path, payload=None, timeout=8):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        CONTROL_URL + path, data=data,
        headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def bluetooth_port():
    """Ask the bridge helper to expose the device's debugger locally.
    Returns a port, or None if Bluetooth isn't usable right now."""
    try:
        status = _control('/status')
    except Exception:
        return None
    if not status.get('linkUp'):
        return None
    if not status.get('protocol', {}).get('inbound'):
        # An older device that predates protocol v2 cannot accept inbound
        # streams; it needs its btmux.py updated.
        return None
    for fwd in status.get('forwards', []):
        if fwd.get('service') == 'cdp':
            return fwd['port']
    try:
        res = _control('/forward/open', {'service': 'cdp'})
    except Exception:
        return None
    return res.get('port') if res.get('ok') else None


def find_adb():
    for candidate in ADB_CANDIDATES:
        if os.path.sep in candidate:
            if os.access(candidate, os.X_OK):
                return candidate
        else:
            from shutil import which
            found = which(candidate)
            if found:
                return found
    return None


def usb_port():
    """Fall back to a plain adb port forward."""
    adb = find_adb()
    if not adb:
        return None
    try:
        devices = subprocess.run([adb, 'devices'], capture_output=True,
                                 timeout=15).stdout.decode()
    except Exception:
        return None
    if '\tdevice' not in devices:
        return None
    for port in range(9222, 9260):
        try:
            rc = subprocess.run(
                [adb, 'forward', 'tcp:%d' % port, 'tcp:%d' % DEVICE_CDP_PORT],
                capture_output=True, timeout=15).returncode
            if rc == 0:
                return port
        except Exception:
            return None
    return None


def resolve_port(preference='auto'):
    if preference in ('auto', 'bluetooth'):
        port = bluetooth_port()
        if port:
            return port, 'bluetooth'
        if preference == 'bluetooth':
            sys.exit('Bluetooth is not available (link down, or the device '
                     'still runs a pre-v2 btmux.py).')
    if preference in ('auto', 'usb'):
        port = usb_port()
        if port:
            return port, 'usb'
    sys.exit('No transport: Bluetooth link is down and no USB device is attached.')


# ---------------------------------------------------------------- CDP client

class CDP:
    """A very small Chrome DevTools Protocol client.

    Deliberately dependency-free: this has to run from a checkout with nothing
    installed. Speaks just enough WebSocket to issue commands and read events.
    """

    def __init__(self, port, timeout=90):
        self.port = port
        self.timeout = timeout
        self.sock = None
        self._next_id = 0

    def targets(self):
        with urllib.request.urlopen(
                'http://127.0.0.1:%d/json' % self.port, timeout=self.timeout) as r:
            return json.load(r)

    def version(self):
        with urllib.request.urlopen(
                'http://127.0.0.1:%d/json/version' % self.port, timeout=self.timeout) as r:
            return json.load(r)

    def page_target(self):
        pages = [t for t in self.targets() if t.get('type') == 'page']
        if not pages:
            sys.exit('No page target on the device — is chromium running?')
        return pages[0]

    def connect(self, ws_url):
        path = ws_url.split(str(self.port), 1)[1]
        self.sock = socket.create_connection(('127.0.0.1', self.port),
                                             timeout=self.timeout)
        key = base64.b64encode(b'0123456789abcdef').decode()
        self.sock.sendall(
            ('GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n'
             'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
             'Sec-WebSocket-Version: 13\r\n\r\n' % (path, self.port, key)).encode())
        self.sock.recv(4096)   # handshake response

    def send(self, method, params=None):
        self._next_id += 1
        payload = json.dumps(
            {'id': self._next_id, 'method': method, 'params': params or {}}).encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126, n >> 8, n & 0xff])
        else:
            header += bytes([0x80 | 127]) + n.to_bytes(8, 'big')
        header += b'\x00\x00\x00\x00'   # masking key (zero: we mask with XOR 0)
        self.sock.sendall(bytes(header) + payload)
        return self._next_id

    def recv(self):
        head = self._read(2)
        if not head:
            return None
        length = head[1] & 0x7f
        if length == 126:
            length = int.from_bytes(self._read(2), 'big')
        elif length == 127:
            length = int.from_bytes(self._read(8), 'big')
        body = self._read(length)
        if body is None:
            return None
        try:
            return json.loads(body.decode('utf-8', 'replace'))
        except ValueError:
            return None

    def _read(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.sock.recv(min(65536, n - len(buf)))
            if not chunk:
                return None
            buf += chunk
        return buf

    def await_result(self, msg_id, deadline=None):
        deadline = deadline or (time.time() + self.timeout)
        while time.time() < deadline:
            msg = self.recv()
            if msg is None:
                continue
            if msg.get('id') == msg_id:
                if 'error' in msg:
                    sys.exit('device returned an error: %s' % json.dumps(msg['error']))
                return msg.get('result', {})
        sys.exit('timed out waiting for the device to answer')

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def open_page(port):
    cdp = CDP(port)
    target = cdp.page_target()
    cdp.connect(target['webSocketDebuggerUrl'])
    return cdp, target


# ------------------------------------------------------------------ commands

def cmd_info(args, port, transport):
    cdp = CDP(port)
    version = cdp.version()
    target = cdp.page_target()
    print('transport : %s (127.0.0.1:%d)' % (transport, port))
    print('browser   : %s' % version.get('Browser'))
    print('page      : %s' % target.get('url'))
    print('title     : %s' % target.get('title'))
    others = [t for t in cdp.targets() if t.get('type') != 'page']
    if others:
        print('other targets: %s' % ', '.join(sorted({t['type'] for t in others})))


def cmd_shot(args, port, transport):
    cdp, _ = open_page(port)
    try:
        msg_id = cdp.send('Page.captureScreenshot', {'format': args.format})
        result = cdp.await_result(msg_id)
        blob = base64.b64decode(result['data'])
        with open(args.output, 'wb') as f:
            f.write(blob)
        print('wrote %s (%d bytes) over %s' % (args.output, len(blob), transport))
    finally:
        cdp.close()


def cmd_eval(args, port, transport):
    cdp, _ = open_page(port)
    try:
        msg_id = cdp.send('Runtime.evaluate', {
            'expression': args.expression,
            'returnByValue': True,
            'awaitPromise': True,
        })
        result = cdp.await_result(msg_id)
        value = result.get('result', {})
        if value.get('type') == 'undefined':
            print('undefined')
        else:
            print(json.dumps(value.get('value'), indent=2, default=str))
    finally:
        cdp.close()


def cmd_navigate(args, port, transport):
    cdp, _ = open_page(port)
    try:
        cdp.await_result(cdp.send('Page.navigate', {'url': args.url}))
        print('navigated to %s' % args.url)
    finally:
        cdp.close()


def cmd_reload(args, port, transport):
    cdp, _ = open_page(port)
    try:
        cdp.await_result(cdp.send('Page.reload', {'ignoreCache': args.hard}))
        print('reloaded%s' % (' (cache bypassed)' if args.hard else ''))
    finally:
        cdp.close()


def cmd_console(args, port, transport):
    cdp, _ = open_page(port)
    try:
        cdp.send('Runtime.enable')
        cdp.send('Log.enable')
        print('streaming console from the device (Ctrl-C to stop)...')
        while True:
            msg = cdp.recv()
            if msg is None:
                continue
            method = msg.get('method')
            if method == 'Runtime.consoleAPICalled':
                params = msg['params']
                parts = []
                for arg in params.get('args', []):
                    parts.append(str(arg.get('value', arg.get('description', ''))))
                print('[%s] %s' % (params.get('type', 'log'), ' '.join(parts)))
            elif method == 'Log.entryAdded':
                entry = msg['params']['entry']
                print('[%s] %s' % (entry.get('level'), entry.get('text')))
            elif method == 'Runtime.exceptionThrown':
                details = msg['params']['exceptionDetails']
                print('[exception] %s' % details.get('text'))
    except KeyboardInterrupt:
        print('\nstopped.')
    finally:
        cdp.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--transport', choices=['auto', 'bluetooth', 'usb'],
                        default='auto', help='force a transport (default: auto)')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('info', help='what is running and what page is loaded')

    p = sub.add_parser('shot', help='screenshot the device display')
    p.add_argument('output', nargs='?', default='carthing.png')
    p.add_argument('--format', choices=['png', 'jpeg'], default='png')

    p = sub.add_parser('eval', help='run JavaScript in the page')
    p.add_argument('expression')

    p = sub.add_parser('navigate', help='point the page at a URL')
    p.add_argument('url')

    p = sub.add_parser('reload', help='reload the page')
    p.add_argument('--hard', action='store_true', help='bypass the cache')

    sub.add_parser('console', help='stream console output until Ctrl-C')

    args = parser.parse_args()
    port, transport = resolve_port(args.transport)
    handler = {
        'info': cmd_info, 'shot': cmd_shot, 'eval': cmd_eval,
        'navigate': cmd_navigate, 'reload': cmd_reload, 'console': cmd_console,
    }[args.command]
    handler(args, port, transport)


if __name__ == '__main__':
    main()
