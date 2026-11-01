import { copyTextToClipboard } from "@pureadmin/utils";

/**
 * 复制文本到剪贴板,返回是否成功。
 * 先用同步的 execCommand:HTTP 访问(非安全上下文)时没有 Clipboard API,它也能用;不行再试 Clipboard API
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (copyTextToClipboard(text)) return true;
  } catch {
    // 再试 Clipboard API
  }
  if (!window.isSecureContext || !navigator.clipboard) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
