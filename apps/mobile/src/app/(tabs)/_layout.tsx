import Ionicons from "@expo/vector-icons/Ionicons";
import { Tabs } from "expo-router";
import type { ComponentProps } from "react";
import { StyleSheet, type ColorValue } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { colors } from "@/theme/colors";

function TabIcon({
  name,
  color,
}: {
  name: ComponentProps<typeof Ionicons>["name"];
  color: ColorValue;
}) {
  return <Ionicons name={name} color={color} size={24} />;
}

export default function TabLayout() {
  const insets = useSafeAreaInsets();

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        sceneStyle: { backgroundColor: colors.background },
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.textDim,
        tabBarLabelStyle: styles.label,
        tabBarIconStyle: styles.icon,
        tabBarStyle: [
          styles.bar,
          {
            height: 60 + insets.bottom,
            paddingBottom: Math.max(insets.bottom, 4),
          },
        ],
        tabBarItemStyle: styles.item,
        tabBarHideOnKeyboard: true,
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "今日",
          tabBarIcon: ({ color }) => <TabIcon name="home-outline" color={color} />,
        }}
      />
      <Tabs.Screen
        name="inbox"
        options={{
          title: "治理",
          tabBarIcon: ({ color }) => (
            <TabIcon name="shield-checkmark-outline" color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="sources"
        options={{
          title: "同步",
          tabBarIcon: ({ color }) => <TabIcon name="sync-outline" color={color} />,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: "设置",
          tabBarIcon: ({ color }) => (
            <TabIcon name="settings-outline" color={color} />
          ),
        }}
      />
    </Tabs>
  );
}

const styles = StyleSheet.create({
  bar: {
    paddingTop: 4,
    backgroundColor: colors.surfaceSoft,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    elevation: 0,
    shadowOpacity: 0,
  },
  item: {
    paddingVertical: 3,
  },
  icon: {
    marginTop: 0,
  },
  label: {
    fontSize: 10,
    fontWeight: "500",
    lineHeight: 12,
    marginTop: 1,
  },
});
