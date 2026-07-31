"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld(
  "centaurPocketDesktop",
  Object.freeze({
    getBootstrapSettings: () =>
      ipcRenderer.invoke("centaur-pocket:get-bootstrap-settings"),
    request: (input) => ipcRenderer.invoke("centaur-pocket:api-request", input),
    selectFolder: () => ipcRenderer.invoke("centaur-pocket:select-folder"),
  }),
);
