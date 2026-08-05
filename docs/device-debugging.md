# Debugging the Car Thing over Bluetooth

The Car Thing runs a chromium kiosk, but **its firmware has no `screencap`** —
before the Bluetooth tunnel there was no way to see what it was rendering
except by pointing a camera at it. Protocol v2 forwards the device's chromium
debugger to your computer, so you can screenshot the display, run JavaScript
against the live page, and read its console **with no cable attached**.

This is the fastest way to develop UI for the device.

---

## Quick start

```bash
cd DeskThingServer/bt_source/tools

./carthing-debug.py info                    # what's running, what page is loaded
./carthing-debug.py shot screen.png         # screenshot the display
./carthing-debug.py eval "location.href"    # run JS in the page
./carthing-debug.py console                 # stream console output
./carthing-debug.py reload                  # pick up a fresh client build
```

No dependencies — standard library only, so it runs from a plain checkout.

**Transport is automatic:** the Bluetooth forward if the link is up, otherwise
`adb forward` if the device is on USB. Force one with `--transport bluetooth`
or `--transport usb`.

### Prerequisites

- DeskThing running (the tool asks its bridge helper to open the forward).
- A paired device with the Bluetooth link up — check with `./carthing-debug.py info`.
- The device running a **protocol v2** `btmux.py`. An older device can't accept
  inbound streams; the tool will say so and fall back to USB.

---

## What each command is for

| Command | Use it when |
| --- | --- |
| `info` | First thing to run. Confirms the transport, browser version, and loaded page. |
| `shot [file]` | Checking UI work. `--format jpeg` for a smaller transfer. |
| `eval <js>` | Inspecting app state, probing browser capabilities, driving the UI. Awaits promises and returns JSON. |
| `console` | Debugging client-side code. Streams `console.*`, log entries, and uncaught exceptions until Ctrl-C. |
| `navigate <url>` | Pointing the device at something else. The client loads from `file://`, so this replaces the DeskThing UI until you reload. |
| `reload [--hard]` | After deploying a new client build. |

---

## Verified device facts

Measured on hardware over Bluetooth with `carthing-debug.py eval` — worth
knowing before designing anything that runs on the device:

| | |
| --- | --- |
| Browser | **Chrome/69.0.3497.128** (QtWebEngine 5.12.x) — a 2018 engine |
| Viewport | 800×480 |
| Video | **VP8 in WebM: yes.** VP9: **no**. H.264/MP4: **no** |
| Audio | Opus: yes. Vorbis: yes. MP3: yes. AAC: no |
| Page origin | `file:///usr/share/qt-superbird-app/webapp/index.html` |

Two consequences that catch people out:

- **Encode video as VP8 + Opus/Vorbis in WebM.** VP9 and H.264 will not play —
  the device has no proprietary-codec build, and its engine predates VP9
  support here. Verify with
  `./carthing-debug.py eval "MediaSource.isTypeSupported('video/webm; codecs=\"vp8,opus\"')"`.
- **Chrome 69 predates a lot.** The DeskThing client ships a legacy build
  targeting it for exactly this reason. Modern web apps generally will not run.

---

## Under the hood

`carthing-debug.py` asks the bridge helper's control API to expose the device's
debugger as a loopback port:

```
POST 127.0.0.1:8899/forward/open {"service":"cdp"}  →  {"ok":true,"port":60000}
```

That port is a plain TCP forward of the device's `127.0.0.1:2222`, so **ordinary
tools work unmodified** — point `chrome://inspect` at it and you get real
DevTools against the Car Thing over Bluetooth.

`cdp` is one of a small default-deny set of named services the device is willing
to expose (see [`DeskThingServer/bt_source/README.md`](../DeskThingServer/bt_source/README.md)). Arbitrary ports are deliberately not
reachable.

---

## Troubleshooting

**"No transport available"** — the Bluetooth link is down and nothing is on USB.
The device has no battery, so "not connecting" is usually "not powered"; use a
**USB-A** charger, since a C-to-C cable delivers none. Check with
`carthing_status` or `curl -s 127.0.0.1:8899/status`.

**"the device still runs a pre-v2 btmux.py"** — deploy the current one over USB:

```bash
adb push bt_source/superbird/btmux.py /etc/deskthing-bt/btmux.py
adb shell "supervisorctl restart btmux"
```

**A request times out once, then works** — the mux restarts chromium if no
client stream appears within 25s of the tunnel coming up (it rescues a client
wedged from booting before the link existed). That restart drops in-flight CDP
connections. Just retry.

**Screenshots are slow** — the link saturates at ~155 KB/s and a PNG of this
screen is 150–180 KB, so expect a second or two. `--format jpeg` is smaller.
