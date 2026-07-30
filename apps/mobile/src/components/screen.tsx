import type { PropsWithChildren, ReactNode } from "react";
import {
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type ScrollViewProps,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import { colors } from "@/theme/colors";

type ScreenProps = PropsWithChildren<{
  scroll?: boolean;
  refreshing?: boolean;
  onRefresh?: () => void;
  footer?: ReactNode;
  contentContainerStyle?: ScrollViewProps["contentContainerStyle"];
}>;

export function Screen({
  children,
  scroll = true,
  refreshing = false,
  onRefresh,
  footer,
  contentContainerStyle,
}: ScreenProps) {
  const content = scroll ? (
    <ScrollView
      contentContainerStyle={[styles.content, contentContainerStyle]}
      showsVerticalScrollIndicator={false}
      keyboardShouldPersistTaps="handled"
      refreshControl={
        onRefresh ? (
          <RefreshControl
            refreshing={refreshing}
            onRefresh={onRefresh}
            tintColor={colors.primary}
            colors={[colors.primary]}
            progressBackgroundColor={colors.surface}
          />
        ) : undefined
      }
    >
      {children}
    </ScrollView>
  ) : (
    <View style={[styles.content, styles.flex]}>{children}</View>
  );

  return (
    <SafeAreaView style={styles.safeArea} edges={["top", "left", "right"]}>
      <View style={styles.orbOne} />
      <View style={styles.orbTwo} />
      {content}
      {footer}
    </SafeAreaView>
  );
}

export function BrandHeader({
  eyebrow,
  title,
  subtitle,
  trailing,
}: {
  eyebrow?: string;
  title: string;
  subtitle?: string;
  trailing?: ReactNode;
}) {
  return (
    <View style={styles.header}>
      <View style={styles.headerCopy}>
        {eyebrow ? <Text style={styles.eyebrow}>{eyebrow}</Text> : null}
        <Text style={styles.title}>{title}</Text>
        {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
      </View>
      {trailing}
    </View>
  );
}

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: colors.background,
    overflow: "hidden",
  },
  flex: {
    flex: 1,
  },
  content: {
    paddingHorizontal: 20,
    paddingTop: 14,
    paddingBottom: 120,
    gap: 18,
  },
  orbOne: {
    position: "absolute",
    top: -120,
    right: -100,
    width: 280,
    height: 280,
    borderRadius: 140,
    backgroundColor: "#183348",
    opacity: 0.38,
  },
  orbTwo: {
    position: "absolute",
    top: 210,
    left: -170,
    width: 260,
    height: 260,
    borderRadius: 130,
    backgroundColor: "#2D245A",
    opacity: 0.2,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 16,
  },
  headerCopy: {
    flex: 1,
    gap: 5,
  },
  eyebrow: {
    color: colors.primary,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 2.2,
  },
  title: {
    color: colors.text,
    fontSize: 28,
    fontWeight: "800",
    letterSpacing: -0.7,
  },
  subtitle: {
    color: colors.textMuted,
    fontSize: 13,
    lineHeight: 20,
  },
});
