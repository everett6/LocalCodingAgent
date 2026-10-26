const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('localCoder', {
  request: (path, method, body) => ipcRenderer.invoke('engine:request', path, method, body),
  chooseProject: () => ipcRenderer.invoke('workspace:choose'),
  openIDE: () => ipcRenderer.invoke('ide:open'),
});
