const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pumailAPI', {
  getAutoStart: () => ipcRenderer.invoke('get-auto-start'),
  setAutoStart: (enable) => ipcRenderer.invoke('set-auto-start', enable),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  openLocalPage: (name) => ipcRenderer.invoke('open-local-page', name),
});
