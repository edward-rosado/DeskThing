import {
  BluetoothIPCData,
  IPC_BLUETOOTH_TYPES,
  BluetoothHandlerReturnMap
} from '@shared/types/ipc/ipcBluetooth'
import { bluetoothManager } from '@server/services/bluetooth'

export const bluetoothHandler = async (
  data: BluetoothIPCData
): Promise<BluetoothHandlerReturnMap[(typeof data)['type']]> => {
  switch (data.type) {
    case IPC_BLUETOOTH_TYPES.GET_STATUS:
      return await bluetoothManager.getStatus()
    case IPC_BLUETOOTH_TYPES.SET_PREFERENCE:
      return await bluetoothManager.setPreference(data.payload)
    case IPC_BLUETOOTH_TYPES.PROVISION_DEVICE:
      return await bluetoothManager.provision(data.payload.adbId)
  }
}
