import { BluetoothPreference, BluetoothTransport } from '@shared/types'

/**
 * Client for the bridge helper's local control API.
 *
 * The helper owns the RFCOMM link and the USB-reverse arbitration, and exposes
 * its state on a loopback port. This module is the only place that knows that
 * protocol; everything else in the app goes through the manager.
 */

const CONTROL_URL = 'http://127.0.0.1:8899'
const TIMEOUT_MS = 3000

export interface BridgeControlState {
  preference: BluetoothPreference
  transport: BluetoothTransport
  linkUp: boolean
}

const request = async (path: string, init?: RequestInit): Promise<BridgeControlState | null> => {
  try {
    const res = await fetch(`${CONTROL_URL}${path}`, {
      ...init,
      signal: AbortSignal.timeout(TIMEOUT_MS)
    })
    if (!res.ok) return null
    return (await res.json()) as BridgeControlState
  } catch {
    return null
  }
}

/** Returns the helper's live state, or null when the helper isn't reachable. */
export const fetchBridgeState = (): Promise<BridgeControlState | null> => request('/status')

/** Sets the transport preference; returns the resulting state or null. */
export const pushBridgePreference = (
  preference: BluetoothPreference
): Promise<BridgeControlState | null> =>
  request('/preference', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preference })
  })
