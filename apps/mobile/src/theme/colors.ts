import { Platform } from "react-native";

/**
 * CentaurAI "暖米 Warm Cream" palette.
 *
 * These values mirror the current CentaurAI renderer theme. Keep the legacy
 * property names because feature screens consume them directly; the aliases
 * below intentionally map older violet/blue roles onto the current brand
 * accents.
 */
export const colors = {
  background: "#FAF7F2",
  backgroundRaised: "#F3ECE2",
  surface: "#FFFDFA",
  surfaceSoft: "#F3ECE2",
  surfaceHighlight: "#ECE3D6",
  border: "#E6DFD4",
  borderSoft: "#EFE9DE",
  text: "#2C2C2E",
  textMuted: "#55534F",
  textDim: "#77716A",
  primary: "#C0755A",
  primaryDark: "#A35C43",
  primarySoft: "#F8EDE7",
  primaryBorder: "#F0D9CE",
  gold: "#E8B04B",
  goldDark: "#8A5E12",
  goldSoft: "#FBF0D6",
  violet: "#8A5E12",
  violetSoft: "#FBF0D6",
  blue: "#5090E0",
  blueSoft: "#EDF4FD",
  info: "#5090E0",
  infoSoft: "#EDF4FD",
  warning: "#8A5E12",
  warningSoft: "#FBF0D6",
  danger: "#C0492F",
  dangerSoft: "#FFF5F2",
  success: "#5A8A4E",
  successSoft: "#E7EFE2",
  white: "#FFFFFF",
  black: "#000000",
} as const;

export const radii = {
  small: 8,
  medium: 12,
  large: 18,
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

export const shadows = {
  card: {
    shadowColor: "#2C1E14",
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: 0.08,
    shadowRadius: 12,
    elevation: 2,
  },
} as const;
