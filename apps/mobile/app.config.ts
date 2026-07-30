import type { ExpoConfig } from "expo/config";

const config: ExpoConfig = {
  name: "半人马随身数据中心",
  slug: "centaurai-pocket",
  scheme: "centaur-pocket",
  version: "0.1.0",
  icon: "./assets/icon.png",
  orientation: "portrait",
  userInterfaceStyle: "dark",
  backgroundColor: "#070B14",
  ios: {
    bundleIdentifier: "ai.centaur.pocket",
    supportsTablet: true,
  },
  android: {
    package: "ai.centaur.pocket",
    allowBackup: false,
    blockedPermissions: [
      "android.permission.READ_EXTERNAL_STORAGE",
      "android.permission.WRITE_EXTERNAL_STORAGE",
    ],
    adaptiveIcon: {
      foregroundImage: "./assets/adaptive-icon.png",
      backgroundColor: "#070B14",
    },
    predictiveBackGestureEnabled: false,
  },
  web: {
    bundler: "metro",
    output: "static",
    favicon: "./assets/icon.png",
  },
  plugins: [
    "expo-router",
    "expo-secure-store",
    [
      "expo-sharing",
      {
        ios: {
          enabled: true,
          activationRule: {
            supportsText: true,
            supportsWebUrlWithMaxCount: 1,
            supportsWebPageWithMaxCount: 1,
          },
        },
        android: {
          enabled: true,
          singleShareMimeTypes: ["text/plain", "text/*"],
        },
      },
    ],
  ],
  experiments: {
    typedRoutes: true,
  },
};

export default config;
