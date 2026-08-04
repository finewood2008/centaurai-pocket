(function startCentaurWechatObserver() {
  "use strict";

  const core = globalThis.CentaurWechatObserverCore;
  if (!core || location.origin !== "https://wx.qq.com") return;

  const HEARTBEAT_INTERVAL_MS = 20_000;
  const ROOT_DISCOVERY_INTERVAL_MS = 1_000;
  const BATCH_DELAY_MS = 750;
  const MAX_BATCH_EVENTS = 25;
  const MAX_QUEUE_EVENTS = 200;
  const MAX_DEDUPE_KEYS = 10_000;

  const browserSessionId = crypto.randomUUID();
  const dedupeKeys = new Set();
  const dedupeOrder = [];
  const queue = [];
  let chatArea = null;
  let observer = null;
  let flushTimer = null;
  let paired = false;
  let capturePaused = true;
  let state = "awaiting_pairing";
  let lastDisplayedTime = "";
  let lastDomChangeAt = null;
  let lastEventAt = null;
  let activeConversationId = null;
  let recoveryInProgress = false;
  let nextRecoveryAt = 0;

  function isoNow() {
    return new Date().toISOString();
  }

  function rememberDedupeKey(key) {
    if (dedupeKeys.has(key)) return false;
    dedupeKeys.add(key);
    dedupeOrder.push(key);
    if (dedupeOrder.length > MAX_DEDUPE_KEYS) {
      dedupeKeys.delete(dedupeOrder.shift());
    }
    return true;
  }

  function pageState() {
    if (document.querySelector("#chatArea")) return "active";
    if (document.querySelector(".qrcode, .login_box, #login_container, .login")) {
      return "login_required";
    }
    return "browser_offline";
  }

  function unreadConversationCount() {
    const conversations = document.querySelectorAll(".chat_item:not(.active)");
    let count = 0;
    for (const conversation of conversations) {
      if (conversation.querySelector(".web_wechat_reddot, .web_wechat_reddot_middle")) count += 1;
    }
    return count;
  }

  function currentConversation() {
    const root = document.querySelector("#chatArea");
    return root ? core.getConversation(root) : null;
  }

  async function sendHeartbeat(explicitState) {
    const conversation = currentConversation();
    const heartbeat = {
      browser_session_id: browserSessionId,
      state: explicitState || pageState(),
      observed_at: isoNow(),
      extension_version: browser.runtime.getManifest().version,
      parser_version: core.PARSER_VERSION,
      unread_conversation_count: unreadConversationCount(),
    };
    if (conversation) {
      heartbeat.current_conversation_id = conversation.id;
      if (conversation.name) heartbeat.current_conversation_name = conversation.name;
    }
    try {
      await browser.runtime.sendMessage({ type: "observer.heartbeat", body: heartbeat });
      paired = true;
      return true;
    } catch (_error) {
      paired = false;
      capturePaused = true;
      state = "capture_paused";
      detachObserver();
      return false;
    }
  }

  function scheduleFlush() {
    if (flushTimer || capturePaused || queue.length === 0) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      void flushQueue();
    }, BATCH_DELAY_MS);
  }

  async function flushQueue() {
    if (capturePaused || queue.length === 0) return;
    const events = queue.slice(0, MAX_BATCH_EVENTS);
    const body = {
      batch_id: crypto.randomUUID(),
      browser_session_id: browserSessionId,
      events,
    };
    try {
      await browser.runtime.sendMessage({ type: "observer.events", body });
      queue.splice(0, events.length);
      lastEventAt = isoNow();
      if (queue.length) scheduleFlush();
    } catch (_error) {
      capturePaused = true;
      state = "capture_paused";
      detachObserver();
      void sendHeartbeat("capture_paused");
    }
  }

  function queueEvent(event) {
    const key = `${event.provider_conversation_id}\u0000${event.provider_msgid}`;
    if (dedupeKeys.has(key)) return;
    if (queue.length >= MAX_QUEUE_EVENTS) {
      capturePaused = true;
      state = "capture_paused";
      detachObserver();
      void sendHeartbeat("capture_paused");
      return;
    }
    rememberDedupeKey(key);
    queue.push(event);
    scheduleFlush();
  }

  function extractElement(element) {
    if (capturePaused || !element || !element.closest("#chatArea")) return;
    const conversation = currentConversation();
    if (!conversation || !conversation.id) {
      capturePaused = true;
      state = "parser_degraded";
      detachObserver();
      void sendHeartbeat("parser_degraded");
      return;
    }
    if (conversation && conversation.id !== activeConversationId) {
      activeConversationId = conversation.id;
      lastDisplayedTime = "";
    }
    if (core.isTimeSeparator(element)) {
      lastDisplayedTime = core.normalizeText(element.textContent || "", 128);
    }
    const message = core.findMessageElement(element);
    if (!message) return;
    const event = core.extractMessage(message, { displayedTime: lastDisplayedTime });
    if (event) queueEvent(event);
  }

  function scanRenderedMessages(root) {
    if (!root || capturePaused) return;
    const candidates = root.querySelectorAll(".message_system, [data-cm]");
    for (const candidate of candidates) extractElement(candidate);
  }

  function attachObserver(root) {
    if (root === chatArea && observer) return;
    detachObserver();
    chatArea = root;
    lastDisplayedTime = "";
    activeConversationId = null;
    observer = new MutationObserver((records) => {
      lastDomChangeAt = isoNow();
      for (const record of records) {
        if (record.type === "characterData" && record.target.parentElement) {
          extractElement(record.target.parentElement);
        }
        for (const node of record.addedNodes) {
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          extractElement(node);
          if (node.querySelectorAll) {
            for (const descendant of node.querySelectorAll(".message_system, [data-cm]")) {
              extractElement(descendant);
            }
          }
        }
      }
    });
    observer.observe(root, { childList: true, subtree: true, characterData: true });
    scanRenderedMessages(root);
  }

  function detachObserver() {
    if (observer) observer.disconnect();
    observer = null;
    chatArea = null;
    activeConversationId = null;
  }

  async function handshake() {
    state = "awaiting_pairing";
    try {
      await browser.runtime.sendMessage({
        type: "observer.handshake",
      });
      paired = true;
      state = pageState();
    } catch (_error) {
      paired = false;
      capturePaused = true;
      state = "capture_paused";
    }
  }

  async function rediscover() {
    if ((!paired || capturePaused) && !recoveryInProgress && Date.now() >= nextRecoveryAt) {
      recoveryInProgress = true;
      nextRecoveryAt = Date.now() + 5_000;
      try {
        if (!paired) await handshake();
        if (paired) {
          const desiredState = pageState();
          const conversation = currentConversation();
          const activationState =
            desiredState === "active" && (!conversation || !conversation.id)
              ? "parser_degraded"
              : desiredState;
          const heartbeatAccepted = await sendHeartbeat(activationState);
          if (heartbeatAccepted && activationState !== "parser_degraded") {
            capturePaused = false;
            state = desiredState;
            scheduleFlush();
          } else if (heartbeatAccepted) {
            capturePaused = true;
            state = "parser_degraded";
          }
        }
      } finally {
        recoveryInProgress = false;
      }
    }
    if (!capturePaused) {
      const current = document.querySelector("#chatArea");
      if (queue.length >= MAX_QUEUE_EVENTS) detachObserver();
      else if (current && current !== chatArea) attachObserver(current);
      else if (!current) detachObserver();
    }
    state = capturePaused ? "capture_paused" : pageState();
  }

  void (async () => {
    await rediscover();
    setInterval(() => void rediscover(), ROOT_DISCOVERY_INTERVAL_MS);
    setInterval(() => {
      if (!capturePaused && paired) void sendHeartbeat(pageState());
    }, HEARTBEAT_INTERVAL_MS);
  })();

  // These timestamps deliberately remain internal. The collector receives the
  // canonical fields defined by its API; heartbeat gaps are derived server-side.
  void lastDomChangeAt;
  void lastEventAt;
})();
