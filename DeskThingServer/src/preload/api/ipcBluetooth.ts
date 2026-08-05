import {
  IPC_HANDLERS,
  IPC_BLUETOOTH_TYPES,
  BluetoothIPCData,
  BluetoothHandlerReturnMap,
  BluetoothBridgeStatus,
  BluetoothPreference,
  BluetoothProvisionResult
} from '@shared/types'
import { ipcRenderer } from 'electron'

export const bluetooth = {
  getStatus: async (): Promise<BluetoothBridgeStatus> =>
    await sendCommand({
      kind: IPC_HANDLERS.BLUETOOTH,
      type: IPC_BLUETOOTH_TYPES.GET_STATUS,
      request: 'get'
    }),
  setPreference: async (preference: BluetoothPreference): Promise<BluetoothBridgeStatus> =>
    await sendCommand({
      kind: IPC_HANDLERS.BLUETOOTH,
      type: IPC_BLUETOOTH_TYPES.SET_PREFERENCE,
      request: 'set',
      payload: preference
    }),
  provision: async (adbId: string): Promise<BluetoothProvisionResult> =>
    await sendCommand({
      kind: IPC_HANDLERS.BLUETOOTH,
      type: IPC_BLUETOOTH_TYPES.PROVISION_DEVICE,
      request: 'set',
      payload: { adbId }
    })
}

const sendCommand = <T extends IPC_BLUETOOTH_TYPES>(
  payload: Extract<BluetoothIPCData, { type: T }>
): Promise<BluetoothHandlerReturnMap[T]> => {
  const requestPayload = { ...payload, kind: IPC_HANDLERS.BLUETOOTH }
  return ipcRenderer.invoke(IPC_HANDLERS.BLUETOOTH, requestPayload)
}
