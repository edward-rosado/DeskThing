import { defineConfig } from 'vitest/config'
import { resolve } from 'path'
export default defineConfig({
  test: {
    environment: 'node',
    include: ['**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html']
    },
    alias: {
      // `electron/main` is a real Electron runtime subpath, but it does not
      // resolve under plain node -- electron's npm package only exports a stub
      // index.js. Six files in src/main import it (settingsStore, thingifyStore,
      // statsCollectionStore, flashStore, envBuilder, protocol), and any suite
      // that transitively pulls one in failed to LOAD, not to assert.
      //
      // Mapping it onto 'electron' makes the module id identical to the one the
      // suites already `vi.mock('electron', ...)`, so the existing mocks apply
      // and no production import has to change to suit the test runner.
      'electron/main': 'electron',
      '@renderer': resolve('src/renderer/src'),
      '@server': resolve('src/main'),
      '@shared': resolve('src/shared'),
      '@utilities': resolve('src/main/utilities'),
      '@processes': resolve('src/main/processes')
    }
  }
})
