import {
  BluetoothBridgeStatus,
  BluetoothPreference,
  BluetoothProvisionResult
} from '@shared/types'
import {
  bridgeBinaryExists,
  isBridgeRunning,
  startBridgeProcess,
  stopBridgeProcess
} from './bridgeProcess'
import { fetchBridgeState, pushBridgePreference } from './bridgeClient'
import { provisionDevice } from './provisioner'

/**
 * Platform-neutral entry point for the Bluetooth transport.
 *
 * Everything outside this directory — lifecycle, IPC, UI — talks to this
 * interface only. Supporting a new OS means adding a manager implementation
 * here and shipping a helper for it; no call sites change.
 */
export interface BluetoothTransportManager {
  start(): void
  stop(): void
  getStatus(): Promise<BluetoothBridgeStatus>
  setPreference(preference: BluetoothPreference): Promise<BluetoothBridgeStatus>
  provision(adbId: string): Promise<BluetoothProvisionResult>
}

const UNSUPPORTED_STATUS: BluetoothBridgeStatus = {
  supported: false,
  running: false,
  linkUp: false,
  transport: 'none',
  preference: 'bluetooth'
}

/** macOS: an IOBluetooth helper shipped in Resources/mac, supervised by us. */
const helperBridgeManager: BluetoothTransportManager = {
  start: startBridgeProcess,
  stop: stopBridgeProcess,

  getStatus: async (): Promise<BluetoothBridgeStatus> => {
    const running = isBridgeRunning()
    const state = running ? await fetchBridgeState() : null
    return {
      supported: true,
      running,
      linkUp: state?.linkUp ?? false,
      transport: state?.transport ?? 'none',
      preference: state?.preference ?? 'bluetooth'
    }
  },

  setPreference: async (preference): Promise<BluetoothBridgeStatus> => {
    await pushBridgePreference(preference)
    return helperBridgeManager.getStatus()
  },

  provision: provisionDevice
}

/** Platforms without a helper: report unsupported, never fail. */
const unsupportedManager: BluetoothTransportManager = {
  start: (): void => {},
  stop: (): void => {},
  getStatus: async (): Promise<BluetoothBridgeStatus> => UNSUPPORTED_STATUS,
  setPreference: async (): Promise<BluetoothBridgeStatus> => UNSUPPORTED_STATUS,
  provision: async (): Promise<BluetoothProvisionResult> => ({
    success: false,
    steps: [
      {
        id: 'unsupported',
        label: 'Bluetooth transport',
        ok: false,
        detail: 'No Bluetooth bridge is available for this operating system yet'
      }
    ]
  })
}

export const bluetoothManager: BluetoothTransportManager =
  process.platform === 'darwin' && bridgeBinaryExists() ? helperBridgeManager : unsupportedManager
