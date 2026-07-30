import { Tabs } from "expo-router";
import { StyleSheet, Text, type ColorValue } from "react-native";

import { colors } from "@/theme/colors";

function TabIcon({
  symbol,
  color,
}: {
  symbol: string;
  color: ColorValue;
}) {
  return <Text style={[styles.icon, { color }]}>{symbol}</Text>;
}

export default function TabLayout() {
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        sceneStyle: { backgroundColor: colors.background },
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.textDim,
        tabBarLabelStyle: styles.label,
        tabBarStyle: styles.bar,
        tabBarItemStyle: styles.item,
        tabBarHideOnKeyboard: true,
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "今日",
          tabBarIcon: ({ color }) => <TabIcon symbol="⌂" color={color} />,
        }}
      />
      <Tabs.Screen
        name="inbox"
        options={{
          title: "治理",
          tabBarIcon: ({ color }) => <TabIcon symbol="◇" color={color} />,
        }}
      />
      <Tabs.Screen
        name="sources"
        options={{
          title: "同步",
          tabBarIcon: ({ color }) => <TabIcon symbol="↻" color={color} />,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: "设置",
          tabBarIcon: ({ color }) => <TabIcon symbol="⚙" color={color} />,
        }}
      />
    </Tabs>
  );
}

const styles = StyleSheet.create({
  bar: {
    position: "absolute",
    height: 76,
    paddingTop: 8,
    paddingBottom: 10,
    backgroundColor: "#0B1220F5",
    borderTopWidth: 1,
    borderTopColor: colors.borderSoft,
    elevation: 0,
  },
  item: {
    borderRadius: 14,
  },
  icon: {
    fontSize: 22,
    fontWeight: "700",
    lineHeight: 24,
  },
  label: {
    fontSize: 11,
    fontWeight: "700",
    marginTop: 2,
  },
});
