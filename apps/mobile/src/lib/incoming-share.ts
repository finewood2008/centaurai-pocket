import type { SharePayload } from "expo-sharing";

export type IncomingShareDraft = {
  text: string;
  url: string;
  mimeType: string;
  acceptedCount: number;
  unsupportedCount: number;
};

const WEB_URL = /^https?:\/\/[^\s]+$/i;

function isWebUrl(value: string): boolean {
  return WEB_URL.test(value.trim());
}

function firstWebUrl(value: string): string {
  const match = value.match(/https?:\/\/[^\s<>"']+/i);
  return match?.[0]?.replace(/[),.;!?，。；！？]+$/, "") ?? "";
}

export function incomingShareDraft(
  payloads: SharePayload[],
): IncomingShareDraft {
  const textParts: string[] = [];
  let url = "";
  let mimeType = "text/plain";
  let acceptedCount = 0;
  let unsupportedCount = 0;

  for (const payload of payloads) {
    const value = payload.value.trim();
    if (!value) continue;

    if (payload.shareType === "url") {
      if (!url && isWebUrl(value)) {
        url = value;
        mimeType = payload.mimeType || "text/uri-list";
        acceptedCount += 1;
      } else if (isWebUrl(value)) {
        textParts.push(value);
        acceptedCount += 1;
      } else {
        unsupportedCount += 1;
      }
      continue;
    }

    if (payload.shareType === "text") {
      textParts.push(value);
      if (!url) url = firstWebUrl(value);
      mimeType = payload.mimeType || mimeType;
      acceptedCount += 1;
      continue;
    }

    unsupportedCount += 1;
  }

  return {
    text: textParts.join("\n\n"),
    url,
    mimeType,
    acceptedCount,
    unsupportedCount,
  };
}
