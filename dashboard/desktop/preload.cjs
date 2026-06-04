const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('homieDesktop', {
  getConfig: () => ipcRenderer.invoke('config:get'),
  saveConfig: (config) => ipcRenderer.invoke('config:save', config),
  status: () => ipcRenderer.invoke('stack:status'),
  startStack: () => ipcRenderer.invoke('stack:start'),
  stopStack: () => ipcRenderer.invoke('stack:stop'),
  openOperatingRoom: () => ipcRenderer.invoke('operating-room:open'),
  onStackEvent: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('stack:event', listener);
    return () => ipcRenderer.removeListener('stack:event', listener);
  },
});
