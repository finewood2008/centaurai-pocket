import type { ReactNode } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
  type PressableProps,
  type StyleProp,
  type ViewStyle,
} from "react-native";

import { colors, radii } from "@/theme/colors";

export function SectionHeader({
  title,
  caption,
  action,
}: {
  title: string;
  caption?: string;
  action?: ReactNode;
}) {
  return (
    <View style={styles.sectionHeader}>
      <View style={styles.sectionHeaderCopy}>
        <Text style={styles.sectionTitle}>{title}</Text>
        {caption ? <Text style={styles.sectionCaption}>{caption}</Text> : null}
      </View>
      {action}
    </View>
  );
}

export function Pill({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "neutral" | "primary" | "warning" | "danger" | "violet";
}) {
  return (
    <View style={[styles.pill, pillTone[tone]]}>
      <Text style={[styles.pillText, pillTextTone[tone]]}>{label}</Text>
    </View>
  );
}

export function Notice({
  title,
  message,
  tone = "primary",
  action,
}: {
  title: string;
  message: string;
  tone?: "primary" | "warning" | "danger";
  action?: ReactNode;
}) {
  return (
    <View style={[styles.notice, noticeTone[tone]]}>
      <View style={styles.noticeIcon}>
        <Text style={styles.noticeIconText}>
          {tone === "danger" ? "!" : tone === "warning" ? "↻" : "·"}
        </Text>
      </View>
      <View style={styles.noticeCopy}>
        <Text style={styles.noticeTitle}>{title}</Text>
        <Text style={styles.noticeMessage}>{message}</Text>
      </View>
      {action}
    </View>
  );
}

export function EmptyState({
  symbol = "✓",
  title,
  message,
  action,
}: {
  symbol?: string;
  title: string;
  message: string;
  action?: ReactNode;
}) {
  return (
    <View style={styles.empty}>
      <View style={styles.emptyIcon}>
        <Text style={styles.emptyIconText}>{symbol}</Text>
      </View>
      <Text style={styles.emptyTitle}>{title}</Text>
      <Text style={styles.emptyMessage}>{message}</Text>
      {action}
    </View>
  );
}

type ButtonProps = PressableProps & {
  label: string;
  tone?: "primary" | "secondary" | "ghost" | "danger";
  loading?: boolean;
  icon?: string;
  compact?: boolean;
  style?: StyleProp<ViewStyle>;
};

export function Button({
  label,
  tone = "primary",
  loading = false,
  icon,
  compact = false,
  disabled,
  style,
  ...props
}: ButtonProps) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled || loading}
      style={({ pressed }) => [
        styles.button,
        compact && styles.buttonCompact,
        buttonTone[tone],
        (disabled || loading) && styles.buttonDisabled,
        pressed && styles.buttonPressed,
        style,
      ]}
      {...props}
    >
      {loading ? (
        <ActivityIndicator
          size="small"
          color={tone === "primary" ? colors.background : colors.primary}
        />
      ) : icon ? (
        <Text style={[styles.buttonIcon, buttonTextTone[tone]]}>{icon}</Text>
      ) : null}
      <Text style={[styles.buttonText, buttonTextTone[tone]]}>{label}</Text>
    </Pressable>
  );
}

export function LoadingCards({ count = 3 }: { count?: number }) {
  return (
    <View style={styles.loadingList}>
      {Array.from({ length: count }, (_, index) => (
        <View key={index} style={styles.loadingCard}>
          <View style={[styles.skeleton, styles.skeletonShort]} />
          <View style={[styles.skeleton, styles.skeletonLong]} />
          <View style={[styles.skeleton, styles.skeletonMedium]} />
        </View>
      ))}
    </View>
  );
}

const pillTone = StyleSheet.create({
  neutral: { backgroundColor: colors.surfaceHighlight },
  primary: { backgroundColor: colors.primarySoft },
  warning: { backgroundColor: colors.warningSoft },
  danger: { backgroundColor: colors.dangerSoft },
  violet: { backgroundColor: colors.violetSoft },
});

const pillTextTone = StyleSheet.create({
  neutral: { color: colors.textMuted },
  primary: { color: colors.primary },
  warning: { color: colors.warning },
  danger: { color: colors.danger },
  violet: { color: colors.violet },
});

const noticeTone = StyleSheet.create({
  primary: { borderColor: "#23544F", backgroundColor: "#102C2D" },
  warning: { borderColor: "#5A4729", backgroundColor: "#292319" },
  danger: { borderColor: "#5F2F3C", backgroundColor: "#2B1921" },
});

const buttonTone = StyleSheet.create({
  primary: { backgroundColor: colors.primary },
  secondary: {
    backgroundColor: colors.surfaceHighlight,
    borderWidth: 1,
    borderColor: colors.border,
  },
  ghost: { backgroundColor: "transparent" },
  danger: { backgroundColor: colors.dangerSoft },
});

const buttonTextTone = StyleSheet.create({
  primary: { color: colors.background },
  secondary: { color: colors.text },
  ghost: { color: colors.textMuted },
  danger: { color: colors.danger },
});

const styles = StyleSheet.create({
  sectionHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
  sectionHeaderCopy: {
    flex: 1,
    gap: 3,
  },
  sectionTitle: {
    color: colors.text,
    fontSize: 18,
    fontWeight: "700",
  },
  sectionCaption: {
    color: colors.textMuted,
    fontSize: 12,
  },
  pill: {
    alignSelf: "flex-start",
    borderRadius: radii.pill,
    paddingHorizontal: 10,
    paddingVertical: 5,
  },
  pillText: {
    fontSize: 11,
    fontWeight: "700",
  },
  notice: {
    borderWidth: 1,
    borderRadius: radii.medium,
    padding: 14,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  noticeIcon: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: "rgba(255,255,255,0.08)",
    alignItems: "center",
    justifyContent: "center",
  },
  noticeIconText: {
    color: colors.text,
    fontSize: 17,
    fontWeight: "800",
  },
  noticeCopy: {
    flex: 1,
    gap: 3,
  },
  noticeTitle: {
    color: colors.text,
    fontWeight: "700",
    fontSize: 13,
  },
  noticeMessage: {
    color: colors.textMuted,
    fontSize: 12,
    lineHeight: 18,
  },
  empty: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    alignItems: "center",
    padding: 28,
    gap: 9,
  },
  emptyIcon: {
    width: 52,
    height: 52,
    borderRadius: 26,
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 4,
  },
  emptyIconText: {
    color: colors.primary,
    fontSize: 24,
    fontWeight: "800",
  },
  emptyTitle: {
    color: colors.text,
    fontSize: 17,
    fontWeight: "700",
  },
  emptyMessage: {
    color: colors.textMuted,
    textAlign: "center",
    fontSize: 13,
    lineHeight: 20,
  },
  button: {
    minHeight: 46,
    borderRadius: radii.medium,
    paddingHorizontal: 18,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 7,
  },
  buttonCompact: {
    minHeight: 36,
    borderRadius: 12,
    paddingHorizontal: 13,
  },
  buttonPressed: {
    opacity: 0.78,
    transform: [{ scale: 0.985 }],
  },
  buttonDisabled: {
    opacity: 0.45,
  },
  buttonIcon: {
    fontSize: 16,
    fontWeight: "800",
  },
  buttonText: {
    fontSize: 14,
    fontWeight: "700",
  },
  loadingList: {
    gap: 12,
  },
  loadingCard: {
    height: 142,
    borderRadius: radii.large,
    padding: 18,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    gap: 14,
  },
  skeleton: {
    height: 12,
    borderRadius: 6,
    backgroundColor: colors.surfaceHighlight,
  },
  skeletonShort: {
    width: "28%",
  },
  skeletonMedium: {
    width: "60%",
  },
  skeletonLong: {
    width: "92%",
    height: 20,
  },
});
