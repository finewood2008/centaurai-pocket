import { useEffect, useRef } from "react";
import { useIncomingShare } from "expo-sharing";
import { Stack, usePathname, useRouter } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { StatusBar } from "expo-status-bar";
import { Platform } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { PocketProvider } from "@/context/pocket-context";
import { colors } from "@/theme/colors";

SplashScreen.setOptions({
  duration: 320,
  fade: true,
});

function NativeIncomingShareNavigator() {
  const router = useRouter();
  const pathname = usePathname();
  const { sharedPayloads, refreshSharePayloads } = useIncomingShare();
  const handledSignatureRef = useRef("");

  useEffect(() => {
    if (
      pathname !== "/handle-share" &&
      handledSignatureRef.current
    ) {
      refreshSharePayloads();
    }
  }, [pathname, refreshSharePayloads]);

  useEffect(() => {
    if (sharedPayloads.length === 0) {
      handledSignatureRef.current = "";
      return;
    }

    const signature = JSON.stringify(sharedPayloads);
    if (
      signature === handledSignatureRef.current ||
      pathname === "/handle-share"
    ) {
      return;
    }
    handledSignatureRef.current = signature;
    router.push("/handle-share");
  }, [pathname, router, sharedPayloads]);

  return null;
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <PocketProvider>
        <StatusBar style="dark" />
        {Platform.OS === "web" ? null : <NativeIncomingShareNavigator />}
        <Stack
          screenOptions={{
            headerShown: false,
            contentStyle: { backgroundColor: colors.background },
            animation: "fade_from_bottom",
          }}
        >
          <Stack.Screen name="(tabs)" />
          <Stack.Screen
            name="handle-share"
            options={{
              presentation: "modal",
              animation: "slide_from_bottom",
            }}
          />
          <Stack.Screen
            name="add-source"
            options={{
              presentation: "modal",
              animation: "slide_from_bottom",
            }}
          />
        </Stack>
      </PocketProvider>
    </SafeAreaProvider>
  );
}
