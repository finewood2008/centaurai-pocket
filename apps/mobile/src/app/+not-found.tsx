import { useRouter } from "expo-router";

import { Screen } from "@/components/screen";
import { Button, EmptyState } from "@/components/ui";

export default function NotFoundScreen() {
  const router = useRouter();
  return (
    <Screen>
      <EmptyState
        symbol="?"
        title="页面不存在"
        message="这个入口可能已移动，返回今日概览继续使用。"
        action={<Button label="返回首页" onPress={() => router.replace("/")} />}
      />
    </Screen>
  );
}
