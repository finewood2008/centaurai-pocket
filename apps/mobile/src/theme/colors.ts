export const colors = {
  background: "#070B14",
  backgroundRaised: "#0A1020",
  surface: "#10182A",
  surfaceSoft: "#151F34",
  surfaceHighlight: "#1A2740",
  border: "#24314A",
  borderSoft: "#1A263C",
  text: "#F4F7FC",
  textMuted: "#93A3BE",
  textDim: "#667692",
  primary: "#39E6C2",
  primaryDark: "#123F3B",
  primarySoft: "#173A39",
  violet: "#A58BFF",
  violetSoft: "#282344",
  blue: "#69A7FF",
  blueSoft: "#182A48",
  warning: "#FFBF69",
  warningSoft: "#3F3022",
  danger: "#FF718A",
  dangerSoft: "#442431",
  success: "#52E096",
  white: "#FFFFFF",
  black: "#000000",
} as const;

export const radii = {
  small: 10,
  medium: 16,
  large: 22,
  pill: 999,
} as const;

export const shadows = {
  card: {
    shadowColor: colors.black,
    shadowOffset: { width: 0, height: 10 },
    shadowOpacity: 0.18,
    shadowRadius: 22,
    elevation: 4,
  },
} as const;
