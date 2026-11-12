// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * 虾导本地助手运行环境缺失（P1-5）的识别与记忆。
 *
 * 后端目前没有独立的 runtime-status 端点，运行时缺失唯一的信号是
 * `_user_facing_chat_error` 返回的那句文案（或未来后端返回的
 * `runtime_missing` 错误码）。一旦命中就记住：下次打开面板时，
 * 在发任何消息之前就先展示缺失状态，而不是先显示「已连接」再失败。
 * 收到任何一条助手有效回复即视为运行环境恢复，清除记忆。
 */
const RUNTIME_MISSING_STORAGE_KEY = "reelforge:chat-runtime-missing";

/** 后端返回的「本地助手运行时不可用」文案（见 src/novelvideo/api/routes/chat.py）。 */
export const RUNTIME_MISSING_BACKEND_MESSAGE = "本地助手运行时不可用";

export function isRuntimeMissingText(text: string | null | undefined): boolean {
  if (!text) return false;
  return (
    text.includes("runtime_missing") ||
    text.includes(RUNTIME_MISSING_BACKEND_MESSAGE) ||
    text.includes("CodexRuntimeError") ||
    text.includes("HermesForkMissing")
  );
}

export function readRuntimeMissing(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(RUNTIME_MISSING_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function markRuntimeMissing(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(RUNTIME_MISSING_STORAGE_KEY, "1");
  } catch {
    // 本地存储不可用时只影响跨会话记忆，本次会话内的 state 仍然有效。
  }
}

export function clearRuntimeMissing(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(RUNTIME_MISSING_STORAGE_KEY);
  } catch {
    // ignore
  }
}

/**
 * 把后端那句误导文案（「请重启应用；若仍失败请更新应用」）换成真因：
 * 本地助手运行环境（Codex）没装。用于助手消息文本的渲染归一化，
 * 也用于错误横幅——「重启/更新」解决不了「没装」。
 */
// i18n-exempt-start —— 比对的是后端报错原文
const MISLEADING_RUNTIME_SENTENCE =
  "本地助手运行时不可用：请重启应用；若仍失败，请更新应用并导出诊断日志。";
const CORRECTED_RUNTIME_SENTENCE =
  "未检测到本地助手运行环境（Codex），请在设置中下载/安装。";
// i18n-exempt-end

export function translateRuntimeMissingMessageText(text: string): string {
  if (!text.includes(RUNTIME_MISSING_BACKEND_MESSAGE)) return text;
  if (text.includes(MISLEADING_RUNTIME_SENTENCE)) {
    return text.replace(MISLEADING_RUNTIME_SENTENCE, CORRECTED_RUNTIME_SENTENCE);
  }
  // 只有「本地助手运行时不可用」没有后半句时，也把整句换掉，避免半句误导残留。
  return text.replace(
    /本地助手运行时不可用[^。\n]*。?/g,
    CORRECTED_RUNTIME_SENTENCE,
  );
}
