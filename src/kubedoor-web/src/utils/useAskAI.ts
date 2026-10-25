import { ElMessage } from "element-plus";
import { useAIStoreHook } from "@/store/modules/ai";
import type { AIAskRequest } from "@/utils/ai";

/** 页面上的「问 AI」：打开 AI 助手，新开会话、选好资源，把问题放进输入框等用户确认 */
export function useAskAI() {
  const ai = useAIStoreHook();
  return async (request: AIAskRequest) => {
    try {
      await ai.ask(request);
    } catch (reason) {
      ElMessage.error(
        reason instanceof Error ? reason.message : "打开 AI 助手失败"
      );
    }
  };
}
