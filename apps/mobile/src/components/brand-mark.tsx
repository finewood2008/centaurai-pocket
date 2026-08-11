import { StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";

import { colors, fonts, radii } from "@/theme/colors";

/**
 * 金石 · 数据层的印：方形黛青「盒」印（DESIGN_SYSTEM_V3 跟进）。
 * 与秘书的朱砂「秘」印同一语言、不同职守——朱砂管事务，黛青守数据。
 */
export function BrandMark({
  size = 44,
  style,
}: {
  size?: number;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <View
      accessibilityLabel="CentaurAI Pocket · 盒"
      accessibilityRole="image"
      style={[
        styles.mark,
        {
          width: size,
          height: size,
          borderRadius: Math.max(3, Math.round(size * 0.12)),
        },
        style,
      ]}
    >
      <Text
        style={[
          styles.letter,
          {
            fontSize: Math.round(size * 0.52),
            lineHeight: Math.round(size * 0.62),
          },
        ]}
      >
        盒
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  mark: {
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    borderRadius: radii.small,
    backgroundColor: colors.primary,
    borderColor: colors.primaryDark,
    borderWidth: 1,
  },
  letter: {
    color: colors.white,
    fontFamily: fonts.serif,
    fontWeight: "700",
    textAlign: "center",
  },
});
