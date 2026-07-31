import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useIncomingShare,
  type SharePayload,
} from "expo-sharing";
import { useLocalSearchParams, useRouter } from "expo-router";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { Screen } from "@/components/screen";
import { Button, Notice, Pill } from "@/components/ui";
import { usePocket } from "@/context/pocket-context";
import { incomingShareDraft } from "@/lib/incoming-share";
import { colors, radii } from "@/theme/colors";

function firstParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

type IncomingShareSnapshot = {
  payloads: SharePayload[];
  clear: () => void;
  isResolving: boolean;
  error: string | null;
};

function NativeIncomingShareBridge({
  onChange,
}: {
  onChange: (snapshot: IncomingShareSnapshot) => void;
}) {
  const {
    sharedPayloads,
    clearSharedPayloads,
    refreshSharePayloads,
    isResolving,
    error,
  } = useIncomingShare();
  const clear = useCallback(() => {
    clearSharedPayloads();
    refreshSharePayloads();
  }, [clearSharedPayloads, refreshSharePayloads]);

  useEffect(() => {
    onChange({
      payloads: sharedPayloads,
      clear,
      isResolving,
      error: error?.message ?? null,
    });
  }, [
    clear,
    error,
    isResolving,
    onChange,
    sharedPayloads,
  ]);

  return null;
}

export default function HandleShareScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{
    title?: string | string[];
    text?: string | string[];
    url?: string | string[];
    mimeType?: string | string[];
  }>();
  const initial = useMemo(
    () => ({
      title: firstParam(params.title),
      text: firstParam(params.text),
      url: firstParam(params.url),
      mimeType: firstParam(params.mimeType) || "text/plain",
    }),
    [params.mimeType, params.text, params.title, params.url],
  );
  const { ready, isConfigured, queueCapture, mutations } = usePocket();
  const [title, setTitle] = useState(initial.title);
  const [text, setText] = useState(initial.text);
  const [url, setUrl] = useState(initial.url);
  const [mimeType, setMimeType] = useState(initial.mimeType);
  const [unsupportedCount, setUnsupportedCount] = useState(0);
  const [shareIsResolving, setShareIsResolving] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const clearSharedPayloadsRef = useRef<() => void>(() => undefined);
  const appliedPayloadSignatureRef = useRef("");

  const receiveNativeShare = useCallback(
    (snapshot: IncomingShareSnapshot) => {
      clearSharedPayloadsRef.current = snapshot.clear;
      setShareIsResolving(snapshot.isResolving);
      setShareError(snapshot.error);

      const signature = JSON.stringify(snapshot.payloads);
      if (!signature || signature === appliedPayloadSignatureRef.current) return;
      appliedPayloadSignatureRef.current = signature;

      const draft = incomingShareDraft(snapshot.payloads);
      setUnsupportedCount(draft.unsupportedCount);
      if (draft.acceptedCount === 0) return;

      setText((current) => current.trim() || draft.text);
      setUrl((current) => current.trim() || draft.url);
      setMimeType(draft.mimeType);
      setTitle(
        (current) =>
          current.trim() ||
          (draft.url && !draft.text ? "来自手机的链接" : "来自手机的分享"),
      );
    },
    [],
  );

  function close() {
    clearSharedPayloadsRef.current();
    if (router.canGoBack()) router.back();
    else router.replace("/");
  }

  async function saveCapture() {
    if (!isConfigured) {
      setError("请先完成服务地址与 Owner token 配置");
      return;
    }
    if (!text.trim() && !url.trim()) {
      setError("请填写正文或来源链接");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await queueCapture({
        title: title.trim() || "来自手机的碎片",
        text: text.trim(),
        url: url.trim(),
        mimeType,
      });
      clearSharedPayloadsRef.current();
      setSaved(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.keyboard}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      {Platform.OS === "web" ? null : (
        <NativeIncomingShareBridge onChange={receiveNativeShare} />
      )}
      <Screen>
        <View style={styles.modalHeader}>
          <Pressable
            accessibilityRole="button"
            onPress={close}
            style={styles.closeButton}
          >
            <Text style={styles.closeText}>×</Text>
          </Pressable>
          <View style={styles.modalTitleWrap}>
            <Text style={styles.eyebrow}>QUICK CAPTURE</Text>
            <Text style={styles.modalTitle}>保存到随身数据中心</Text>
          </View>
          <Pill label="私密" tone="primary" />
        </View>

        <View style={styles.captureHero}>
          <View style={styles.captureIcon}>
            <Text style={styles.captureIconText}>↘</Text>
          </View>
          <View style={styles.captureHeroCopy}>
            <Text style={styles.captureHeroTitle}>先收下，再治理</Text>
            <Text style={styles.captureHeroText}>
              系统分享的文字和网页链接会在这里确认；断网时先保存在本机队列。
            </Text>
          </View>
        </View>

        {unsupportedCount > 0 ? (
          <Notice
            title="这个分享里含有暂不支持的内容"
            message="当前版本只接收文字和网页链接；文件内容尚未上传，也不会被误存为文本。"
            tone="warning"
          />
        ) : null}

        {shareError && (text.trim() || url.trim()) ? (
          <Notice
            title="已读取原始分享内容"
            message="链接扩展信息解析失败，但原始文字或网址仍可正常保存。"
            tone="warning"
          />
        ) : null}

        {saved ? (
          <View style={styles.savedCard}>
            <View style={styles.savedIcon}>
              <Text style={styles.savedIconText}>✓</Text>
            </View>
            <Text style={styles.savedTitle}>已经收下</Text>
            <Text style={styles.savedText}>
              内容已进入写入队列。你可以稍后在治理收件箱中确认分类和质量建议。
            </Text>
            <Pill label={`${mutations.length} 项等待同步`} tone="warning" />
            <Button label="完成" onPress={close} style={styles.doneButton} />
          </View>
        ) : (
          <View style={styles.form}>
            {!isConfigured ? (
              <Notice
                title="先连接你的私人数据中心"
                message="分享内容会保留在当前系统分享中；完成服务地址与 Owner token 配置后，再返回这里保存。"
                tone="warning"
                action={
                  <Button
                    compact
                    tone="ghost"
                    label="去设置"
                    onPress={() => router.push("/settings")}
                  />
                }
              />
            ) : null}

            <View style={styles.field}>
              <Text style={styles.label}>标题</Text>
              <TextInput
                value={title}
                onChangeText={setTitle}
                placeholder="给这个碎片一个容易找到的名字"
                placeholderTextColor={colors.textDim}
                style={styles.input}
              />
            </View>

            <View style={styles.field}>
              <Text style={styles.label}>内容</Text>
              <TextInput
                value={text}
                onChangeText={setText}
                placeholder="文字、想法、备注或分享说明…"
                placeholderTextColor={colors.textDim}
                multiline
                textAlignVertical="top"
                style={[styles.input, styles.textarea]}
              />
            </View>

            <View style={styles.field}>
              <Text style={styles.label}>来源链接</Text>
              <TextInput
                value={url}
                onChangeText={setUrl}
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="url"
                placeholder="https://"
                placeholderTextColor={colors.textDim}
                style={styles.input}
              />
            </View>

            {error ? (
              <Notice title="还不能保存" message={error} tone="danger" />
            ) : null}

            <Button
              label={shareIsResolving ? "保存原始分享内容" : "保存到私人收件箱"}
              icon="✓"
              loading={saving}
              disabled={
                !ready ||
                !isConfigured ||
                (!text.trim() && !url.trim())
              }
              onPress={() => void saveCapture()}
            />
            <Text style={styles.privacy}>
              {!ready
                ? "正在读取本机安全设置…"
                : isConfigured
                ? "仅提交到你在设置页指定的 CentaurAI Pocket 服务"
                : "尚未配对时不会创建队列，也不会显示保存成功"}
            </Text>
          </View>
        )}
      </Screen>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  keyboard: {
    flex: 1,
    backgroundColor: colors.background,
  },
  modalHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  closeButton: {
    width: 40,
    height: 40,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    alignItems: "center",
    justifyContent: "center",
  },
  closeText: {
    color: colors.text,
    fontSize: 24,
    lineHeight: 26,
    fontWeight: "300",
  },
  modalTitleWrap: {
    flex: 1,
    gap: 3,
  },
  eyebrow: {
    color: colors.primary,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.6,
  },
  modalTitle: {
    color: colors.text,
    fontSize: 16,
    fontWeight: "700",
  },
  captureHero: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft,
    padding: 18,
    flexDirection: "row",
    gap: 14,
  },
  captureIcon: {
    width: 48,
    height: 48,
    borderRadius: 14,
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    justifyContent: "center",
  },
  captureIconText: {
    color: colors.primary,
    fontSize: 25,
    fontWeight: "700",
  },
  captureHeroCopy: {
    flex: 1,
    gap: 5,
  },
  captureHeroTitle: {
    color: colors.text,
    fontSize: 16,
    fontWeight: "700",
  },
  captureHeroText: {
    color: colors.textMuted,
    fontSize: 12,
    lineHeight: 18,
  },
  form: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 18,
    gap: 17,
  },
  field: {
    gap: 8,
  },
  label: {
    color: colors.text,
    fontSize: 12,
    fontWeight: "700",
  },
  input: {
    minHeight: 48,
    borderRadius: 13,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.backgroundRaised,
    color: colors.text,
    paddingHorizontal: 13,
    paddingVertical: 12,
    fontSize: 13,
  },
  textarea: {
    minHeight: 150,
    lineHeight: 20,
  },
  privacy: {
    color: colors.textDim,
    fontSize: 10,
    textAlign: "center",
  },
  savedCard: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.success,
    backgroundColor: colors.surface,
    padding: 26,
    alignItems: "center",
    gap: 10,
  },
  savedIcon: {
    width: 64,
    height: 64,
    borderRadius: 32,
    backgroundColor: colors.primarySoft,
    borderWidth: 1,
    borderColor: colors.success,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 4,
  },
  savedIconText: {
    color: colors.primary,
    fontSize: 30,
    fontWeight: "800",
  },
  savedTitle: {
    color: colors.text,
    fontSize: 22,
    fontWeight: "800",
  },
  savedText: {
    color: colors.textMuted,
    fontSize: 13,
    lineHeight: 20,
    textAlign: "center",
    maxWidth: 310,
  },
  doneButton: {
    alignSelf: "stretch",
    marginTop: 12,
  },
});
