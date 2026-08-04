import Foundation
import IOBluetooth
import Network

// DeskThing Bluetooth bridge (Mac side).
// Connects to the Car Thing's RFCOMM channel 3 and demuxes tunneled TCP
// streams onto localhost:8891 (the DeskThing server).
// Frame: type(1) streamID(4 BE) len(2 BE) payload. 1=OPEN 2=DATA 3=CLOSE.
//
// Also serves a small control API on 127.0.0.1:8899 so the DeskThing UI can
// show which transport is live and let the user choose which one to prefer.

let deviceAddr = "30-e3-d6-05-78-45"
let rfcommChannelID: UInt8 = 3
let targetHost = NWEndpoint.Host("127.0.0.1")
let targetPort = NWEndpoint.Port(rawValue: 8891)!
let controlPort = NWEndpoint.Port(rawValue: 8899)!

let adbPath = "/Applications/DeskThing.app/Contents/Resources/mac/adb"
let deviceSerial = "8550R283Q910"

let prefURL = FileManager.default
  .homeDirectoryForCurrentUser
  .appendingPathComponent("Library/Application Support/deskthing/bt-transport.json")

func log(_ s: String) {
  let ts = ISO8601DateFormatter().string(from: Date())
  print("[\(ts)] \(s)")
  fflush(stdout)
}

@discardableResult
func adb(_ args: [String]) -> Int32 {
  guard FileManager.default.isExecutableFile(atPath: adbPath) else { return -1 }
  let p = Process()
  p.executableURL = URL(fileURLWithPath: adbPath)
  p.arguments = ["-s", deviceSerial] + args
  p.standardOutput = FileHandle.nullDevice
  p.standardError = FileHandle.nullDevice
  do { try p.run() } catch { return -1 }
  p.waitUntilExit()
  return p.terminationStatus
}

// MARK: - Shared state

/// Which transport the user wants. "bluetooth" (default) keeps the wireless link
/// and tears down the USB reverse while it is up; "usb" pins traffic to the cable.
enum Preference: String {
  case bluetooth
  case usb
}

final class State {
  static let shared = State()
  private let q = DispatchQueue(label: "bridge.prefs")
  private var _preference: Preference = .bluetooth
  private var _linkUp = false

  var preference: Preference {
    get { q.sync { _preference } }
    set { q.sync { _preference = newValue } }
  }
  var linkUp: Bool {
    get { q.sync { _linkUp } }
    set { q.sync { _linkUp = newValue } }
  }

  /// The transport actually carrying data right now. Falling back to USB only
  /// counts if the cable is really there, otherwise nothing is connected.
  var activeTransport: String {
    if linkUp { return "bluetooth" }
    return adb(["get-state"]) == 0 ? "usb" : "none"
  }

  func load() {
    guard let data = try? Data(contentsOf: prefURL),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let raw = obj["preference"] as? String,
          let p = Preference(rawValue: raw)
    else { return }
    preference = p
    log("preference loaded: \(p.rawValue)")
  }

  func save() {
    let obj: [String: Any] = ["preference": preference.rawValue]
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted])
    else { return }
    try? FileManager.default.createDirectory(
      at: prefURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    try? data.write(to: prefURL)
  }
}

// MARK: - Transport priority

/// The device-side mux owns 127.0.0.1:8891 whenever it can, but `adb reverse`
/// binds the same port over USB — so whoever bound first used to win, arbitrarily.
/// Make it explicit instead: while Bluetooth is up, tear the USB reverse down so
/// the mux owns the port; when Bluetooth drops, restore it so the client keeps
/// working over the cable.
func preferBluetooth() {
  let rc = adb(["reverse", "--remove", "tcp:8891"])
  log("transport: bluetooth active (USB reverse removed, rc=\(rc))")
}

func fallBackToUSB() {
  let rc = adb(["reverse", "tcp:8891", "tcp:8891"])
  log(rc == 0
      ? "transport: USB active (adb reverse restored)"
      : "transport: USB unavailable (rc=\(rc)) — device likely unplugged")
}

// MARK: - Control API (consumed by the DeskThing UI)

final class ControlServer {
  private var listener: NWListener?
  private let q = DispatchQueue(label: "bridge.control")

  func start() {
    let params = NWParameters.tcp
    params.allowLocalEndpointReuse = true
    params.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: controlPort)
    guard let l = try? NWListener(using: params) else {
      log("control: could not bind 127.0.0.1:\(controlPort)")
      return
    }
    listener = l
    l.newConnectionHandler = { [weak self] conn in self?.handle(conn) }
    l.start(queue: q)
    log("control: listening on 127.0.0.1:\(controlPort)")
  }

  private func handle(_ conn: NWConnection) {
    conn.start(queue: q)
    conn.receive(minimumIncompleteLength: 1, maximumLength: 16384) { data, _, _, _ in
      let req = String(data: data ?? Data(), encoding: .utf8) ?? ""
      let body = self.respond(to: req)
      let http = """
      HTTP/1.1 200 OK\r
      Content-Type: application/json\r
      Access-Control-Allow-Origin: *\r
      Access-Control-Allow-Methods: GET, POST, OPTIONS\r
      Access-Control-Allow-Headers: Content-Type\r
      Cache-Control: no-store\r
      Content-Length: \(body.utf8.count)\r
      Connection: close\r
      \r
      \(body)
      """
      conn.send(content: http.data(using: .utf8), completion: .contentProcessed { _ in
        conn.cancel()
      })
    }
  }

  private func respond(to request: String) -> String {
    let s = State.shared

    // A preference change arrives as POST /preference {"preference":"usb"}
    if request.hasPrefix("POST /preference") {
      if let range = request.range(of: "\r\n\r\n") {
        let json = String(request[range.upperBound...])
        if let d = json.data(using: .utf8),
           let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
           let raw = obj["preference"] as? String,
           let p = Preference(rawValue: raw) {
          s.preference = p
          s.save()
          log("preference set to \(p.rawValue) by UI")
          // Apply immediately rather than waiting for the next reconnect cycle.
          if p == .usb {
            fallBackToUSB()
          } else if s.linkUp {
            preferBluetooth()
          }
        }
      }
    }

    return """
    {"preference":"\(s.preference.rawValue)","transport":"\(s.activeTransport)","linkUp":\(s.linkUp)}
    """
  }
}

// MARK: - RFCOMM tunnel

final class Bridge: NSObject, IOBluetoothRFCOMMChannelDelegate {
  private var channel: IOBluetoothRFCOMMChannel?
  private var conns: [UInt32: NWConnection] = [:]
  private var rxBuf = Data()
  private let q = DispatchQueue(label: "bridge.state")
  // writeSync blocks while the RFCOMM link drains. It must never run on `q`,
  // or inbound frames can't be processed and both directions deadlock.
  private let writeQueue = DispatchQueue(label: "bridge.write")
  private var mtu: UInt16 = 990
  var closed = false

  func rfcommChannelOpenComplete(_ ch: IOBluetoothRFCOMMChannel, status error: IOReturn) {
    if error != kIOReturnSuccess {
      log("open failed: \(error)")
      q.sync { closed = true }
      return
    }
    channel = ch
    mtu = ch.getMTU()
    log("rfcomm open, mtu=\(mtu)")
    State.shared.linkUp = true
    preferBluetooth()
  }

  func rfcommChannelClosed(_ ch: IOBluetoothRFCOMMChannel) {
    log("rfcomm closed")
    State.shared.linkUp = false
    fallBackToUSB()
    q.sync {
      for (_, c) in conns { c.cancel() }
      conns.removeAll()
      closed = true
    }
  }

  func rfcommChannelData(_ ch: IOBluetoothRFCOMMChannel, data: UnsafeMutableRawPointer, length: Int) {
    let chunk = Data(bytes: data, count: length)
    q.async {
      self.rxBuf.append(chunk)
      self.drainFrames()
    }
  }

  // Runs on q.
  private func drainFrames() {
    while rxBuf.count >= 7 {
      let t = rxBuf[rxBuf.startIndex]
      let sid = rxBuf.subdata(in: rxBuf.startIndex+1..<rxBuf.startIndex+5).withUnsafeBytes { $0.load(as: UInt32.self).bigEndian }
      let ln = Int(rxBuf.subdata(in: rxBuf.startIndex+5..<rxBuf.startIndex+7).withUnsafeBytes { $0.load(as: UInt16.self).bigEndian })
      guard rxBuf.count >= 7 + ln else { return }
      let payload = rxBuf.subdata(in: rxBuf.startIndex+7..<rxBuf.startIndex+7+ln)
      rxBuf.removeFirst(7 + ln)
      switch t {
      case 1: openStream(sid)
      case 2: conns[sid]?.send(content: payload, completion: .contentProcessed { _ in })
      case 3:
        conns[sid]?.cancel()
        conns.removeValue(forKey: sid)
      default:
        log("bad frame type \(t), resetting buffer")
        rxBuf.removeAll()
      }
    }
  }

  // Runs on q.
  private func openStream(_ sid: UInt32) {
    let conn = NWConnection(host: targetHost, port: targetPort, using: .tcp)
    conns[sid] = conn
    conn.stateUpdateHandler = { [weak self] state in
      switch state {
      case .failed, .cancelled:
        self?.q.async {
          if self?.conns.removeValue(forKey: sid) != nil {
            self?.sendFrame(3, sid, Data())
          }
        }
      case .ready:
        self?.receiveLoop(sid, conn)
      default: break
      }
    }
    conn.start(queue: q)
  }

  private func receiveLoop(_ sid: UInt32, _ conn: NWConnection) {
    conn.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, isDone, _ in
      guard let self = self else { return }
      if isDone {
        self.q.async {
          if self.conns.removeValue(forKey: sid) != nil {
            self.sendFrame(3, sid, Data())
          }
        }
        return
      }
      guard let data = data, !data.isEmpty else {
        self.receiveLoop(sid, conn)
        return
      }
      // Push this batch onto the link, then resume reading only once it is
      // actually on the wire. That backpressure keeps a fast TCP source from
      // outrunning the much slower Bluetooth link.
      self.writeQueue.async {
        var off = 0
        while off < data.count {
          let n = min(data.count - off, Int(self.mtu) - 7)
          self.writeFrame(2, sid, data.subdata(in: off..<off+n))
          off += n
        }
        self.receiveLoop(sid, conn)
      }
    }
  }

  // Enqueue a frame for the link. Safe to call from any queue.
  private func sendFrame(_ t: UInt8, _ sid: UInt32, _ payload: Data) {
    writeQueue.async { self.writeFrame(t, sid, payload) }
  }

  // Must only run on writeQueue.
  private func writeFrame(_ t: UInt8, _ sid: UInt32, _ payload: Data) {
    guard let ch = channel else { return }
    var frame = Data([t])
    var sidBE = sid.bigEndian
    var lenBE = UInt16(payload.count).bigEndian
    withUnsafeBytes(of: &sidBE) { frame.append(contentsOf: $0) }
    withUnsafeBytes(of: &lenBE) { frame.append(contentsOf: $0) }
    frame.append(payload)
    frame.withUnsafeBytes { (p: UnsafeRawBufferPointer) in
      let m = UnsafeMutableRawPointer(mutating: p.baseAddress!)
      _ = ch.writeSync(m, length: UInt16(frame.count))
    }
  }

  func isClosed() -> Bool { q.sync { closed } }
}

// MARK: - Main

State.shared.load()

let control = ControlServer()
control.start()

// IOBluetooth's coordinator initializes lazily and waits on work scheduled to the
// main queue — including the Bluetooth permission check. Blocking the main thread
// while that happens deadlocks the process and suppresses the permission prompt,
// so the radio work runs on its own thread and main is left to service the queue.
Thread.detachNewThread {
  runBridgeLoop()
}

RunLoop.main.run()

func runBridgeLoop() -> Never {
  guard let device = IOBluetoothDevice(addressString: deviceAddr) else {
    log("bad device address"); exit(1)
  }
  log("bluetooth ready for \(device.addressString ?? deviceAddr)")

  while true {
  if State.shared.preference == .usb {
    // User pinned the cable. Keep the reverse in place and stay off the radio.
    if State.shared.linkUp { State.shared.linkUp = false }
    fallBackToUSB()
    Thread.sleep(forTimeInterval: 5)
    continue
  }

  let bridge = Bridge()
  var channel: IOBluetoothRFCOMMChannel?
  log("connecting to Car Thing rfcomm ch\(rfcommChannelID)...")
  let res = device.openRFCOMMChannelSync(&channel, withChannelID: rfcommChannelID, delegate: bridge)
  if res == kIOReturnSuccess {
    log("connected")
    while !bridge.isClosed() && State.shared.preference == .bluetooth {
      RunLoop.current.run(until: Date().addingTimeInterval(0.5))
    }
    log("session ended")
    channel?.close()
    device.closeConnection()
    State.shared.linkUp = false
  } else {
    log("connect failed (\(res)); device off/out of range? retrying in 10s")
    State.shared.linkUp = false
    // No Bluetooth link, so make sure the USB path is available if the cable is in.
    fallBackToUSB()
  }
    Thread.sleep(forTimeInterval: 10)
  }
}
