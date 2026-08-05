# Bluetooth transport

Lets a Spotify Car Thing reach the DeskThing server over Bluetooth instead of a
USB data cable, so after one-time setup the device only needs power. macOS
removed Bluetooth PAN, so there is no IP-over-Bluetooth to lean on; instead a
small TCP multiplexer runs over a single RFCOMM serial channel.

```
Car Thing                                   Computer
─────────                                   ────────
DeskThing client ──TCP 127.0.0.1:8891──► btmux.py ══RFCOMM ch 3══► btbridge ──TCP 127.0.0.1:8891──► DeskThing server
                                                                       │
                                                                       └── control API 127.0.0.1:8899 (DeskThing UI)
```

The client already talks to `localhost:8891`; when the Bluetooth link is up the
mux simply becomes that endpoint, so the client needs no changes.

## Pieces

| Path | Runs on | Role |
| --- | --- | --- |
| `btbridge.swift` | Computer (macOS) | Opens RFCOMM to the device, demuxes streams onto the local server port, serves the control API. Compiled by `scripts/build-btbridge.js` into `mac/btbridge` (gitignored) during the build; packaged into `Contents/Resources/mac/`. |
| `superbird/btmux.py` | Car Thing | Owns `127.0.0.1:8891` on the device, muxes client TCP streams into frames over the RFCOMM channel. Installed by the in-app provisioner as a supervisord service. |

## Frame protocol

One RFCOMM channel carries many TCP streams. Each frame is a fixed header plus
payload (struct layout `>BIH`):

| Field | Size | Meaning |
| --- | --- | --- |
| type | 1 byte | 1 = OPEN, 2 = DATA, 3 = CLOSE |
| streamID | 4 bytes BE | Stream identifier; streams originate device-side only |
| len | 2 bytes BE | Payload length |

macOS caps the RFCOMM MTU at 667 bytes (L2CAP default 672 − 5); the measured
usable throughput is ~155 KB/s with the radio saturated. Optimize payloads,
not the protocol.

## Control API (127.0.0.1:8899)

- `GET /status` → `{"preference":"bluetooth","transport":"bluetooth","linkUp":true}`
- `POST /preference` with `{"preference":"usb"|"bluetooth"}` → updated status

The main process (`src/main/services/bluetooth/`) consumes this API and exposes
it to the renderer over typed IPC; nothing else should call it directly.

## Transport priority

While the RFCOMM link is up, the helper removes the `adb reverse tcp:8891`
forward so Bluetooth carries the traffic; when the link drops (or the user pins
USB) the helper restores it. The preference persists in
`~/Library/Application Support/deskthing/bt-transport.json`.

## Adding a platform

The transport is macOS-only today, but the seam is small:

1. Implement the `BluetoothTransportManager` interface in
   `src/main/services/bluetooth/index.ts` for the new platform (start/stop the
   helper, report status, provision devices).
2. Ship a helper binary under `bt_source/<platform>/` that speaks the frame
   protocol above and serves the same control API, and teach
   `scripts/build-btbridge.js` and the packaging config to build and bundle it.

Platforms without a helper report `supported: false` and the UI hides itself —
plain USB setups look no different than before.
