// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 「外部 File → 新建节点」这条投递链的载体（模块级暂存）。
 *
 * 为什么不能只靠事件总线：投递方（画布拖入文件、粘贴图片、
 * spawnExternalAssetNodes、UploadNode 变形成 video/audio）都是「先建节点、下一帧
 * 再把 File 发给它」，而 InMemoryCanvasEventBus 没有重放语义 —— 发的那一刻没有
 * 订阅者，File 就永久丢了。低缩放档（zoom < LOW_DETAIL_ZOOM_THRESHOLD）里这条
 * 假设直接不成立：新节点先以 LOD shell 挂载（见 [[LodShellNode]]），完整组件要等
 * requestShellUpgrade 的升级队列放行才挂上订阅，必然晚于投递那一帧。结果就是画布
 * 上留下一个永远空着的「上传资源」节点，跟用户拖进来的图毫不相干。
 *
 * 所以 File 本体走这里暂存，事件降级成「敲一下已挂载的节点」：
 *   - 投递方：stashExternalFile(...) 之后照旧发事件（两件事都做，见各投递点）；
 *   - 消费方：useExternalFileHandoff 在挂载时和收到事件时都 take 一次。
 * take 是消费性的（拿走即删），所以「挂载补投」与「事件即时投」不会重复处理同一个
 * File，也就不需要额外的去重标记。
 *
 * 新增投递方必须 stash —— 只 publish 不 stash 的话消费侧看不到 File（消费侧只认
 * 暂存里的东西，不读事件 payload 的 file）。
 */
import type { CanvasEventMap } from '@/features/canvas/application/ports';

/** 三条「注入外部 File」的通道，与 [[ports]] 里的事件名一一对应。 */
export type ExternalFileChannel = Extract<
  keyof CanvasEventMap,
  'upload-node/external-file' | 'video-node/external-file' | 'audio-node/external-file'
>;

/**
 * 上限只是兜底：节点在消费前就被删掉（撤销、手滑删节点）时没人来 take，条目会
 * 一直握着 File 的引用。超过上限就按插入顺序淘汰最老的一条 —— 待投递的 File 天然
 * 是「刚刚拖进来的那几个」，正常用法下 map 里同时不会有几条。
 */
const MAX_PENDING = 512;

const pending = new Map<string, File>();

/**
 * 分隔符用 NUL：通道名与节点 id 都不可能含它，拼不出歧义的 key。必须写成
 * \u0000 转义 —— 直接嵌一个裸 0x00 字节会让 git 把本文件当二进制，PR 里既没有
 * 行级 diff 也没法评审。
 */
function keyOf(channel: ExternalFileChannel, nodeId: string): string {
  return `${channel}\u0000${nodeId}`;
}

/** 暂存一份待投递的 File；同一节点重复 stash 以最后一次为准。 */
export function stashExternalFile(
  channel: ExternalFileChannel,
  nodeId: string,
  file: File,
): void {
  const key = keyOf(channel, nodeId);
  // 覆盖已有条目不会让 map 变大，此时淘汰只会白白丢掉一份不相干的待投递文件。
  if (pending.size >= MAX_PENDING && !pending.has(key)) {
    const oldest = pending.keys().next();
    if (!oldest.done) pending.delete(oldest.value);
  }
  pending.set(key, file);
}

/** 取走待投递的 File（消费性）；没有则返回 null。 */
export function takeExternalFile(
  channel: ExternalFileChannel,
  nodeId: string,
): File | null {
  const key = keyOf(channel, nodeId);
  const file = pending.get(key);
  if (file === undefined) return null;
  pending.delete(key);
  return file;
}

/** 仅测试用：清空暂存，避免用例间互相污染。 */
export function resetPendingExternalFilesForTest(): void {
  pending.clear();
}
