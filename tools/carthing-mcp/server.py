#!/usr/bin/env python3
"""MCP server exposing the Car Thing's screen and browser as agent tools.

The device has no `screencap`, so before the Bluetooth tunnel there was no way
for an agent to see what it was rendering. This wraps the debug CLI that ships
in the DeskThing repo (bt_source/tools/carthing-debug.py) so an agent can call
`carthing_screenshot` and actually *look* at the display, run JS against the
live page, and read its console — over Bluetooth, with no cable.

Dependency-free: speaks JSON-RPC over stdio directly, so it runs from a plain
checkout with nothing installed.
"""
import base64
import importlib.util
import json
import os
import sys
import threading
import time

PROTOCOL_VERSION = '2024-11-05'

# The engine lives in the DeskThing repo so it ships with the transport it
# depends on. Allow an override for checkouts in other locations.
DEFAULT_CLI = os.path.expanduser(
    '~/Spotify_Thing/DeskThing/DeskThingServer/bt_source/tools/carthing-debug.py')
CLI_PATH = os.environ.get('CARTHING_DEBUG_CLI', DEFAULT_CLI)


def load_engine():
    if not os.path.exists(CLI_PATH):
        return None
    spec = importlib.util.spec_from_file_location('carthing_debug', CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.argv = sys.argv, [CLI_PATH]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    return mod


ENGINE = load_engine()


class ToolError(Exception):
    pass


def _port(transport='auto'):
    if ENGINE is None:
        raise ToolError(
            'carthing-debug.py not found at %s. Set CARTHING_DEBUG_CLI to its '
            'path.' % CLI_PATH)
    if transport in ('auto', 'bluetooth'):
        port = ENGINE.bluetooth_port()
        if port:
            return port, 'bluetooth'
        if transport == 'bluetooth':
            raise ToolError('Bluetooth link is down, or the device still runs '
                            'a pre-v2 btmux.py that cannot accept inbound streams.')
    if transport in ('auto', 'usb'):
        port = ENGINE.usb_port()
        if port:
            return port, 'usb'
    raise ToolError('No transport available: Bluetooth link is down and no USB '
                    'device is attached. Check the device has power.')


def _page(port):
    cdp = ENGINE.CDP(port)
    target = cdp.page_target()
    cdp.connect(target['webSocketDebuggerUrl'])
    return cdp, target


# ---------------------------------------------------------------- the tools

def tool_status(_args):
    lines = []
    try:
        status = ENGINE._control('/status')
    except Exception:
        raise ToolError('The DeskThing Bluetooth helper is not answering on '
                        '127.0.0.1:8899 — is DeskThing running?')
    proto = status.get('protocol') or {}
    lines.append('link up      : %s' % status.get('linkUp'))
    lines.append('transport    : %s' % status.get('transport'))
    lines.append('paired       : %s' % status.get('paired'))
    lines.append('device        : %s' % status.get('deviceAddress'))
    lines.append('protocol      : v%s (inbound=%s)'
                 % (proto.get('version', '?'), proto.get('inbound')))
    services = [s.get('name') for s in status.get('services', [])]
    lines.append('services      : %s' % (', '.join(services) or 'none'))
    forwards = ['%s->%s' % (f['service'], f['port'])
                for f in status.get('forwards', [])]
    lines.append('forwards      : %s' % (', '.join(forwards) or 'none'))
    return [{'type': 'text', 'text': '\n'.join(lines)}]


def tool_screenshot(args):
    port, transport = _port(args.get('transport', 'auto'))
    cdp, target = _page(port)
    try:
        fmt = args.get('format', 'png')
        msg_id = cdp.send('Page.captureScreenshot', {'format': fmt})
        result = cdp.await_result(msg_id)
        data = result.get('data')
        if not data:
            raise ToolError('the device returned no image data')
        blob = base64.b64decode(data)
        out = []
        path = args.get('save_to')
        if path:
            with open(os.path.expanduser(path), 'wb') as f:
                f.write(blob)
            out.append({'type': 'text',
                        'text': 'Saved %d bytes to %s' % (len(blob), path)})
        out.append({'type': 'text',
                    'text': 'Car Thing screen over %s — page: %s'
                            % (transport, target.get('url', ''))})
        out.append({'type': 'image', 'data': data,
                    'mimeType': 'image/png' if fmt == 'png' else 'image/jpeg'})
        return out
    finally:
        cdp.close()


def tool_eval(args):
    expression = args.get('expression')
    if not expression:
        raise ToolError('expression is required')
    port, transport = _port(args.get('transport', 'auto'))
    cdp, _ = _page(port)
    try:
        msg_id = cdp.send('Runtime.evaluate', {
            'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        result = cdp.await_result(msg_id)
        # A thrown exception arrives as a *successful* CDP response carrying
        # exceptionDetails. Reporting only the (undefined) value would say
        # "null" and hide the error — the tool would lie about what happened.
        if 'exceptionDetails' in result:
            raise ToolError('JavaScript exception: %s'
                            % ENGINE.describe_exception(result['exceptionDetails']))
        value = result.get('result', {})
        if value.get('type') == 'undefined':
            text = 'undefined'
        else:
            text = json.dumps(value.get('value'), indent=2, default=str)
        return [{'type': 'text', 'text': text}]
    finally:
        cdp.close()


def tool_console(args):
    seconds = min(float(args.get('seconds', 5)), 60)
    port, _ = _port(args.get('transport', 'auto'))
    cdp, _ = _page(port)
    lines = []
    try:
        cdp.send('Runtime.enable')
        cdp.send('Log.enable')
        deadline = time.time() + seconds
        cdp.sock.settimeout(1.0)
        while time.time() < deadline:
            try:
                msg = cdp.recv()
            except Exception:
                continue
            if not msg:
                continue
            method = msg.get('method')
            if method == 'Runtime.consoleAPICalled':
                p = msg['params']
                parts = [str(a.get('value', a.get('description', '')))
                         for a in p.get('args', [])]
                lines.append('[%s] %s' % (p.get('type', 'log'), ' '.join(parts)))
            elif method == 'Log.entryAdded':
                e = msg['params']['entry']
                lines.append('[%s] %s' % (e.get('level'), e.get('text')))
            elif method == 'Runtime.exceptionThrown':
                d = msg['params']['exceptionDetails']
                lines.append('[exception] %s' % d.get('text'))
    finally:
        cdp.close()
    return [{'type': 'text',
             'text': '\n'.join(lines) if lines
                     else '(no console output in %gs)' % seconds}]


def tool_navigate(args):
    url = args.get('url')
    if not url:
        raise ToolError('url is required')
    port, _ = _port(args.get('transport', 'auto'))
    cdp, _ = _page(port)
    try:
        cdp.await_result(cdp.send('Page.navigate', {'url': url}))
        return [{'type': 'text', 'text': 'Navigated the device to %s' % url}]
    finally:
        cdp.close()


def tool_reload(args):
    port, _ = _port(args.get('transport', 'auto'))
    cdp, _ = _page(port)
    try:
        cdp.await_result(cdp.send('Page.reload',
                                  {'ignoreCache': bool(args.get('hard'))}))
        return [{'type': 'text', 'text': 'Reloaded the device page'}]
    finally:
        cdp.close()


TRANSPORT_PROP = {
    'type': 'string',
    'enum': ['auto', 'bluetooth', 'usb'],
    'description': 'Force a transport. Default auto: Bluetooth if the link is '
                   'up, else USB.'
}

TOOLS = [
    {
        'name': 'carthing_status',
        'description': 'Bluetooth link state for the Spotify Car Thing: whether '
                       'the tunnel is up, which transport is carrying data, the '
                       'negotiated protocol version, and any forwarded device '
                       'services. Start here when device tooling misbehaves.',
        'inputSchema': {'type': 'object', 'properties': {}},
        'handler': tool_status,
    },
    {
        'name': 'carthing_screenshot',
        'description': "Capture the Car Thing's screen and return it as an image "
                       'you can look at. This is the only way to see the display: '
                       'the firmware has no screencap. Use it to check UI work on '
                       'the device.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'save_to': {'type': 'string',
                            'description': 'Optional path to also write the file to.'},
                'format': {'type': 'string', 'enum': ['png', 'jpeg'],
                           'description': 'Image format (default png).'},
                'transport': TRANSPORT_PROP,
            },
        },
        'handler': tool_screenshot,
    },
    {
        'name': 'carthing_eval',
        'description': 'Run JavaScript in the live page on the Car Thing and return '
                       'the result as JSON. Use it to inspect app state, probe '
                       'browser capabilities, or drive the UI. Awaits promises.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'expression': {'type': 'string',
                               'description': 'JavaScript to evaluate in the page.'},
                'transport': TRANSPORT_PROP,
            },
            'required': ['expression'],
        },
        'handler': tool_eval,
    },
    {
        'name': 'carthing_console',
        'description': "Collect the device page's console output and uncaught "
                       'exceptions for a few seconds. Use it to debug client code '
                       'running on the device.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'seconds': {'type': 'number',
                            'description': 'How long to listen (default 5, max 60).'},
                'transport': TRANSPORT_PROP,
            },
        },
        'handler': tool_console,
    },
    {
        'name': 'carthing_navigate',
        'description': 'Point the Car Thing page at a URL. The client normally '
                       'loads from file://, so navigating away replaces the '
                       'DeskThing UI until the page is reloaded.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'url': {'type': 'string'},
                'transport': TRANSPORT_PROP,
            },
            'required': ['url'],
        },
        'handler': tool_navigate,
    },
    {
        'name': 'carthing_reload',
        'description': 'Reload the page on the Car Thing — the quickest way to '
                       'pick up a freshly deployed client build.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'hard': {'type': 'boolean',
                         'description': 'Bypass the cache.'},
                'transport': TRANSPORT_PROP,
            },
        },
        'handler': tool_reload,
    },
]

BY_NAME = {t['name']: t for t in TOOLS}


# ------------------------------------------------------------ JSON-RPC plumbing

def respond(msg_id, result=None, error=None):
    out = {'jsonrpc': '2.0', 'id': msg_id}
    if error is not None:
        out['error'] = error
    else:
        out['result'] = result
    sys.stdout.write(json.dumps(out) + '\n')
    sys.stdout.flush()


def handle(msg):
    method = msg.get('method')
    msg_id = msg.get('id')

    if method == 'initialize':
        respond(msg_id, {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'carthing', 'version': '1.0.0'},
        })
    elif method == 'notifications/initialized':
        pass  # notification: no reply
    elif method == 'tools/list':
        respond(msg_id, {'tools': [
            {k: t[k] for k in ('name', 'description', 'inputSchema')} for t in TOOLS
        ]})
    elif method == 'tools/call':
        params = msg.get('params', {})
        name = params.get('name')
        tool = BY_NAME.get(name)
        if tool is None:
            respond(msg_id, error={'code': -32602, 'message': 'unknown tool: %s' % name})
            return
        try:
            content = tool['handler'](params.get('arguments') or {})
            respond(msg_id, {'content': content})
        except ToolError as e:
            respond(msg_id, {'content': [{'type': 'text', 'text': str(e)}],
                             'isError': True})
        except Exception as e:  # never take the server down over one bad call
            respond(msg_id, {'content': [{'type': 'text',
                                          'text': '%s: %s' % (type(e).__name__, e)}],
                             'isError': True})
    elif msg_id is not None:
        respond(msg_id, error={'code': -32601, 'message': 'method not found: %s' % method})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as e:
            if msg.get('id') is not None:
                respond(msg['id'], error={'code': -32603, 'message': str(e)})


if __name__ == '__main__':
    main()
