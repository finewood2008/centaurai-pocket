(function observerCoreFactory(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.CentaurWechatObserverCore = Object.freeze(api);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function buildObserverCore() {
  "use strict";

  const PARSER_VERSION = "visible-dom-v1";
  const MAX_TEXT_LENGTH = 16_000;
  const MAX_LABEL_LENGTH = 500;

  const MESSAGE_TYPE_NAMES = Object.freeze({
    1: "text",
    3: "image",
    34: "voice",
    37: "other",
    42: "other",
    43: "video",
    47: "other",
    48: "other",
    49: "file",
    62: "video",
    10000: "system",
  });

  function normalizeText(value, maximum = MAX_TEXT_LENGTH) {
    if (typeof value !== "string") return "";
    return value
      .replace(/\u00a0/g, " ")
      .replace(/\r\n?/g, "\n")
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
      .slice(0, maximum);
  }

  function normalizeLabel(value, maximum = MAX_LABEL_LENGTH) {
    return normalizeText(value, maximum).replace(/\s+/g, " ");
  }

  function safeJsonObject(value) {
    if (typeof value !== "string" || value.length === 0 || value.length > 32_768) {
      return null;
    }
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    } catch (_error) {
      return null;
    }
    return null;
  }

  function firstDefined(object, keys) {
    for (const key of keys) {
      if (object[key] !== undefined && object[key] !== null) return object[key];
    }
    return undefined;
  }

  function parseDataCm(rawValue) {
    const parsed = safeJsonObject(rawValue);
    if (!parsed) return null;
    const msgId = firstDefined(parsed, ["msgId", "MsgId", "NewMsgId"]);
    if (typeof msgId !== "string" && typeof msgId !== "number") return null;
    const normalizedId = String(msgId).trim();
    if (!normalizedId || normalizedId.length > MAX_LABEL_LENGTH) return null;

    const actualSender = firstDefined(parsed, ["actualSender", "ActualSender"]);
    const msgType = firstDefined(parsed, ["msgType", "MsgType"]);
    return {
      msgId: normalizedId,
      actualSender:
        typeof actualSender === "string"
          ? normalizeLabel(actualSender, MAX_LABEL_LENGTH)
          : null,
      msgType:
        typeof msgType === "number" || typeof msgType === "string"
          ? String(msgType).trim().slice(0, 64)
          : "unknown",
    };
  }

  function mapMessageType(rawType) {
    if (Object.prototype.hasOwnProperty.call(MESSAGE_TYPE_NAMES, rawType)) {
      return MESSAGE_TYPE_NAMES[rawType];
    }
    return "other";
  }

  function stableDisplayKey(value) {
    const input = normalizeLabel(value, MAX_LABEL_LENGTH).toLowerCase();
    let hash = 0x811c9dc5;
    for (let index = 0; index < input.length; index += 1) {
      hash ^= input.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return `display:${hash.toString(16).padStart(8, "0")}`;
  }

  function selectText(element, selectors, maximum = MAX_TEXT_LENGTH, singleLine = false) {
    for (const selector of selectors) {
      const match = element.querySelector(selector);
      if (!match) continue;
      if (match.closest && match.closest("#chatArea") && !isActuallyVisible(match)) continue;
      const raw = normalizeText(match.textContent || "", maximum);
      const value = singleLine ? raw.replace(/\s+/g, " ") : raw;
      if (value) return value;
    }
    return "";
  }

  function containsClass(element, name) {
    if (!element) return false;
    if (element.classList && typeof element.classList.contains === "function") {
      return element.classList.contains(name);
    }
    return String(element.className || "")
      .split(/\s+/)
      .includes(name);
  }

  function isActuallyVisible(element, view) {
    if (!element || typeof element.closest !== "function") return false;
    if (element.closest("#prerender")) return false;
    const chatArea = element.closest("#chatArea");
    if (!chatArea) return false;

    const windowLike = view || (element.ownerDocument && element.ownerDocument.defaultView);
    let current = element;
    while (current) {
      if (current.hidden || current.getAttribute("aria-hidden") === "true") return false;
      if (windowLike && typeof windowLike.getComputedStyle === "function") {
        const style = windowLike.getComputedStyle(current);
        if (
          style &&
          (style.display === "none" || style.visibility === "hidden" || style.opacity === "0")
        ) {
          return false;
        }
      }
      if (current === chatArea) break;
      current = current.parentElement;
    }

    // Firefox reports client rects for elements that participate in layout. Unit-test
    // fixtures can omit getClientRects; production DOM nodes cannot.
    if (typeof element.getClientRects === "function" && element.getClientRects().length === 0) {
      return false;
    }
    return true;
  }

  function getConversation(chatArea) {
    const documentLike = chatArea.ownerDocument;
    const activeConversation =
      documentLike && typeof documentLike.querySelector === "function"
        ? documentLike.querySelector(".chat_item.active")
        : null;
    const titleIdentity = chatArea.querySelector(
      ".box_hd [data-username], .title_wrap [data-username]",
    );
    const idCandidate =
      chatArea.getAttribute("data-username") ||
      chatArea.getAttribute("data-conversation-id") ||
      (titleIdentity && titleIdentity.getAttribute("data-username")) ||
      (activeConversation && activeConversation.getAttribute("data-username"));
    const attributeName = chatArea.getAttribute("data-conversation-name");
    const titleName = selectText(
      chatArea,
      [".box_hd .title_name", ".title_wrap .title_name", ".title_name"],
      MAX_LABEL_LENGTH,
      true,
    );
    const activeName = activeConversation
      ? selectText(
          activeConversation,
          [".nickname_text", ".nickname"],
          MAX_LABEL_LENGTH,
          true,
        )
      : "";
    const name = normalizeLabel(attributeName || titleName || activeName, MAX_LABEL_LENGTH);
    const id =
      normalizeLabel(idCandidate || "", MAX_LABEL_LENGTH) ||
      (name ? stableDisplayKey(name) : null);
    const groupHint =
      Boolean(id && id.startsWith("@@")) ||
      Boolean(chatArea.querySelector(".chatRoomMembers"));
    return {
      id,
      name: name || null,
      type: groupHint ? "group" : "direct",
    };
  }

  function getDirection(messageElement, messageType) {
    if (messageType === "system") return "system";
    if (containsClass(messageElement, "me") || messageElement.closest(".message.me")) {
      return "outgoing";
    }
    if (containsClass(messageElement, "you") || messageElement.closest(".message.you")) {
      return "incoming";
    }
    return "unknown";
  }

  function findMessageElement(node) {
    if (!node || node.nodeType !== 1) return null;
    if (node.matches && node.matches("[data-cm]")) return node;
    if (node.closest) {
      const closest = node.closest("[data-cm]");
      if (closest && closest.closest("#chatArea")) return closest;
    }
    if (node.querySelector) return node.querySelector("[data-cm]");
    return null;
  }

  function extractMessage(messageElement, options = {}) {
    if (!messageElement || !isActuallyVisible(messageElement, options.view)) return null;
    const cmElement = messageElement.hasAttribute("data-cm")
      ? messageElement
      : messageElement.querySelector("[data-cm]");
    if (!cmElement) return null;
    const cm = parseDataCm(cmElement.getAttribute("data-cm"));
    if (!cm) return null;

    const chatArea = messageElement.closest("#chatArea");
    if (!chatArea || chatArea.closest("#prerender")) return null;
    const conversation = getConversation(chatArea);
    if (!conversation.id) return null;
    const messageType = mapMessageType(cm.msgType);
    const direction = getDirection(messageElement, messageType);
    const text = selectText(messageElement, [
      ".js_message_plain",
      ".plain",
      ".bubble_cont",
      ".content",
    ]);
    let senderName = selectText(
      messageElement,
      [".nickname .nickname_text", ".nickname_text", ".nickname"],
      MAX_LABEL_LENGTH,
      true,
    );
    if (!senderName) {
      const avatar = messageElement.querySelector(".avatar[title]");
      if (avatar && isActuallyVisible(avatar)) {
        senderName = normalizeLabel(avatar.getAttribute("title") || "", MAX_LABEL_LENGTH);
      }
    }
    const ownTime = selectText(
      messageElement,
      [".js_message_time", ".message_time", ".time"],
      128,
      true,
    );

    const now = typeof options.now === "function" ? options.now() : new Date();
    const event = {
      provider_msgid: cm.msgId,
      provider_conversation_id: conversation.id,
      conversation_type: conversation.type,
      direction,
      message_type: messageType,
      observed_at: now.toISOString(),
    };
    if (conversation.name) event.conversation_name = conversation.name;
    if (cm.actualSender) event.sender_provider_id = cm.actualSender;
    if (senderName) event.sender_display_name = senderName;
    else if (direction === "outgoing") event.sender_display_name = "我";
    if (messageType === "text" && !text) return null;
    if (text) event.text = text;
    const displayedTime = (ownTime || normalizeText(options.displayedTime || "", 128)).replace(
      /\s+/g,
      " ",
    );
    if (displayedTime) event.displayed_time_text = displayedTime;
    return event;
  }

  function isTimeSeparator(element) {
    if (!element || element.nodeType !== 1 || !isActuallyVisible(element)) return false;
    return Boolean(
      (element.matches && element.matches(".message_system, .time")) ||
        (element.closest && element.closest(".message_system")),
    );
  }

  return {
    MAX_LABEL_LENGTH,
    MAX_TEXT_LENGTH,
    PARSER_VERSION,
    extractMessage,
    findMessageElement,
    getConversation,
    isActuallyVisible,
    isTimeSeparator,
    mapMessageType,
    normalizeLabel,
    normalizeText,
    parseDataCm,
    stableDisplayKey,
  };
});
