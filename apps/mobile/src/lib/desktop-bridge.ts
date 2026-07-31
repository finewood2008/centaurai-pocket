import type { ApiRequest } from "@/lib/api";

export const DESKTOP_MANAGED_OWNER = "centaur-pocket-desktop-managed-owner";

export type DesktopBootstrapSettings = {
  managedByDesktop: true;
  serverUrl: string;
};

export type DesktopApiResponse = {
  ok: boolean;
  status: number | null;
  payload: unknown;
};

type CentaurPocketDesktopBridge = {
  getBootstrapSettings: () => Promise<DesktopBootstrapSettings>;
  request: (input: ApiRequest) => Promise<DesktopApiResponse>;
  selectFolder: () => Promise<string | null>;
};

declare global {
  interface Window {
    centaurPocketDesktop?: CentaurPocketDesktopBridge;
  }
}

export function isDesktopRuntime(): boolean {
  return (
    typeof window !== "undefined" &&
    window.location?.protocol === "centaur-pocket:"
  );
}

export function getDesktopBridge(): CentaurPocketDesktopBridge | null {
  if (typeof window === "undefined") return null;
  const bridge = window.centaurPocketDesktop;
  if (
    !bridge ||
    typeof bridge.getBootstrapSettings !== "function" ||
    typeof bridge.request !== "function" ||
    typeof bridge.selectFolder !== "function"
  ) {
    if (isDesktopRuntime()) {
      throw new Error("Electron 安全桥不可用，桌面操作已停止");
    }
    return null;
  }
  return bridge;
}

export async function selectDesktopFolder(): Promise<string | null> {
  const bridge = getDesktopBridge();
  if (!bridge) return null;
  const selected = await bridge.selectFolder();
  return typeof selected === "string" && selected.trim()
    ? selected.trim()
    : null;
}

export async function loadDesktopBootstrap(): Promise<DesktopBootstrapSettings | null> {
  const bridge = getDesktopBridge();
  if (!bridge) return null;
  const settings = await bridge.getBootstrapSettings();
  if (
    settings?.managedByDesktop !== true ||
    typeof settings.serverUrl !== "string" ||
    !settings.serverUrl.trim()
  ) {
    throw new Error("Electron 返回了无效的桌面托管配置");
  }
  return {
    managedByDesktop: true,
    serverUrl: settings.serverUrl.trim(),
  };
}

export function isDesktopApiResponse(
  value: unknown,
): value is DesktopApiResponse {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.ok === "boolean" &&
    (typeof candidate.status === "number" || candidate.status === null) &&
    Object.hasOwn(candidate, "payload")
  );
}
