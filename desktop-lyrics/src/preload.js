'use strict';
/* ==========================================================================
   preload：渲染进程与主进程之间**唯一**的通道
   --------------------------------------------------------------------------
   渲染进程开着 contextIsolation、关着 nodeIntegration，拿不到 require 和 fs，
   只能用这里显式暴露出去的几个函数。它只画歌词，所以需要的能力也很少 ——
   这正是这么设计的目的：即使渲染进程被一行恶意歌词注入了脚本，它也没有
   任何读写文件或联网的能力（歌词文本一律用 textContent 写入，见 overlay.js）。
   ========================================================================== */

const { contextBridge, ipcRenderer } = require('electron');

/** 把主进程推送过来的频道包装成「注册回调」，并把 event 参数藏掉。 */
function subscribe(channel, callback) {
  const listener = (event, payload) => callback(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('lyrics', {
  /** 渲染进程准备好接收状态了（主进程收到后会补发最近一份状态）。 */
  ready: () => ipcRenderer.send('lyrics:ready'),
  onState: (callback) => subscribe('lyrics:state', callback),
  onStatus: (callback) => subscribe('lyrics:status', callback),
  onConfig: (callback) => subscribe('lyrics:config', callback)
});

contextBridge.exposeInMainWorld('settings', {
  load: () => ipcRenderer.invoke('settings:load'),
  save: (payload) => ipcRenderer.invoke('settings:save', payload),
  test: (payload) => ipcRenderer.invoke('settings:test', payload),
  close: () => ipcRenderer.send('settings:close'),
  openReadme: () => ipcRenderer.send('settings:open-readme'),
  onChanged: (callback) => subscribe('settings:changed', callback)
});
