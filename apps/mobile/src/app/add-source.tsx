import { useState } from "react";
import { useRouter } from "expo-router";
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
import { selectDesktopFolder } from "@/lib/desktop-bridge";
import { isAbsoluteServerPath } from "@/lib/source-input";
import type { FolderSourceSchedule } from "@/lib/types";
import { colors, radii } from "@/theme/colors";

const schedules: {
  value: FolderSourceSchedule;
  label: string;
  detail: string;
}[] = [
  { value: "manual", label: "手动", detail: "需要时同步" },
  { value: "hourly", label: "每小时", detail: "持续更新" },
  { value: "daily", label: "每天", detail: "每日归集" },
];

export default function AddSourceScreen() {
  const router = useRouter();
  const { ready, isConfigured, queueFolderSource, settings } = usePocket();
  const desktopManaged = settings.managedByDesktop === true;
  const [displayName, setDisplayName] = useState("");
  const [path, setPath] = useState("");
  const [schedule, setSchedule] =
    useState<FolderSourceSchedule>("manual");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function close() {
    if (router.canGoBack()) router.back();
    else router.replace("/sources");
  }

  async function chooseDesktopFolder() {
    setError(null);
    try {
      const selected = await selectDesktopFolder();
      if (!selected) return;
      setPath(selected);
      if (!displayName.trim()) {
        const folderName = selected
          .replace(/[\\/]+$/, "")
          .split(/[\\/]/)
          .pop();
        if (folderName) setDisplayName(folderName);
      }
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "无法打开目录选择器",
      );
    }
  }

  async function save() {
    if (!isConfigured) {
      setError("请先完成服务地址与 Owner token 配置");
      return;
    }
    const normalizedName = displayName.trim();
    const normalizedPath = path.trim();
    if (!normalizedName) {
      setError("请填写来源名称");
      return;
    }
    if (!isAbsoluteServerPath(normalizedPath)) {
      setError("请填写数据中心服务器上的绝对路径");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await queueFolderSource({
        displayName: normalizedName,
        path: normalizedPath,
        schedule,
      });
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
      <Screen>
        <View style={styles.header}>
          <Pressable
            accessibilityRole="button"
            onPress={close}
            style={styles.closeButton}
          >
            <Text style={styles.closeText}>×</Text>
          </Pressable>
          <View style={styles.headerCopy}>
            <Text style={styles.eyebrow}>NEW DATA SOURCE</Text>
            <Text style={styles.title}>添加文件夹来源</Text>
          </View>
          <Pill label="单人私有" tone="primary" />
        </View>

        <View style={styles.hero}>
          <View style={styles.heroIcon}>
            <Text style={styles.heroIconText}>▱</Text>
          </View>
          <View style={styles.heroCopy}>
            <Text style={styles.heroTitle}>在服务端持续归集</Text>
            <Text style={styles.heroText}>
              {desktopManaged
                ? "选择一个允许 Pocket 只读扫描的本机文件夹。"
                : "这里填写的是运行 Pocket 服务的电脑或 NAS 路径，不是手机本地目录。"}
            </Text>
          </View>
        </View>

        {saved ? (
          <View style={styles.savedCard}>
            <View style={styles.savedIcon}>
              <Text style={styles.savedIconText}>✓</Text>
            </View>
            <Text style={styles.savedTitle}>同步源已加入队列</Text>
            <Text style={styles.savedText}>
              在线时会立即提交；断网时保留在本机，连接恢复后按原连接配置发送。
            </Text>
            <Button
              label="返回同步源"
              onPress={close}
              style={styles.fullButton}
            />
          </View>
        ) : (
          <View style={styles.form}>
            {!isConfigured ? (
              <Notice
                title="先连接你的私人数据中心"
                message="文件夹路径属于服务端。完成服务地址与 Owner token 配置后，才能安全绑定并保存来源。"
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
              <Text style={styles.label}>来源名称</Text>
              <TextInput
                value={displayName}
                onChangeText={setDisplayName}
                maxLength={500}
                placeholder="例如：家庭 NAS 文档"
                placeholderTextColor={colors.textDim}
                style={styles.input}
              />
            </View>

            <View style={styles.field}>
              <Text style={styles.label}>服务端绝对路径</Text>
              <View style={styles.pathRow}>
                <TextInput
                  value={path}
                  onChangeText={setPath}
                  editable={!desktopManaged}
                  autoCapitalize="none"
                  autoCorrect={false}
                  placeholder={
                    desktopManaged
                      ? "点击右侧按钮授权文件夹"
                      : "/srv/personal-docs"
                  }
                  placeholderTextColor={colors.textDim}
                  style={[styles.input, styles.pathInput]}
                />
                {desktopManaged ? (
                  <Button
                    compact
                    tone="secondary"
                    label="选择文件夹"
                    onPress={() => void chooseDesktopFolder()}
                  />
                ) : null}
              </View>
              <Text style={styles.help}>
                {desktopManaged
                  ? "只有通过系统目录选择器明确授权的路径才能创建同步源。"
                  : "支持 Linux/macOS 绝对路径、Windows 盘符路径或 UNC 网络路径。"}
              </Text>
            </View>

            <View style={styles.field}>
              <Text style={styles.label}>自动同步频率</Text>
              <View style={styles.scheduleList}>
                {schedules.map((option) => {
                  const selected = schedule === option.value;
                  return (
                    <Pressable
                      key={option.value}
                      accessibilityRole="radio"
                      accessibilityState={{ checked: selected }}
                      onPress={() => setSchedule(option.value)}
                      style={[
                        styles.schedule,
                        selected && styles.scheduleSelected,
                      ]}
                    >
                      <View
                        style={[
                          styles.radio,
                          selected && styles.radioSelected,
                        ]}
                      >
                        {selected ? <View style={styles.radioDot} /> : null}
                      </View>
                      <View style={styles.scheduleCopy}>
                        <Text
                          style={[
                            styles.scheduleLabel,
                            selected && styles.scheduleLabelSelected,
                          ]}
                        >
                          {option.label}
                        </Text>
                        <Text style={styles.scheduleDetail}>{option.detail}</Text>
                      </View>
                    </Pressable>
                  );
                })}
              </View>
            </View>

            {error ? (
              <Notice title="还不能添加" message={error} tone="danger" />
            ) : null}

            <Button
              label="保存并开始连接"
              icon="+"
              loading={saving}
              disabled={!ready || !isConfigured}
              onPress={() => void save()}
            />
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
  header: {
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
  headerCopy: {
    flex: 1,
    gap: 3,
  },
  eyebrow: {
    color: colors.primary,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.6,
  },
  title: {
    color: colors.text,
    fontSize: 17,
    fontWeight: "700",
  },
  hero: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft,
    padding: 18,
    flexDirection: "row",
    gap: 14,
  },
  heroIcon: {
    width: 48,
    height: 48,
    borderRadius: 14,
    backgroundColor: colors.goldSoft,
    alignItems: "center",
    justifyContent: "center",
  },
  heroIconText: {
    color: colors.primary,
    fontSize: 25,
    fontWeight: "700",
  },
  heroCopy: {
    flex: 1,
    gap: 5,
  },
  heroTitle: {
    color: colors.text,
    fontSize: 16,
    fontWeight: "700",
  },
  heroText: {
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
    gap: 19,
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
    minHeight: 49,
    borderRadius: 13,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.backgroundRaised,
    color: colors.text,
    paddingHorizontal: 13,
    paddingVertical: 12,
    fontSize: 13,
  },
  pathRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 9,
  },
  pathInput: {
    flex: 1,
  },
  help: {
    color: colors.textDim,
    fontSize: 10,
    lineHeight: 16,
  },
  scheduleList: {
    gap: 9,
  },
  schedule: {
    minHeight: 58,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.backgroundRaised,
    paddingHorizontal: 14,
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
  },
  scheduleSelected: {
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft,
  },
  radio: {
    width: 18,
    height: 18,
    borderRadius: 9,
    borderWidth: 1,
    borderColor: colors.textDim,
    alignItems: "center",
    justifyContent: "center",
  },
  radioSelected: {
    borderColor: colors.primary,
  },
  radioDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.primary,
  },
  scheduleCopy: {
    flex: 1,
    gap: 2,
  },
  scheduleLabel: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
  },
  scheduleLabelSelected: {
    color: colors.primary,
  },
  scheduleDetail: {
    color: colors.textMuted,
    fontSize: 10,
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
    fontSize: 21,
    fontWeight: "800",
  },
  savedText: {
    color: colors.textMuted,
    fontSize: 13,
    lineHeight: 20,
    textAlign: "center",
    maxWidth: 310,
  },
  fullButton: {
    alignSelf: "stretch",
    marginTop: 12,
  },
});
