/**
 * A stand-in for the `electron` module in tests.
 *
 * Why this exists: CI installs with `npm ci --ignore-scripts` (electron's
 * postinstall downloads a ~100MB binary no source test needs), and without that
 * binary the real `node_modules/electron/index.js` THROWS on import —
 * "Electron failed to install correctly". On a developer machine the binary is
 * present, so the throw never happens and the gap is invisible until CI.
 *
 * vitest aliases both `electron` and `electron/main` here, so a suite that does
 * not call `vi.mock('electron', ...)` gets a working stub instead of a crash.
 * Suites that DO mock still win — vi.mock overrides the alias.
 *
 * Deliberately minimal: only the surface src/main actually touches (measured —
 * app.getPath is 43 call sites, app.getVersion 8, plus the window/tray/dialog
 * objects). Extend it when a test needs more, rather than mirroring Electron.
 */
const noop = (): void => undefined

export const app = {
  getPath: (name: string): string => `/tmp/deskthing-test/${name}`,
  getAppPath: (): string => '/tmp/deskthing-test/app',
  getVersion: (): string => '0.0.0-test',
  getName: (): string => 'deskthing-test',
  setName: noop,
  quit: noop,
  exit: noop,
  relaunch: noop,
  on: noop,
  once: noop,
  off: noop,
  whenReady: (): Promise<void> => Promise.resolve(),
  requestSingleInstanceLock: (): boolean => true,
  setAsDefaultProtocolClient: (): boolean => true,
  isPackaged: false,
  dock: { hide: noop, show: noop, setIcon: noop },
}

class StubBrowserWindow {
  static getAllWindows = (): unknown[] => []
  static fromWebContents = (): null => null
  webContents = { send: noop, on: noop, openDevTools: noop, setWindowOpenHandler: noop }
  on = noop
  once = noop
  loadURL = (): Promise<void> => Promise.resolve()
  loadFile = (): Promise<void> => Promise.resolve()
  show = noop
  hide = noop
  close = noop
  destroy = noop
  isDestroyed = (): boolean => false
  minimize = noop
  setMenuBarVisibility = noop
}
export const BrowserWindow = StubBrowserWindow

export const ipcMain = { on: noop, once: noop, handle: noop, removeHandler: noop, emit: noop }
export const shell = {
  openExternal: (): Promise<void> => Promise.resolve(),
  openPath: (): Promise<string> => Promise.resolve(''),
  showItemInFolder: noop,
}
export const dialog = {
  showOpenDialog: (): Promise<{ canceled: boolean; filePaths: string[] }> =>
    Promise.resolve({ canceled: true, filePaths: [] }),
  showMessageBox: (): Promise<{ response: number }> => Promise.resolve({ response: 0 }),
  showErrorBox: noop,
}
export const Menu = {
  buildFromTemplate: (): unknown => ({}),
  setApplicationMenu: noop,
}
export class Tray {
  setToolTip = noop
  setContextMenu = noop
  on = noop
  destroy = noop
}
export const nativeImage = {
  createFromPath: (): unknown => ({ isEmpty: () => true, resize: () => ({}) }),
  createEmpty: (): unknown => ({ isEmpty: () => true }),
}
export class Notification {
  show = noop
  on = noop
  static isSupported = (): boolean => false
}
export const net = { request: (): unknown => ({ on: noop, end: noop }) }
export const protocol = { handle: noop, registerSchemesAsPrivileged: noop }
export const utilityProcess = { fork: (): unknown => ({ on: noop, postMessage: noop, kill: noop }) }

export default {
  app, BrowserWindow, ipcMain, shell, dialog, Menu, Tray,
  nativeImage, Notification, net, protocol, utilityProcess,
}
