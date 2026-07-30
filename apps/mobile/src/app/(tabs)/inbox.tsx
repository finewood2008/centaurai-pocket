import { useCallback, useMemo, useRef, useState } from "react";
import { useFocusEffect } from "expo-router";
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { BrandHeader, Screen } from "@/components/screen";
import {
  Button,
  EmptyState,
  LoadingCards,
  Notice,
  Pill,
  SectionHeader,
} from "@/components/ui";
import { usePocket } from "@/context/pocket-context";
import { useRemoteResource } from "@/hooks/use-remote-resource";
import { demoTasks } from "@/lib/demo";
import { formatRelativeTime } from "@/lib/format";
import {
  governanceApplyPatch,
  governanceTagsError,
  parseGovernanceTags,
} from "@/lib/governance-input";
import { pendingTaskAction } from "@/lib/mutation-queue";
import type {
  GovernancePatch,
  GovernanceTask,
  GovernanceTaskKind,
  TaskAction,
} from "@/lib/types";
import { colors, radii, shadows } from "@/theme/colors";

type Filter = "all" | GovernanceTaskKind;

const filters: { key: Filter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "deduplicate", label: "去重" },
  { key: "classify", label: "分类" },
  { key: "quality", label: "质量" },
  { key: "normalize", label: "规范化" },
  { key: "deletion", label: "删除" },
];

const kindMeta: Record<
  GovernanceTaskKind,
  {
    label: string;
    symbol: string;
    tone: "primary" | "warning" | "danger" | "violet" | "neutral";
  }
> = {
  deduplicate: { label: "疑似重复", symbol: "⌘", tone: "warning" },
  classify: { label: "智能分类", symbol: "#", tone: "violet" },
  quality: { label: "质量补全", symbol: "+", tone: "primary" },
  normalize: { label: "格式规范", symbol: "≡", tone: "primary" },
  deletion: { label: "源文件已删除", symbol: "⌫", tone: "danger" },
  review: { label: "需要确认", symbol: "?", tone: "warning" },
  unknown: { label: "治理建议", symbol: "◇", tone: "neutral" },
};

export default function InboxScreen() {
  const { api, mutations, queueTaskAction } = usePocket();
  const [filter, setFilter] = useState<Filter>("all");
  const [hidden, setHidden] = useState<Record<string, TaskAction>>({});
  const [lastDecision, setLastDecision] = useState<{
    task: GovernanceTask;
    action: Exclude<TaskAction, "undo">;
    demo: boolean;
  } | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);
  const loadTasks = useCallback(() => api.tasks(), [api]);
  const resource = useRemoteResource(loadTasks, demoTasks);
  const reloadTasks = resource.reload;
  const hasFocusedRef = useRef(false);
  useFocusEffect(
    useCallback(() => {
      if (!hasFocusedRef.current) {
        hasFocusedRef.current = true;
        return;
      }
      void reloadTasks();
    }, [reloadTasks]),
  );

  const visibleTasks = useMemo(
    () =>
      resource.data.filter((task) => {
        if (filter !== "all" && task.kind !== filter) return false;
        const queuedAction = pendingTaskAction(mutations, task.id);
        if (queuedAction === "apply" || queuedAction === "skip") return false;
        return !hidden[task.id];
      }),
    [filter, hidden, mutations, resource.data],
  );

  async function decide(
    task: GovernanceTask,
    action: Exclude<TaskAction, "undo">,
    patch?: GovernancePatch,
  ) {
    const previousDecision = lastDecision;
    setOperationError(null);
    setHidden((current) => ({ ...current, [task.id]: action }));
    setLastDecision({ task, action, demo: resource.isDemo });
    if (resource.isDemo) return;

    try {
      await queueTaskAction(task.id, action, patch);
    } catch (error) {
      setHidden((current) => {
        if (current[task.id] !== action) return current;
        const next = { ...current };
        delete next[task.id];
        return next;
      });
      setLastDecision((current) =>
        current?.task.id === task.id && current.action === action
          ? previousDecision
          : current,
      );
      setOperationError(
        error instanceof Error
          ? error.message
          : "操作没有写入本机离线队列",
      );
    }
  }

  async function undo() {
    if (!lastDecision || undoing) return;
    const decision = lastDecision;
    const { task, action, demo } = decision;
    setOperationError(null);
    setHidden((current) => {
      const next = { ...current };
      delete next[task.id];
      return next;
    });
    setLastDecision(null);
    if (demo) return;

    setUndoing(true);
    try {
      await queueTaskAction(task.id, "undo");
    } catch (error) {
      setHidden((current) => ({ ...current, [task.id]: action }));
      setLastDecision((current) => current ?? decision);
      setOperationError(
        error instanceof Error
          ? `撤销没有保存：${error.message}`
          : "撤销没有写入本机离线队列",
      );
    } finally {
      setUndoing(false);
    }
  }

  return (
    <Screen
      refreshing={resource.refreshing}
      onRefresh={() => void reloadTasks()}
      footer={
        lastDecision ? (
          <View style={styles.undoToast}>
            <View style={styles.undoCopy}>
              <Text style={styles.undoTitle}>
                {lastDecision.action === "apply"
                  ? lastDecision.task.kind === "deletion"
                    ? "已确认归档"
                    : "已接受"
                  : lastDecision.task.kind === "deletion"
                    ? "已继续保留"
                    : "已跳过"}
              </Text>
              <Text style={styles.undoSubtitle} numberOfLines={1}>
                {lastDecision.task.title}
              </Text>
            </View>
            <Button
              compact
              tone="ghost"
              label="撤销"
              loading={undoing}
              onPress={() => void undo()}
            />
          </View>
        ) : null
      }
    >
      <BrandHeader
        eyebrow="GOVERNANCE INBOX"
        title="治理收件箱"
        subtitle="一条一条确认，碎片时间也能把数据整理好"
        trailing={<Pill label={`${visibleTasks.length} 待处理`} tone="warning" />}
      />

      {resource.isDemo ? (
        <Notice
          title="离线预览"
          message={`${resource.error ?? "未连接到私人数据中心"}。演示建议可操作但不会写入队列。`}
          tone="warning"
        />
      ) : null}

      {operationError ? (
        <Notice
          title="操作没有保存"
          message={`${operationError}。卡片已恢复，请确认本机存储或连接设置后重试。`}
          tone="danger"
        />
      ) : null}

      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.filters}
      >
        {filters.map((item) => {
          const selected = item.key === filter;
          return (
            <Pressable
              key={item.key}
              accessibilityRole="button"
              onPress={() => setFilter(item.key)}
              style={[styles.filter, selected && styles.filterSelected]}
            >
              <Text
                style={[styles.filterText, selected && styles.filterTextSelected]}
              >
                {item.label}
              </Text>
            </Pressable>
          );
        })}
      </ScrollView>

      {resource.loading ? (
        <LoadingCards count={3} />
      ) : visibleTasks.length === 0 ? (
        <EmptyState
          title={filter === "all" ? "收件箱清空了" : "这一类暂时没有任务"}
          message={
            filter === "all"
              ? "新数据同步后，系统会把需要你确认的建议放到这里。"
              : "切换“全部”继续处理其他治理建议。"
          }
          action={
            filter !== "all" ? (
              <Button label="查看全部" tone="secondary" onPress={() => setFilter("all")} />
            ) : undefined
          }
        />
      ) : (
        <View style={styles.taskList}>
          <SectionHeader
            title="建议你确认"
            caption="确认数据进入 Ready 区、继续保留或归档"
          />
          {visibleTasks.map((task, index) => (
            <TaskCard
              key={task.id}
              task={task}
              featured={index === 0}
              onApply={(patch) => void decide(task, "apply", patch)}
              onSkip={() => void decide(task, "skip")}
            />
          ))}
        </View>
      )}
    </Screen>
  );
}

function TaskCard({
  task,
  featured,
  onApply,
  onSkip,
}: {
  task: GovernanceTask;
  featured: boolean;
  onApply: (patch: GovernancePatch) => void;
  onSkip: () => void;
}) {
  const meta = kindMeta[task.kind];
  const isDeletion = task.kind === "deletion";
  const [editing, setEditing] = useState(false);
  const [editedTitle, setEditedTitle] = useState(
    task.suggestedTitle || task.title,
  );
  const [editedTags, setEditedTags] = useState(
    task.suggestedTags.join("，"),
  );
  const [editedCategory, setEditedCategory] = useState(
    task.suggestedCategory || task.currentCategory,
  );
  const parsedTags = parseGovernanceTags(editedTags);
  const tagsError = governanceTagsError(parsedTags);

  function applyEditedSuggestion() {
    onApply(
      governanceApplyPatch(task.kind, {
        title: editedTitle,
        tags: parsedTags,
        category: editedCategory,
      }),
    );
  }

  return (
    <View
      style={[
        styles.taskCard,
        featured && styles.featuredCard,
        isDeletion && styles.deletionCard,
      ]}
    >
      <View style={styles.taskTop}>
        <View
          style={[
            styles.kindIcon,
            featured && styles.kindIconFeatured,
            isDeletion && styles.kindIconDeletion,
          ]}
        >
          <Text
            style={[
              styles.kindSymbol,
              featured && styles.kindSymbolFeatured,
              isDeletion && styles.kindSymbolDeletion,
            ]}
          >
            {meta.symbol}
          </Text>
        </View>
        <View style={styles.taskMeta}>
          <View style={styles.taskMetaLine}>
            <Pill label={meta.label} tone={meta.tone} />
            {task.confidence !== null ? (
              <Text style={styles.confidence}>
                {Math.round(task.confidence * 100)}% 置信
              </Text>
            ) : null}
          </View>
          <Text style={styles.taskSource}>
            {task.sourceName} · {formatRelativeTime(task.createdAt)}
          </Text>
        </View>
      </View>

      <View style={styles.taskCopy}>
        <Text style={styles.taskTitle}>{task.title}</Text>
        {task.preview ? <Text style={styles.taskPreview}>{task.preview}</Text> : null}
      </View>

      {isDeletion ? (
        <View style={styles.deletionNotice}>
          <Text style={styles.deletionNoticeTitle}>确认源文件删除结果</Text>
          <Text style={styles.deletionNoticeText}>
            归档后这条数据将从个人 Agent 查询结果中隐藏；选择继续保留，则现有
            内容维持当前可见性，未确认内容仍不会开放给 Agent。
          </Text>
          {task.reason ? (
            <Text style={styles.reason}>{task.reason}</Text>
          ) : null}
        </View>
      ) : task.suggestion || task.reason ? (
        <View style={styles.suggestion}>
          <View style={styles.suggestionHeader}>
            <Text style={styles.suggestionLabel}>系统建议</Text>
            <Pressable
              accessibilityRole="button"
              onPress={() => setEditing((current) => !current)}
              style={styles.editToggle}
            >
              <Text style={styles.editToggleText}>
                {editing ? "收起编辑" : "编辑标题、分类与标签"}
              </Text>
            </Pressable>
          </View>
          {editing ? (
            <View style={styles.editor}>
              <View style={styles.editorField}>
                <Text style={styles.editorLabel}>确认后的标题</Text>
                <TextInput
                  value={editedTitle}
                  onChangeText={setEditedTitle}
                  maxLength={500}
                  placeholder="输入标题"
                  placeholderTextColor={colors.textDim}
                  style={styles.editorInput}
                />
              </View>
              <View style={styles.editorField}>
                <Text style={styles.editorLabel}>分类</Text>
                <TextInput
                  value={editedCategory}
                  onChangeText={setEditedCategory}
                  maxLength={120}
                  placeholder="例如：家庭财务（留空可清除分类）"
                  placeholderTextColor={colors.textDim}
                  style={styles.editorInput}
                />
              </View>
              <View style={styles.editorField}>
                <Text style={styles.editorLabel}>标签</Text>
                <TextInput
                  value={editedTags}
                  onChangeText={setEditedTags}
                  placeholder="用逗号分隔，例如：家庭，保险"
                  placeholderTextColor={colors.textDim}
                  style={styles.editorInput}
                />
                {tagsError ? (
                  <Text style={styles.editorError}>{tagsError}</Text>
                ) : (
                  <Text style={styles.editorHelp}>
                    将写入 {parsedTags.length} 个标签
                  </Text>
                )}
              </View>
            </View>
          ) : (
            <>
              <Text style={styles.suggestionText}>
                {task.suggestion || task.reason}
              </Text>
              {task.suggestedTitle ? (
                <Text style={styles.suggestionPreview}>
                  标题：{task.suggestedTitle}
                </Text>
              ) : null}
              {task.suggestedTags.length ? (
                <Text style={styles.suggestionPreview}>
                  标签：{task.suggestedTags.join(" · ")}
                </Text>
              ) : null}
              {task.suggestedCategory ? (
                <Text style={styles.suggestionPreview}>
                  分类：{task.suggestedCategory}
                </Text>
              ) : null}
              {task.suggestion && task.reason ? (
                <Text style={styles.reason}>{task.reason}</Text>
              ) : null}
            </>
          )}
        </View>
      ) : null}

      <View style={styles.actions}>
        <Button
          style={styles.skipButton}
          tone="secondary"
          label={isDeletion ? "继续保留" : "跳过"}
          icon="×"
          onPress={onSkip}
        />
        <Button
          style={styles.applyButton}
          tone={isDeletion ? "danger" : "primary"}
          label={
            isDeletion
              ? "确认归档"
              : editing
                ? "保存并接受"
                : "接受建议"
          }
          icon={isDeletion ? "⌫" : "✓"}
          disabled={
            !isDeletion &&
            (!editedTitle.trim() || Boolean(tagsError))
          }
          onPress={applyEditedSuggestion}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  filters: {
    gap: 8,
    paddingRight: 20,
  },
  filter: {
    borderRadius: radii.pill,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    paddingHorizontal: 15,
    paddingVertical: 9,
  },
  filterSelected: {
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft,
  },
  filterText: {
    color: colors.textMuted,
    fontSize: 12,
    fontWeight: "700",
  },
  filterTextSelected: {
    color: colors.primary,
  },
  taskList: {
    gap: 14,
  },
  taskCard: {
    borderRadius: 24,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    padding: 18,
    gap: 17,
    ...shadows.card,
  },
  featuredCard: {
    borderColor: "#285651",
    backgroundColor: "#101C27",
  },
  deletionCard: {
    borderColor: "#5F2F3C",
    backgroundColor: "#1E151D",
  },
  taskTop: {
    flexDirection: "row",
    gap: 12,
    alignItems: "center",
  },
  kindIcon: {
    width: 42,
    height: 42,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.surfaceHighlight,
  },
  kindIconFeatured: {
    backgroundColor: colors.primarySoft,
  },
  kindIconDeletion: {
    backgroundColor: colors.dangerSoft,
  },
  kindSymbol: {
    color: colors.textMuted,
    fontSize: 22,
    fontWeight: "800",
  },
  kindSymbolFeatured: {
    color: colors.primary,
  },
  kindSymbolDeletion: {
    color: colors.danger,
  },
  taskMeta: {
    flex: 1,
    gap: 6,
  },
  taskMetaLine: {
    flexDirection: "row",
    alignItems: "center",
    gap: 9,
  },
  confidence: {
    color: colors.textDim,
    fontSize: 10,
    fontWeight: "700",
  },
  taskSource: {
    color: colors.textMuted,
    fontSize: 11,
  },
  taskCopy: {
    gap: 7,
  },
  taskTitle: {
    color: colors.text,
    fontSize: 18,
    fontWeight: "800",
    lineHeight: 25,
  },
  taskPreview: {
    color: colors.textMuted,
    fontSize: 13,
    lineHeight: 20,
  },
  suggestion: {
    borderRadius: radii.medium,
    backgroundColor: colors.backgroundRaised,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    padding: 13,
    gap: 5,
  },
  suggestionLabel: {
    color: colors.primary,
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 1,
  },
  suggestionHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
  editToggle: {
    paddingHorizontal: 9,
    paddingVertical: 5,
    borderRadius: radii.pill,
    backgroundColor: colors.primarySoft,
  },
  editToggleText: {
    color: colors.primary,
    fontSize: 10,
    fontWeight: "700",
  },
  suggestionText: {
    color: colors.text,
    fontSize: 13,
    lineHeight: 19,
    fontWeight: "600",
  },
  suggestionPreview: {
    color: colors.textMuted,
    fontSize: 11,
    lineHeight: 17,
  },
  deletionNotice: {
    borderRadius: radii.medium,
    backgroundColor: colors.dangerSoft,
    borderWidth: 1,
    borderColor: "#633142",
    padding: 13,
    gap: 6,
  },
  deletionNoticeTitle: {
    color: colors.danger,
    fontSize: 11,
    fontWeight: "800",
  },
  deletionNoticeText: {
    color: colors.text,
    fontSize: 12,
    lineHeight: 18,
  },
  editor: {
    gap: 12,
    marginTop: 4,
  },
  editorField: {
    gap: 6,
  },
  editorLabel: {
    color: colors.textMuted,
    fontSize: 10,
    fontWeight: "700",
  },
  editorInput: {
    minHeight: 44,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    color: colors.text,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 12,
  },
  editorHelp: {
    color: colors.textDim,
    fontSize: 10,
  },
  editorError: {
    color: colors.danger,
    fontSize: 10,
  },
  reason: {
    color: colors.textDim,
    fontSize: 11,
    lineHeight: 17,
  },
  actions: {
    flexDirection: "row",
    gap: 10,
  },
  skipButton: {
    flex: 0.85,
  },
  applyButton: {
    flex: 1.3,
  },
  undoToast: {
    position: "absolute",
    left: 18,
    right: 18,
    bottom: 84,
    zIndex: 20,
    borderRadius: radii.medium,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: "#19243A",
    paddingHorizontal: 14,
    paddingVertical: 10,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    ...shadows.card,
  },
  undoCopy: {
    flex: 1,
    gap: 2,
  },
  undoTitle: {
    color: colors.text,
    fontSize: 12,
    fontWeight: "700",
  },
  undoSubtitle: {
    color: colors.textMuted,
    fontSize: 11,
  },
});
