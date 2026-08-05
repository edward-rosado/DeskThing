/**
 * Compiles the macOS Bluetooth bridge helper from source so the binary never
 * has to live in git. Runs as part of the build chain; on platforms without a
 * helper it is a no-op so cross-platform builds keep working.
 */
import { spawnSync } from 'child_process'
import path from 'path'
import { fileURLToPath } from 'url'

if (process.platform !== 'darwin') {
  console.log('build-btbridge: no Bluetooth helper for this platform, skipping')
  process.exit(0)
}

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..')
const source = path.join(root, 'bt_source', 'btbridge.swift')
const output = path.join(root, 'bt_source', 'mac', 'btbridge')

const result = spawnSync(
  'swiftc',
  ['-O', source, '-o', output, '-framework', 'IOBluetooth', '-framework', 'Network'],
  { stdio: 'inherit' }
)

if (result.error) {
  console.error(`build-btbridge: failed to run swiftc: ${result.error.message}`)
  process.exit(1)
}
if (result.status !== 0) {
  console.error(`build-btbridge: swiftc exited with status ${result.status}`)
  process.exit(result.status ?? 1)
}

console.log(`build-btbridge: built ${output}`)
