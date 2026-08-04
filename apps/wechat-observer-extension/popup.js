(function configureObserver() {
  "use strict";

  const form = document.querySelector("#pairing-form");
  const sourceInput = document.querySelector("#source-id");
  const codeInput = document.querySelector("#pairing-code");
  const apiInput = document.querySelector("#api-base");
  const status = document.querySelector("#status");
  const button = form.querySelector("button[type=submit]");

  function setStatus(message, kind = "") {
    status.textContent = message;
    status.className = kind;
  }

  void browser.storage.local.get(["source_id", "api_base"]).then((saved) => {
    if (typeof saved.source_id === "string") sourceInput.value = saved.source_id;
    if (typeof saved.api_base === "string") apiInput.value = saved.api_base;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const sourceId = sourceInput.value.trim();
    const pairingCode = codeInput.value.trim();
    const apiBase = apiInput.value.trim();
    button.disabled = true;
    setStatus("正在连接本机 Pocket…");
    try {
      await browser.runtime.sendMessage({
        type: "observer.configure",
        source_id: sourceId,
        pairing_code: pairingCode,
        api_base: apiBase,
      });
      codeInput.value = "";
      await browser.storage.local.set({ source_id: sourceId, api_base: apiBase });
      setStatus("配对成功。现在打开 wx.qq.com 并扫码登录。", "success");
    } catch (error) {
      codeInput.value = "";
      setStatus(error && error.message ? error.message : "配对失败", "error");
    } finally {
      button.disabled = false;
    }
  });
})();
