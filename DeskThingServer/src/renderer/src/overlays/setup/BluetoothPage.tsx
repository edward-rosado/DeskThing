import SponsorButton from '@renderer/components/SponsorButton'
import Button from '@renderer/components/Button'
import { IconRefresh } from '@renderer/assets/icons'
import { useClientStore } from '@renderer/stores'
import { TransportSelector } from '@renderer/overlays/modals/DeviceDetails/TransportSelector'
import { ClientConnectionMethod } from '@deskthing/types'
import { BluetoothBridgeStatus, BluetoothProvisionResult } from '@shared/types'
import React, { useEffect, useState } from 'react'

const POLL_MS = 4000

const BluetoothPage: React.FC = () => {
  const [status, setStatus] = useState<BluetoothBridgeStatus | null>(null)
  const clients = useClientStore((store) => store.clients)
  const refreshDevices = useClientStore((store) => store.requestADBDevices)
  const [refreshing, setRefreshing] = useState(false)
  const [provisioning, setProvisioning] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, BluetoothProvisionResult>>({})

  const adbDevices = clients.filter(
    (client) => client.manifest?.context.method === ClientConnectionMethod.ADB
  )

  useEffect(() => {
    const refresh = async (): Promise<void> => {
      try {
        setStatus(await window.electron.bluetooth.getStatus())
      } catch {
        setStatus(null)
      }
    }
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [])

  const handleRefreshDevices = async (): Promise<void> => {
    setRefreshing(true)
    await refreshDevices()
    setTimeout(() => setRefreshing(false), 1000)
  }

  const handleProvision = async (adbId: string): Promise<void> => {
    setProvisioning(adbId)
    try {
      const result = await window.electron.bluetooth.provision(adbId)
      setResults((prev) => ({ ...prev, [adbId]: result }))
    } finally {
      setProvisioning(null)
    }
  }

  return (
    <div className="w-full h-full p-8 flex flex-col overflow-y-auto">
      <h1 className="text-3xl font-bold mb-6 text-white">Bluetooth Settings</h1>
      <div className="w-full flex-col flex items-center h-full space-y-8">
        {status && !status.supported ? (
          <div className="bg-gray-800 p-8 rounded-lg shadow-lg text-center border border-green-500/20 max-w-2xl">
            <p className="text-xl text-gray-300 mb-2">
              The Bluetooth transport is currently only available on macOS.
            </p>
            <p className="text-gray-400">
              Your Car Thing will continue to work normally over its USB cable.
            </p>
          </div>
        ) : (
          <>
            <div className="w-full max-w-2xl">
              <TransportSelector />
            </div>

            <div className="bg-gray-800 p-8 rounded-lg shadow-lg border border-green-500/20 w-full max-w-2xl">
              <div className="flex justify-between items-center mb-4">
                <h2 className="text-xl font-semibold text-white">Set Up a Device</h2>
                <Button
                  className={refreshing ? 'text-gray-300' : 'hover:bg-zinc-900'}
                  onClick={handleRefreshDevices}
                >
                  <IconRefresh className={refreshing ? 'animate-spin' : ''} />
                  <p className="text-nowrap">Find Devices</p>
                </Button>
              </div>
              <p className="text-gray-300 mb-1">
                Enabling Bluetooth installs a small service on the Car Thing so it can reach this
                computer without a data cable — after setup it only needs power.
              </p>
              <p className="text-gray-400 text-sm mb-4">
                The device must be connected over USB for setup. A pairing request will appear on
                this computer during the process — accept it to finish.
              </p>

              {adbDevices.length === 0 ? (
                <p className="text-gray-500 italic">
                  No ADB devices connected. Plug the Car Thing in over USB and hit Find Devices.
                </p>
              ) : (
                <div className="space-y-4">
                  {adbDevices.map((device) => {
                    const adbId = device.meta.adb?.adbId
                    if (!adbId) return null
                    const result = results[adbId]
                    return (
                      <div key={adbId} className="bg-zinc-900 rounded-md p-4">
                        <div className="flex justify-between items-center">
                          <p className="text-white font-medium">{adbId}</p>
                          <Button
                            disabled={provisioning !== null}
                            onClick={() => handleProvision(adbId)}
                            className="border border-green-500 hover:bg-green-500 text-nowrap"
                          >
                            {provisioning === adbId
                              ? 'Setting up…'
                              : 'Enable Bluetooth on this device'}
                          </Button>
                        </div>
                        {result && (
                          <ul className="mt-3 space-y-1">
                            {result.steps.map((step) => (
                              <li key={step.id} className="flex items-start gap-2 text-sm">
                                <span className={step.ok ? 'text-green-400' : 'text-red-400'}>
                                  {step.ok ? '✓' : '✗'}
                                </span>
                                <span className="text-gray-300">
                                  {step.label}
                                  {step.detail && (
                                    <span className="text-gray-500"> — {step.detail}</span>
                                  )}
                                </span>
                              </li>
                            ))}
                            <li
                              className={`text-sm font-medium ${
                                result.success ? 'text-green-400' : 'text-red-400'
                              }`}
                            >
                              {result.success
                                ? 'Bluetooth is set up. You can unplug the cable — the device just needs power.'
                                : 'Setup did not finish. Fix the failing step and try again.'}
                            </li>
                          </ul>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </>
        )}

        <div className="flex flex-col items-center bg-gray-800 p-8 rounded-lg shadow-lg border border-green-500/20 transition-shadow hover:shadow-green-500/20 hover:shadow-xl">
          <p className="text-lg text-gray-300">Support the development of deskthing</p>
          <SponsorButton />
          <p className="text-gray-400 italic">Your support helps keep this project alive</p>
        </div>
      </div>
    </div>
  )
}

export default BluetoothPage
