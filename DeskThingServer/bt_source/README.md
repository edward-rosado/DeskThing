# Bluetooth transport

Lets a Spotify Car Thing reach the DeskThing server over Bluetooth instead of a
USB data cable, so after one-time setup the device only needs power. There is
no IP-over-Bluetooth to lean on (macOS removed PAN entirely); instead a small
TCP multiplexer runs over a single RFCOMM serial channel.

```
Car Thing                                   Computer
─────────                                   ────────
DeskThing client ──TCP 127.0.0.1:8891──► btmux.py ══RFCOMM ch 3══► btbridge ──TCP 127.0.0.1:8891──► DeskThing server
      │                                     ▲                          │
      │ pairing PIN overlay                 │                          └── control API 127.0.0.1:8899
      └──── GET 127.0.0.1:8892/pairing ── btagent.py                       (consumed by the server, surfaced over IPC)
```

The client already talks to `localhost:8891`; when the Bluetooth link is up the
mux simply becomes that endpoint, so the client needs no changes to be carried
over Bluetooth. (The client's pairing overlay and transport badge are additive.)

## Pieces

| Path | Runs on | Role |
| --- | --- | --- |
| `btbridge.swift` | Computer (macOS) | RFCOMM client + tunnel + pairing via IOBluetooth. Compiled by `build-btbridge.js` into `mac/btbridge` (gitignored). |
| `linux/btbridge` | Computer (Linux) | Same contract in Python over BlueZ (`AF_BLUETOOTH` sockets + `bluetoothctl`). Shipped as-is; no compile step. |
| `win/btbridge.c` | Computer (Windows) | Same contract in C over Winsock RFCOMM + the Win32 Bluetooth authentication API. Compiled by `build-btbridge.js` when a C compiler is present; otherwise the app quietly stays USB-only. |
| `superbird/btmux.py` | Car Thing | Owns `127.0.0.1:8891` on the device, muxes client TCP streams into frames over the RFCOMM channel. |
| `superbird/btagent.py` | Car Thing | Persistent BlueZ pairing agent: answers computer-initiated pairing, exposes the 6-digit code on `127.0.0.1:8892/pairing` for the client to draw on screen. |

Both device services are installed by the in-app provisioner (Setup Device →
Bluetooth) as supervisord services, so they restart on boot and on crash.

## Pairing flow (the original Car Thing flow)

1. The computer initiates from the DeskThing setup page (`POST /pair`).
2. The device's screen shows a 6-digit code (btagent → client overlay).
3. The same code surfaces in DeskThing (`/status` → `pairing.code`); the person
   confirms it there (`POST /pair/reply`). The device side auto-accepts —
   the human confirmation happens computer-side.
4. On success the helper stores the device address and connects automatically
   from then on: power the device anywhere in range and it comes back.

Stale half-bonds (one side paired, the other not) make hosts abort right after
encryption with no visible error, so every helper removes the existing bond
before pairing fresh, and `POST /unpair` exposes that for the UI.

## Frame protocol

One RFCOMM channel carries many TCP streams. Each frame is a fixed header plus
payload (struct layout `>BIH`):

| Field | Size | Meaning |
| --- | --- | --- |
| type | 1 byte | see the table below |
| streamID | 4 bytes BE | Stream identifier; high bit marks who opened it |
| len | 2 bytes BE | Payload length |

| Type | Name | Payload |
| --- | --- | --- |
| 1 | OPEN | target descriptor (empty = the DeskThing server) |
| 2 | DATA | stream bytes |
| 3 | CLOSE | — |
| 4 | PING | — |
| 5 | PONG | — |
| 6 | HELLO | version(1) caps(2 BE) epoch(8 BE) |
| 7 | OPEN_ACK | status(1) |

An unknown type must be **skipped**, never treated as a desync — its bytes are
already framed, and discarding the buffer would corrupt every other live
stream. That is what lets a v1 and a v2 peer interoperate.

### Direction and stream IDs

v1 was one-directional: only the device opened streams, always to the DeskThing
server. v2 lets either side open. The 32-bit ID space is split by its high bit
so both ends allocate without coordinating:

- `0x00000001 … 0x7FFFFFFF` — opened by the **device**
- `0x80000001 … 0xFFFFFFFF` — opened by the **computer**

Allocation wraps inside its own half. An OPEN whose ID is in the wrong half is
refused with `ACK_BAD_NAMESPACE`; a collision would silently cross-wire two live
TCP streams.

### HELLO and capabilities

Both sides send HELLO when a session opens. Capability bits: `0x01` INBOUND
(accepts computer-originated streams), `0x02` TARGETED (understands OPEN
descriptors), `0x04` INET (will proxy `host:port` — reserved for internet
sharing). A peer that never sends HELLO is assumed to be v1, and the link
behaves exactly as it did before.

The computer's HELLO carries its clock. The Car Thing has no RTC and nothing
else sets its time; a wrong clock fails every TLS handshake the device ever
makes, in ways that look like a tunnel bug.

### OPEN target descriptors

| Payload | Meaning |
| --- | --- |
| empty | the DeskThing server (identical to v1) |
| `0x01 len name` | a **named service** on the peer |
| `0x02 len host port(2 BE)` | a literal `host:port` (reserved for internet sharing) |

Named services are the only form accepted by default, in either direction. The
device is not a router: a name that is not in its registry is refused, so
`127.0.0.1:5037` is *inexpressible* rather than merely filtered. The descriptor
parse is a trust boundary — every implementation rejects malformed input rather
than guessing, and `test/test_protocol.py` locks the rejection cases.

### Device service registry

What the **computer** may open on the device (default deny):

| Name | Target | Why |
| --- | --- | --- |
| `cdp` | 127.0.0.1:2222 | chromium remote debugging — the only way to screenshot a device with no `screencap` |
| `pairing` | 127.0.0.1:8892 | pairing agent status |

Port **8891 is permanently absent** and must stay that way: it is the mux's own
listener, so an inbound stream there would be handed back to `handle_local` and
forwarded over the link again — an unbounded loop that eats the whole radio.

Golden vectors live in `test/test_protocol.py`; every implementation must
match them. macOS caps the RFCOMM MTU at 667 bytes (L2CAP default 672 − 5);
measured usable throughput is ~155 KB/s with the radio saturated. Optimize
payloads, not the protocol.

## Control API (127.0.0.1:8899)

| Endpoint | Effect |
| --- | --- |
| `GET /status` | `{preference, transport, linkUp, deviceAddress, paired, pairing:{stage,code,error}, found:[{address,name}]}` |
| `POST /preference {"preference"}` | Pin traffic to `bluetooth` or `usb` |
| `POST /discover` | Inquiry for nearby devices; results in later `/status` polls |
| `POST /pair {"address"}` | Computer-initiated pairing (numeric comparison) |
| `POST /pair/reply {"accept"}` | Answer the numeric-comparison prompt |
| `POST /unpair {"address"}` | Remove a bond |
| `POST /device {"address"}` | Set the device the bridge connects to |
| `POST /forward/open {"service"}` | Expose a device service as a loopback port; returns `{ok, port}` |
| `POST /forward/close {"service"}` | Tear that forward down |

The main process (`src/main/services/bluetooth/`) consumes this API and exposes
it to the renderer over typed IPC; nothing else should call it directly.

## Transport priority

While the RFCOMM link is up, the helper removes the `adb reverse tcp:8891`
forward so Bluetooth carries the traffic; when the link drops (or the user pins
USB) the helper restores it. The preference and device address persist in the
platform's app-data dir (`bt-transport.json`).

## Tests

- `npm run test:bt` — the TypeScript layer (IPC dispatch, manager, control-API
  client, provisioner) plus the Python protocol golden vectors and the pairing
  agent's parsing.
- The frame protocol tests double as the cross-implementation contract: change
  them only when changing every helper.

## Adding a platform

1. Ship a helper under `bt_source/<platform>/` that speaks the frame protocol
   and control API above (see the Linux helper for the smallest example).
2. Teach `build-btbridge.js` to build it (if it needs building) and add the
   `extraFiles` packaging entry in package.json.

Platforms whose helper is missing report `supported: false` and the UI hides
itself — plain USB setups look no different than before.
