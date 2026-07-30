import AsyncStorage from "@react-native-async-storage/async-storage";
import {
  CryptoDigestAlgorithm,
  digestStringAsync,
} from "expo-crypto";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

import { DEFAULT_SERVER_URL, normalizeServerUrl } from "@/lib/api";
import type { ConnectionSettings } from "@/lib/types";

const SERVER_URL_KEY = "centaur-pocket.settings.server-url.v1";
const OWNER_TOKEN_KEY = "centaur-pocket.settings.owner-token.v1";

async function connectionProfileId(
  serverUrl: string,
  ownerToken: string,
): Promise<string> {
  const digest = await digestStringAsync(
    CryptoDigestAlgorithm.SHA256,
    JSON.stringify([serverUrl, ownerToken]),
  );
  return `connection-sha256-${digest}`;
}

async function readToken(): Promise<string> {
  if (Platform.OS === "web") {
    return (await AsyncStorage.getItem(OWNER_TOKEN_KEY)) ?? "";
  }
  return (await SecureStore.getItemAsync(OWNER_TOKEN_KEY)) ?? "";
}

async function writeToken(token: string): Promise<void> {
  if (Platform.OS === "web") {
    if (token) await AsyncStorage.setItem(OWNER_TOKEN_KEY, token);
    else await AsyncStorage.removeItem(OWNER_TOKEN_KEY);
    return;
  }

  if (token) {
    await SecureStore.setItemAsync(OWNER_TOKEN_KEY, token, {
      keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
    });
  } else {
    await SecureStore.deleteItemAsync(OWNER_TOKEN_KEY);
  }
}

export async function loadConnectionSettings(): Promise<ConnectionSettings> {
  const [storedServerUrl, ownerToken] = await Promise.all([
    AsyncStorage.getItem(SERVER_URL_KEY),
    readToken(),
  ]);
  const serverUrl = normalizeServerUrl(storedServerUrl ?? DEFAULT_SERVER_URL);

  return {
    serverUrl,
    ownerToken,
    profileId: await connectionProfileId(serverUrl, ownerToken),
  };
}

export async function saveConnectionSettings(
  settings: ConnectionSettings,
): Promise<ConnectionSettings> {
  const serverUrl = normalizeServerUrl(settings.serverUrl);
  const ownerToken = settings.ownerToken.trim();
  const normalized: ConnectionSettings = {
    serverUrl,
    ownerToken,
    profileId: await connectionProfileId(serverUrl, ownerToken),
  };

  await Promise.all([
    AsyncStorage.setItem(SERVER_URL_KEY, normalized.serverUrl),
    writeToken(normalized.ownerToken),
  ]);
  return normalized;
}
