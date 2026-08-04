import { useRef, useState } from "react";
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
import {
  openWechatWebOnDesktop,
  selectDesktopFolder,
} from "@/lib/desktop-bridge";
import { isAbsoluteServerPath } from "@/lib/source-input";
import { createIdempotencyKey } from "@/lib/mutation-queue";
import type { FolderSourceSchedule, SourcePairing } from "@/lib/types";
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
  const { api, ready, isConfigured, queueFolderSource, settings } = usePocket();
  const desktopManaged = settings.managedByDesktop === true;
  const [sourceKind, setSourceKind] = useState<"folder" | "wechat_visible_web">(
    "folder",
  );
  const [displayName, setDisplayName] = useState("");
  const [path, setPath] = useState("");
  const [schedule, setSchedule] =
    useState<FolderSourceSchedule>("manual");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [observerPairing, setObserverPairing] = useState<SourcePairing | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const observerCreationKeyRef = useRef<string | null>(null);
  const saveInFlightRef = useRef(false);

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
    if (saveInFlightRef.current) return;
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
    if (sourceKind === "folder" && !isAbsoluteServerPath(normalizedPath)) {
      setError("请填写数据中心服务器上的绝对路径");
      return;
    }

    saveInFlightRef.current = true;
    setSaving(true);
    setError(null);
    try {
      if (sourceKind === "folder") {
        await queueFolderSource({
          displayName: normalizedName,
          path: normalizedPath,
          schedule,
        });
      } else {
        observerCreationKeyRef.current ??= createIdempotencyKey(
          "wechat-observer-create",
        );
        const source = await api.createWechatObserverSource(
          normalizedName,
          observerCreationKeyRef.current,
        );
        observerCreationKeyRef.current = null;
        try {
          setObserverPairing(await api.createObserverPairing(source.id));
        } catch (caught) {
          setError(
            `观察器已创建，但配对码创建失败：${caught instanceof Error ? caught.message : "请返回来源页重试"}`,
          );
        }
      }
      setSaved(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存失败");
    } finally {
      saveInFlightRef.current = false;
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
            <Text style={styles.title}>
              {sourceKind === "folder" ? "添加文件夹来源" : "添加微信网页观察器"}
            </Text>
          </View>
          <Pill label="单人私有" tone="primary" />
        </View>

        {!saved ? (
          <View style={styles.kindPicker}>
            <SourceKindOption
              selected={sourceKind === "folder"}
              symbol="▱"
              title="文件夹"
              detail="扫描服务器或 NAS"
              onPress={() => {
                setSourceKind("folder");
                setDisplayName((current) =>
                  current === "个人微信网页版" ? "" : current,
                );
                setError(null);
              }}
            />
            <SourceKindOption
              selected={sourceKind === "wechat_visible_web"}
              symbol="微"
              title="微信网页"
              detail="观察当前可见对话"
              onPress={() => {
                setSourceKind("wechat_visible_web");
                setDisplayName((current) => current || "个人微信网页版");
                setError(null);
              }}
            />
          </View>
        ) : null}

        <View
          style={[
            styles.hero,
            sourceKind === "wechat_visible_web" && styles.observerHero,
          ]}
        >
          <View style={styles.heroIcon}>
            <Text style={styles.heroIconText}>
              {sourceKind === "folder" ? "▱" : "微"}
            </Text>
          </View>
          <View style={styles.heroCopy}>
            <Text style={styles.heroTitle}>
              {sourceKind === "folder" ? "在服务端持续归集" : "观察网页已渲染的消息"}
            </Text>
            <Text style={styles.heroText}>
              {sourceKind === "wechat_visible_web"
                ? "扩展只采集你实际打开过的对话；未打开的会话、电脑休眠和退出登录期间会形成覆盖缺口。"
                : desktopManaged
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
            <Text style={styles.savedTitle}>
              {sourceKind === "folder" ? "同步源已加入队列" : "网页观察器已创建"}
            </Text>
            <Text style={styles.savedText}>
              {sourceKind === "folder"
                ? "在线时会立即提交；断网时保留在本机，连接恢复后按原连接配置发送。"
                : observerPairing
                  ? "观察器已创建。请在浏览器扩展中输入下面的一次性配对码。"
                  : "观察器已创建。返回同步源后可以重新生成一次性配对码。"}
            </Text>
            {observerPairing ? (
              <View style={styles.pairingCard}>
                <Text style={styles.pairingLabel}>一次性配对码</Text>
                <Text
                  selectable
                  adjustsFontSizeToFit
                  minimumFontScale={0.55}
                  numberOfLines={1}
                  style={styles.pairingCode}
                >
                  {observerPairing.pairingCode}
                </Text>
                <Text selectable style={styles.sourceIdText}>
                  来源 ID：{observerPairing.sourceId}
                </Text>
                <Text style={styles.pairingHelp}>
                  点击 Firefox 工具栏的 CentaurAI 微信观察器图标，输入来源 ID
                  与配对码。配对码仅本页显示，请勿转发给他人
                  {observerPairing.expiresAt
                    ? ` · ${pairingExpiryLabel(observerPairing.expiresAt)} 失效`
                    : ""}
                </Text>
              </View>
            ) : null}
            {error ? (
              <Notice title="需要继续处理" message={error} tone="warning" />
            ) : null}
            {sourceKind === "wechat_visible_web" && desktopManaged ? (
              <Button
                label="打开微信网页版"
                icon="↗"
                tone="secondary"
                onPress={() =>
                  void openWechatWebOnDesktop().catch((caught) =>
                    setError(
                      caught instanceof Error ? caught.message : "无法打开微信网页版",
                    ),
                  )
                }
                style={styles.fullButton}
              />
            ) : null}
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
                message={
                  sourceKind === "folder"
                    ? "文件夹路径属于服务端。完成服务地址与 Owner token 配置后，才能安全绑定并保存来源。"
                    : "完成服务地址与 Owner token 配置后，才能创建只属于你的网页观察器。"
                }
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
                placeholder={
                  sourceKind === "folder"
                    ? "例如：家庭 NAS 文档"
                    : "例如：老板的微信"
                }
                placeholderTextColor={colors.textDim}
                style={styles.input}
              />
            </View>

            {sourceKind === "folder" ? (
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
            ) : (
              <Notice
                title="实验性、非完整来源"
                message="不会读取 Cookie、拦截网络请求或自动翻看历史。采集可信等级为“网页观察”，重要结论仍需证据或本人确认。"
                tone="warning"
              />
            )}

            {sourceKind === "folder" ? (
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
                          <Text style={styles.scheduleDetail}>
                            {option.detail}
                          </Text>
                        </View>
                      </Pressable>
                    );
                  })}
                </View>
              </View>
            ) : null}

            {error ? (
              <Notice title="还不能添加" message={error} tone="danger" />
            ) : null}

            <Button
              label={
                sourceKind === "folder"
                  ? "保存并开始连接"
                  : "创建观察器并生成配对码"
              }
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

function pairingExpiryLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function SourceKindOption({
  selected,
  symbol,
  title,
  detail,
  onPress,
}: {
  selected: boolean;
  symbol: string;
  title: string;
  detail: string;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="radio"
      accessibilityState={{ checked: selected }}
      onPress={onPress}
      style={[styles.kindOption, selected && styles.kindOptionSelected]}
    >
      <Text style={[styles.kindSymbol, selected && styles.kindSymbolSelected]}>
        {symbol}
      </Text>
      <View style={styles.kindCopy}>
        <Text style={[styles.kindTitle, selected && styles.kindTitleSelected]}>
          {title}
        </Text>
        <Text style={styles.kindDetail}>{detail}</Text>
      </View>
    </Pressable>
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
  kindPicker: {
    flexDirection: "row",
    gap: 10,
  },
  kindOption: {
    flex: 1,
    minHeight: 72,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    borderRadius: radii.medium,
    backgroundColor: colors.surface,
    padding: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  kindOptionSelected: {
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft,
  },
  kindSymbol: {
    color: colors.textMuted,
    fontSize: 20,
    fontWeight: "800",
  },
  kindSymbolSelected: {
    color: colors.primary,
  },
  kindCopy: {
    flex: 1,
    gap: 2,
  },
  kindTitle: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
  },
  kindTitleSelected: {
    color: colors.primaryDark,
  },
  kindDetail: {
    color: colors.textMuted,
    fontSize: 10,
    lineHeight: 14,
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
  observerHero: {
    borderColor: colors.gold,
    backgroundColor: colors.goldSoft,
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
  pairingCard: {
    alignSelf: "stretch",
    borderRadius: radii.medium,
    borderWidth: 1,
    borderColor: colors.primaryBorder,
    backgroundColor: colors.primarySoft,
    padding: 16,
    alignItems: "center",
    gap: 5,
    marginTop: 6,
  },
  pairingLabel: {
    color: colors.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 1,
  },
  pairingCode: {
    color: colors.primaryDark,
    width: "100%",
    fontSize: 18,
    lineHeight: 25,
    fontWeight: "800",
    letterSpacing: 0.4,
    textAlign: "center",
  },
  pairingHelp: {
    color: colors.textMuted,
    fontSize: 10,
    lineHeight: 15,
    textAlign: "center",
  },
  sourceIdText: {
    color: colors.textMuted,
    fontSize: 10,
    lineHeight: 15,
    textAlign: "center",
  },
  fullButton: {
    alignSelf: "stretch",
    marginTop: 12,
  },
});
