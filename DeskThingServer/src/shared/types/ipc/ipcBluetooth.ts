import { IPC_HANDLERS } from './ipcTypes'

/** Which link is actually carrying client traffic right now. */
export type BluetoothTransport = 'bluetooth' | 'usb' | 'none'

/** Which link the user wants to carry traffic when both are available. */
export type BluetoothPreference = 'bluetooth' | 'usb'

export interface BluetoothBridgeStatus {
  /** Whether this platform ships a Bluetooth bridge helper at all. */
  supported: boolean
  /** Whether the helper process is currently running. */
  running: boolean
  /** Whether an RFCOMM session to a device is established. */
  linkUp: boolean
  transport: BluetoothTransport
  preference: BluetoothPreference
}

export interface BluetoothProvisionStep {
  id: string
  label: string
  ok: boolean
  detail?: string
}

export interface BluetoothProvisionResult {
  success: boolean
  steps: BluetoothProvisionStep[]
}

export enum IPC_BLUETOOTH_TYPES {
  GET_STATUS = 'get-status',
  SET_PREFERENCE = 'set-preference',
  PROVISION_DEVICE = 'provision-device'
}

export type BluetoothIPCData = {
  kind: IPC_HANDLERS.BLUETOOTH
} & (
  | {
      type: IPC_BLUETOOTH_TYPES.GET_STATUS
      request: 'get'
    }
  | {
      type: IPC_BLUETOOTH_TYPES.SET_PREFERENCE
      request: 'set'
      payload: BluetoothPreference
    }
  | {
      type: IPC_BLUETOOTH_TYPES.PROVISION_DEVICE
      request: 'set'
      payload: { adbId: string }
    }
)

export type BluetoothHandlerReturnMap = {
  [IPC_BLUETOOTH_TYPES.GET_STATUS]: BluetoothBridgeStatus
  [IPC_BLUETOOTH_TYPES.SET_PREFERENCE]: BluetoothBridgeStatus
  [IPC_BLUETOOTH_TYPES.PROVISION_DEVICE]: BluetoothProvisionResult
}

export type BluetoothHandlerReturnType<K extends IPC_BLUETOOTH_TYPES> = BluetoothHandlerReturnMap[K]
