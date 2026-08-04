import path from 'path'
import { existsSync } from 'node:fs'
import { ChildProcess, spawn } from 'child_process'
import getPlatform from '@server/utils/get-platform'
import Logger from '@server/utils/logger'
import { LOGGING_LEVELS } from '@deskthing/types'

/**
 * Supervises the Bluetooth bridge helper.
 *
 * The Car Thing can reach the server over a Bluetooth RFCOMM tunnel instead of a
 * USB cable, which is preferable since the device only needs power. The tunnel's
 * Mac side is a small helper binary shipped inside the app next to the bundled
 * adb, and it is owned by the app: started with the server and stopped when the
 * server quits, so no separate installation or background agent is involved.
 *
 * Only macOS ships a helper today. On other platforms this is a no-op and the
 * device keeps using USB, exactly as before.
 */

const isDevelopment = process.env.NODE_ENV === 'development'

const resourceRoot = isDevelopment
  ? path.join(__dirname, '..', '..', '..', '..', 'bt_source')
  : process.resourcesPath

const bridgePath = path.join(resourceRoot, getPlatform(), 'btbridge')

/** Where the device-side mux lives, for provisioning a Car Thing over adb. */
export const deviceMuxScriptPath = path.join(resourceRoot, 'superbird', 'btmux.py')

const RESTART_DELAY_MS = 5000

let child: ChildProcess | undefined
let stopping = false
let restartTimer: NodeJS.Timeout | undefined

const isSupported = (): boolean => process.platform === 'darwin'

const launch = (): void => {
  if (stopping) return

  if (!existsSync(bridgePath)) {
    Logger.log(
      LOGGING_LEVELS.WARN,
      `[btBridge] helper not found at ${bridgePath} — Bluetooth transport unavailable`
    )
    return
  }

  child = spawn(bridgePath, [], { stdio: ['ignore', 'pipe', 'pipe'] })

  child.stdout?.on('data', (chunk: Buffer) => {
    chunk
      .toString()
      .split('\n')
      .filter(Boolean)
      .forEach((line) => Logger.log(LOGGING_LEVELS.DEBUG, `[btBridge] ${line}`))
  })

  child.stderr?.on('data', (chunk: Buffer) => {
    Logger.log(LOGGING_LEVELS.WARN, `[btBridge] ${chunk.toString().trim()}`)
  })

  child.on('error', (error) => {
    Logger.log(LOGGING_LEVELS.ERROR, `[btBridge] failed to start: ${error.message}`)
  })

  child.on('exit', (code, signal) => {
    child = undefined
    if (stopping) return
    Logger.log(
      LOGGING_LEVELS.WARN,
      `[btBridge] exited (code=${code} signal=${signal}); restarting in ${RESTART_DELAY_MS}ms`
    )
    restartTimer = setTimeout(launch, RESTART_DELAY_MS)
  })

  Logger.log(LOGGING_LEVELS.LOG, `[btBridge] started (pid ${child.pid})`)
}

export const startBluetoothBridge = (): void => {
  if (!isSupported()) {
    Logger.log(LOGGING_LEVELS.DEBUG, '[btBridge] no helper for this platform; skipping')
    return
  }
  if (child) return
  stopping = false
  launch()
}

export const stopBluetoothBridge = (): void => {
  stopping = true
  if (restartTimer) {
    clearTimeout(restartTimer)
    restartTimer = undefined
  }
  if (child) {
    Logger.log(LOGGING_LEVELS.LOG, '[btBridge] stopping')
    child.kill()
    child = undefined
  }
}
