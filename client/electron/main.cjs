const { app, BrowserWindow, shell } = require('electron')
const path = require('node:path')
const { pathToFileURL } = require('node:url')

function createWindow() {
  const entry = path.join(__dirname, '..', 'dist', 'index.html')
  const allowedUrl = pathToFileURL(entry).href
  const window = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 380,
    minHeight: 600,
    backgroundColor: '#080a0f',
    title: 'Unknown Agent',
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      devTools: !app.isPackaged,
    },
  })
  window.removeMenu()
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url)
    return { action: 'deny' }
  })
  window.webContents.on('will-navigate', (event, url) => {
    if (url !== allowedUrl) event.preventDefault()
  })
  window.once('ready-to-show', () => window.show())
  window.loadFile(entry)
}

app.whenReady().then(() => {
  createWindow()
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow() })
})
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
