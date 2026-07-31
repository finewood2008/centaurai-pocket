import type { ExpoConfig } from "expo/config";

const config: ExpoConfig = {
  name: "半人马随身数据中心",
  slug: "centaurai-pocket",
  scheme: "centaur-pocket",
  version: "0.1.0",
  icon: "./assets/icon.png",
  orientation: "portrait",
  userInterfaceStyle: "light",
  backgroundColor: "#FAF7F2",
  primaryColor: "#C0755A",
  ios: {
    bundleIdentifier: "ai.centaur.pocket",
    buildNumber: "1",
    supportsTablet: false,
    config: {
      usesNonExemptEncryption: false,
    },
  },
  android: {
    package: "ai.centaur.pocket",
    versionCode: 1,
    allowBackup: false,
    blockedPermissions: [
      "android.permission.READ_EXTERNAL_STORAGE",
      "android.permission.WRITE_EXTERNAL_STORAGE",
      "android.permission.SYSTEM_ALERT_WINDOW",
      "android.permission.VIBRATE",
    ],
    adaptiveIcon: {
      foregroundImage: "./assets/adaptive-icon.png",
      monochromeImage: "./assets/monochrome-icon.png",
      backgroundColor: "#151827",
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
    "expo-font",
    [
      "expo-status-bar",
      {
        hidden: false,
        style: "dark",
      },
    ],
    [
      "expo-splash-screen",
      {
        backgroundColor: "#FAF7F2",
        image: "./assets/icon.png",
        imageWidth: 120,
        resizeMode: "contain",
      },
    ],
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
          multipleShareMimeTypes: ["text/plain", "text/*"],
        },
      },
    ],
  ],
  experiments: {
    typedRoutes: true,
  },
};

export default config;
