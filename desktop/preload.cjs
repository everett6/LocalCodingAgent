const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('localCoder', {
  request: (path, method, body) => ipcRenderer.invoke('engine:request', path, method, body),
  chooseInferenceFile: (purpose) => ipcRenderer.invoke('inference:choose-file', purpose),
  chooseProject: () => ipcRenderer.invoke('workspace:choose'),
  openIDE: () => ipcRenderer.invoke('ide:open'),
});
