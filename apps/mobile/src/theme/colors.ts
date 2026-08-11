import { Platform } from "react-native";

/**
 * 金石 · 数据层中性变体（DESIGN_SYSTEM_V3，嘉木 2026-08-11 选定）。
 *
 * 与超级秘书视觉同源：同一张纸（#F5F2E9）、同一色墨（#1F1C17）、同一条
 * 墨线（#D9D3C3）。分工靠主色区分——秘书是朱砂（事务层的确认与在岗），
 * Pocket 用黛青（数据层的沉静），朱砂在这里只作小面积记号（dangerish 警示
 * 另有铁锈色）。旧「暖米 Warm Cream」体系已随 V2 视觉一并作废。
 *
 * Keep the legacy property names because feature screens consume them
 * directly; the aliases below map older violet/blue roles onto the current
 * brand accents.
 */
export const colors = {
  background: "#F5F2E9",
  backgroundRaised: "#E7E3D7",
  surface: "#FBF9F1",
  surfaceSoft: "#EDE9DC",
  surfaceHighlight: "#E3DECD",
  border: "#D9D3C3",
  borderSoft: "#E6E1D2",
  text: "#1F1C17",
  textMuted: "#6F6A5F",
  textDim: "#8E887A",
  /** 黛青：数据层主色（同秘书的 info 色域，沉静不抢） */
  primary: "#3A5A78",
  primaryDark: "#2C475F",
  primarySoft: "#E6ECF0",
  primaryBorder: "#C9D6DF",
  /** 赭铜：提醒/高亮 */
  gold: "#8A6D3B",
  goldDark: "#6E5426",
  goldSoft: "#F0E9D5",
  violet: "#6E5426",
  violetSoft: "#F0E9D5",
  blue: "#3A5A78",
  blueSoft: "#E6ECF0",
  info: "#3A5A78",
  infoSoft: "#E6ECF0",
  warning: "#6E5426",
  warningSoft: "#F0E9D5",
  danger: "#7A2417",
  dangerSoft: "#F4E0DA",
  success: "#4F6B45",
  successSoft: "#E8EDE0",
  white: "#FFFFFF",
  black: "#000000",
} as const;

/** 金石收紧圆角：器物的方正。 */
export const radii = {
  small: 5,
  medium: 8,
  large: 12,
  pill: 999,
} as const;

export const fonts = {
  sans:
    Platform.select({
      ios: "PingFang SC",
      android: "sans-serif",
      web: "'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif",
      default: "sans-serif",
    }) ?? "sans-serif",
  serif:
    Platform.select({
      ios: "Songti SC",
      android: "serif",
      web: "'Noto Serif SC', 'Songti SC', serif",
      default: "serif",
    }) ?? "serif",
} as const;

/** 金石：细墨线立骨，阴影只留一口气。 */
export const shadows = {
  card: {
    shadowColor: "#1F1C17",
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.04,
    shadowRadius: 4,
    elevation: 1,
  },
} as const;
