import AsyncStorage from "@react-native-async-storage/async-storage";
import {
  AESEncryptionKey,
  AESSealedData,
  aesDecryptAsync,
  aesEncryptAsync,
} from "expo-crypto";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

import type { QueuedMutation } from "@/lib/mutation-queue";

const MUTATION_QUEUE_KEY = "centaur-pocket.mutation-queue.v1";
const MUTATION_ENCRYPTION_KEY =
  "centaur-pocket.mutation-queue.encryption-key.v1";

type EncryptedQueueEnvelope = {
  version: 2;
  algorithm: "AES-GCM-256";
  ciphertext: string;
};

let encryptionKeyPromise: Promise<AESEncryptionKey> | null = null;

function isEncryptedEnvelope(value: unknown): value is EncryptedQueueEnvelope {
  if (typeof value !== "object" || value === null) return false;
  const envelope = value as Partial<EncryptedQueueEnvelope>;
  return (
    envelope.version === 2 &&
    envelope.algorithm === "AES-GCM-256" &&
    typeof envelope.ciphertext === "string"
  );
}

async function encryptionKey(create: boolean): Promise<AESEncryptionKey> {
  if (encryptionKeyPromise) return encryptionKeyPromise;

  encryptionKeyPromise = (async () => {
    const encoded = await SecureStore.getItemAsync(MUTATION_ENCRYPTION_KEY);
    if (encoded) {
      return (await AESEncryptionKey.import(
        encoded,
        "base64",
      )) as AESEncryptionKey;
    }
    if (!create) {
      throw new Error("本机离线队列的安全密钥已丢失，无法解密旧操作");
    }
    const generated =
      (await AESEncryptionKey.generate(256)) as AESEncryptionKey;
    await SecureStore.setItemAsync(
      MUTATION_ENCRYPTION_KEY,
      await generated.encoded("base64"),
      { keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY },
    );
    return generated;
  })();

  try {
    return await encryptionKeyPromise;
  } catch (error) {
    encryptionKeyPromise = null;
    throw error;
  }
}

async function decryptEnvelope(
  envelope: EncryptedQueueEnvelope,
): Promise<string> {
  const key = await encryptionKey(false);
  const sealed = AESSealedData.fromCombined(envelope.ciphertext);
  const plaintext = await aesDecryptAsync(sealed, key);
  return new TextDecoder().decode(plaintext);
}

function isQueuedMutation(value: unknown): value is QueuedMutation {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Partial<QueuedMutation>;
  return (
    typeof item.id === "string" &&
    typeof item.idempotencyKey === "string" &&
    (typeof item.profileId === "string" || item.profileId === undefined) &&
    typeof item.path === "string" &&
    typeof item.method === "string" &&
    typeof item.kind === "string" &&
    typeof item.createdAt === "number" &&
    typeof item.attempts === "number" &&
    typeof item.nextAttemptAt === "number" &&
    typeof item.body === "object" &&
    item.body !== null
  );
}

export async function loadMutationQueue(): Promise<QueuedMutation[]> {
  const raw = await AsyncStorage.getItem(MUTATION_QUEUE_KEY);
  if (!raw) return [];

  try {
    const stored: unknown = JSON.parse(raw);
    const encrypted = isEncryptedEnvelope(stored);
    const value: unknown = encrypted
      ? JSON.parse(await decryptEnvelope(stored))
      : stored;
    if (!Array.isArray(value) || !value.every(isQueuedMutation)) {
      throw new Error("离线队列数据格式无效");
    }
    const queue = value
      .map((mutation): QueuedMutation => ({
        ...mutation,
        profileId: mutation.profileId || "legacy-unbound",
        state:
          !mutation.profileId
            ? "needs-attention"
            : mutation.state === "needs-attention"
              ? "needs-attention"
              : "pending",
        lastError:
          mutation.profileId
            ? mutation.lastError
            : "旧版本操作未绑定连接配置，请确认后手动处理",
      }))
      .sort((a, b) => a.createdAt - b.createdAt);
    if (!encrypted && Platform.OS !== "web") {
      await saveMutationQueue(queue);
    }
    return queue;
  } catch (error) {
    throw new Error(
      error instanceof Error
        ? `读取加密离线队列失败：${error.message}`
        : "读取加密离线队列失败",
    );
  }
}

export async function saveMutationQueue(queue: QueuedMutation[]): Promise<void> {
  const plaintext = JSON.stringify(queue);
  if (Platform.OS === "web") {
    await AsyncStorage.setItem(MUTATION_QUEUE_KEY, plaintext);
    return;
  }

  const key = await encryptionKey(true);
  const sealed = await aesEncryptAsync(
    new TextEncoder().encode(plaintext),
    key,
  );
  const envelope: EncryptedQueueEnvelope = {
    version: 2,
    algorithm: "AES-GCM-256",
    ciphertext: await sealed.combined("base64"),
  };
  await AsyncStorage.setItem(MUTATION_QUEUE_KEY, JSON.stringify(envelope));
}

export async function clearMutationQueue(): Promise<void> {
  await AsyncStorage.removeItem(MUTATION_QUEUE_KEY);
}
