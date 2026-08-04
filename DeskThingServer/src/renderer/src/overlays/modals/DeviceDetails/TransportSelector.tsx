import { FC, useCallback, useEffect, useState } from 'react'

/**
 * Shows which transport is actually carrying data to the Car Thing, and lets the
 * user pin a preference.
 *
 * The data comes from the Bluetooth bridge's local control API rather than from
 * the server, because the bridge is the component that owns the RFCOMM link and
 * arbitrates the USB `adb reverse`. If that bridge isn't running, this section
 * hides itself so a plain USB setup looks no different than before.
 */

const CONTROL_API = 'http://127.0.0.1:8899'
const POLL_MS = 4000

type Transport = 'bluetooth' | 'usb' | 'none'
type Preference = 'bluetooth' | 'usb'

type BridgeStatus = {
  preference: Preference
  transport: Transport
  linkUp: boolean
}

const TRANSPORT_LABEL: Record<Transport, string> = {
  bluetooth: 'Bluetooth',
  usb: 'USB Cable',
  none: 'Not Connected'
}

const TRANSPORT_COLOR: Record<Transport, string> = {
  bluetooth: 'text-sky-400',
  usb: 'text-amber-400',
  none: 'text-zinc-500'
}

export const TransportSelector: FC = () => {
  const [status, setStatus] = useState<BridgeStatus | null>(null)
  const [available, setAvailable] = useState(true)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async (): Promise<void> => {
    try {
      const res = await fetch(`${CONTROL_API}/status`, { cache: 'no-store' })
      if (!res.ok) throw new Error(`status ${res.status}`)
      setStatus((await res.json()) as BridgeStatus)
      setAvailable(true)
    } catch {
      setAvailable(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  const choose = async (preference: Preference): Promise<void> => {
    setBusy(true)
    try {
      const res = await fetch(`${CONTROL_API}/preference`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preference })
      })
      if (res.ok) setStatus((await res.json()) as BridgeStatus)
    } catch {
      setAvailable(false)
    } finally {
      setBusy(false)
    }
  }

  if (!available || !status) return null

  const active = status.transport

  return (
    <section className="bg-zinc-800 rounded-lg p-6 shadow-lg">
      <h2 className="text-xl font-semibold mb-4 flex items-center gap-2">
        <span className="text-sky-400">📶</span>
        Connection Type
      </h2>

      <div className="space-y-3">
        <div className="flex justify-between items-center py-2 border-b border-zinc-700">
          <span className="text-zinc-400">Carrying Data</span>
          <span className={`font-medium flex items-center gap-2 ${TRANSPORT_COLOR[active]}`}>
            <span
              className={`h-2 w-2 rounded-full ${
                active === 'none' ? 'bg-zinc-600' : 'bg-current animate-pulse'
              }`}
            />
            {TRANSPORT_LABEL[active]}
          </span>
        </div>

        <div className="py-2">
          <div className="flex justify-between items-center mb-2">
            <span className="text-zinc-400">Prefer</span>
            {status.preference === 'bluetooth' && (
              <span className="text-xs text-zinc-500">recommended</span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2">
            {(['bluetooth', 'usb'] as Preference[]).map((option) => {
              const selected = status.preference === option
              return (
                <button
                  key={option}
                  disabled={busy}
                  onClick={() => choose(option)}
                  className={`rounded-md px-3 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${
                    selected
                      ? 'bg-sky-600 text-white'
                      : 'bg-zinc-700 text-zinc-300 hover:bg-zinc-600'
                  }`}
                >
                  {option === 'bluetooth' ? 'Bluetooth' : 'USB Cable'}
                </button>
              )
            })}
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            {status.preference === 'bluetooth'
              ? 'Bluetooth is used whenever it is available, falling back to USB automatically.'
              : 'Pinned to the USB cable. Bluetooth stays disconnected until you switch back.'}
          </p>
        </div>
      </div>
    </section>
  )
}
