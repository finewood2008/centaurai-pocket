import { StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";

import { colors, fonts } from "@/theme/colors";

export function BrandMark({
  size = 44,
  style,
}: {
  size?: number;
  style?: StyleProp<ViewStyle>;
}) {
  const accentSize = Math.max(6, Math.round(size * 0.16));

  return (
    <View
      accessibilityLabel="CentaurAI"
      accessibilityRole="image"
      style={[
        styles.mark,
        {
          width: size,
          height: size,
          borderRadius: size / 2,
        },
        style,
      ]}
    >
      <Text
        style={[
          styles.letter,
          {
            fontSize: Math.round(size * 0.48),
            lineHeight: Math.round(size * 0.58),
          },
        ]}
      >
        A
      </Text>
      <View
        style={[
          styles.accent,
          {
            width: accentSize,
            height: accentSize,
            top: -Math.round(accentSize * 0.25),
            right: -Math.round(accentSize * 0.25),
          },
        ]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  mark: {
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    backgroundColor: colors.primarySoft,
    borderColor: colors.gold,
    borderWidth: 1.5,
  },
  letter: {
    color: colors.primaryDark,
    fontFamily: fonts.serif,
    fontWeight: "700",
    textAlign: "center",
  },
  accent: {
    position: "absolute",
    borderRadius: 1,
    backgroundColor: colors.gold,
    transform: [{ rotate: "45deg" }],
  },
});
