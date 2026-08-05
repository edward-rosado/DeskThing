import { execFile } from 'child_process'
import { promisify } from 'util'
import { handleAdbCommands } from '@server/handlers/adbHandler'
import Logger from '@server/utils/logger'
import { LOGGING_LEVELS } from '@deskthing/types'
import { BluetoothProvisionResult, BluetoothProvisionStep } from '@shared/types'
import { deviceMuxScriptPath } from './bridgeProcess'

/**
 * One-time device provisioning for the Bluetooth transport.
 *
 * Runs over adb while the Car Thing is on USB, and leaves the device able to
 * reach the server over Bluetooth on every subsequent boot: the mux service is
 * installed under supervisord, bluetoothd gains the compat flag it needs to
 * advertise a serial port, and the device is paired with this computer.
 *
 * Every step is recorded so the UI can show exactly what happened; a failed
 * step stops the sequence.
 */

const execFileAsync = promisify(execFile)

/** RFCOMM channel the device listens on; must match the helper and the mux. */
const RFCOMM_CHANNEL = 3

const MUX_REMOTE_PATH = '/etc/deskthing-bt/btmux.py'

const SUPERVISOR_CONF =
  '[program:btmux]\\n' +
  'command=/usr/bin/python3 /etc/deskthing-bt/btmux.py 8891\\n' +
  'autostart=true\\n' +
  'autorestart=true\\n' +
  'startretries=999\\n' +
  'stopasgroup=true\\n' +
  'killasgroup=true\\n' +
  'redirect_stderr=true\\n' +
  'stdout_logfile=/var/log/btmux.log\\n' +
  'stdout_logfile_maxbytes=512KB\\n' +
  'stdout_logfile_backups=1\\n'

/** The Bluetooth address of this computer's adapter, or null if unknown. */
export const getHostBluetoothAddress = async (): Promise<string | null> => {
  if (process.platform !== 'darwin') return null
  try {
    const { stdout } = await execFileAsync('system_profiler', ['SPBluetoothDataType', '-json'])
    const data = JSON.parse(stdout)
    const address: unknown =
      data?.SPBluetoothDataType?.[0]?.controller_properties?.controller_address
    return typeof address === 'string' ? address.toUpperCase() : null
  } catch {
    return null
  }
}

type StepRunner = () => Promise<string>

const runSteps = async (
  steps: Array<{ id: string; label: string; run: StepRunner }>
): Promise<BluetoothProvisionResult> => {
  const results: BluetoothProvisionStep[] = []
  for (const step of steps) {
    try {
      const detail = await step.run()
      results.push({ id: step.id, label: step.label, ok: true, detail })
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      Logger.log(LOGGING_LEVELS.ERROR, `[btProvision] ${step.id} failed: ${detail}`)
      results.push({ id: step.id, label: step.label, ok: false, detail })
      return { success: false, steps: results }
    }
  }
  return { success: true, steps: results }
}

export const provisionDevice = async (adbId: string): Promise<BluetoothProvisionResult> => {
  const adb = (args: string): Promise<string> => handleAdbCommands(`-s ${adbId} ${args}`)

  const hostAddress = await getHostBluetoothAddress()

  const steps: Array<{ id: string; label: string; run: StepRunner }> = [
    {
      id: 'remount',
      label: 'Make device filesystem writable',
      run: () => adb('shell mount -o remount,rw /')
    },
    {
      id: 'push-mux',
      label: 'Install Bluetooth service on device',
      run: async () => {
        await adb('shell mkdir -p /etc/deskthing-bt')
        return adb(`push "${deviceMuxScriptPath}" ${MUX_REMOTE_PATH}`)
      }
    },
    {
      id: 'supervisor',
      label: 'Register service to start at boot',
      run: async () => {
        await adb('shell mkdir -p /etc/supervisor.d')
        return adb(`shell "printf '${SUPERVISOR_CONF}' > /etc/supervisor.d/btmux.conf"`)
      }
    },
    {
      id: 'bluetoothd-compat',
      label: 'Enable serial port support in device Bluetooth',
      run: async () => {
        // The stock image runs bluetoothd without --compat, which the SDP
        // registration in the mux needs. Idempotent: only patch once.
        const script = await adb('shell cat /etc/start_bluetoothd.sh')
        if (!script.includes('--compat')) {
          await adb(
            `shell "sed -i 's|bluetoothd -n -d|bluetoothd -n -d --compat|' /etc/start_bluetoothd.sh"`
          )
          // Restarting via supervisor orphans the old daemon; kill it explicitly.
          await adb(
            `shell "for p in \\$(pidof bluetoothd); do kill \\$p; done; supervisorctl restart bluetoothd"`
          )
          return 'patched and restarted'
        }
        return 'already enabled'
      }
    },
    {
      id: 'start-mux',
      label: 'Start the Bluetooth service',
      run: async () => {
        await adb('shell supervisorctl reread')
        await adb('shell supervisorctl update')
        await adb('shell "supervisorctl restart btmux || supervisorctl start btmux"')
        const status = await adb('shell supervisorctl status btmux')
        if (!status.includes('RUNNING')) throw new Error(`service not running: ${status.trim()}`)
        return status.trim()
      }
    },
    {
      id: 'pair',
      label: 'Pair device with this computer',
      run: async () => {
        if (!hostAddress) {
          // Without the host adapter address the device can't initiate pairing;
          // the user can still pair manually from the OS Bluetooth settings.
          return 'skipped — host Bluetooth address unavailable on this platform'
        }
        const paired = await adb(`shell bluetoothctl info ${hostAddress}`)
        if (paired.includes('Paired: yes')) return 'already paired'
        // Device-initiated pairing with numeric confirmation; the OS shows a
        // dialog on this computer that the user confirms. macOS rejects
        // just-works pairing, so DisplayYesNo with an auto-yes is required.
        const out = await adb(
          `shell "(printf 'power on\\nagent DisplayYesNo\\ndefault-agent\\npairable on\\npair ${hostAddress}\\n'; sleep 4; printf 'yes\\n'; sleep 12; printf 'trust ${hostAddress}\\nquit\\n'; sleep 1) | bluetoothctl"`
        )
        if (!out.includes('Pairing successful') && !out.includes('AlreadyExists')) {
          throw new Error('pairing did not complete — accept the prompt on this computer and retry')
        }
        return 'paired and trusted'
      }
    },
    {
      id: 'verify-radio',
      label: 'Verify device radio is connectable',
      run: async () => {
        const flags = await adb('shell hciconfig hci0')
        if (!flags.includes('PSCAN')) throw new Error('page scan not enabled')
        const records = await adb('shell sdptool browse local')
        if (!records.includes('Serial Port')) throw new Error('serial port not advertised')
        return `serial port on channel ${RFCOMM_CHANNEL}, radio connectable`
      }
    }
  ]

  const result = await runSteps(steps)
  Logger.log(
    result.success ? LOGGING_LEVELS.LOG : LOGGING_LEVELS.WARN,
    `[btProvision] ${adbId}: ${result.success ? 'complete' : 'failed'} (${result.steps.length} steps)`
  )
  return result
}
