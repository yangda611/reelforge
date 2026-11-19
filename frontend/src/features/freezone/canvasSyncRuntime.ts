// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import {
  getFreezoneCanvas,
  getFreezoneCanvasDelta,
  type FreezoneCanvasPayload,
} from "@/api/canvas";
import { useCanvasStore, type CanvasEdge, type CanvasNode } from "@/stores/canvasStore";
import { getFreezoneCanvasMetadata } from "./canvasMetadataContext";

export type RemoteCanvasMerge = (
  remoteNodes: CanvasNode[],
  remoteEdges: CanvasEdge[],
  localNodes: CanvasNode[],
  localEdges: CanvasEdge[],
) => { nodes: CanvasNode[]; edges: CanvasEdge[] };

export interface RemoteCanvasApplyOptions {
  /**
   * Refuse to replace the open canvas when it contains edits that have not
   * reached the server yet. Remote writes initiated by the current Agent use
   * this guard: a clean canvas can adopt the Agent's new revision silently,
   * while genuine concurrent local work still gets the recovery overlay.
   */
  protectUnsavedLocalEdits?: boolean;
  conflictMessage?: string;
}

type RemoteCanvasApplier = (
  remote: FreezoneCanvasPayload,
  merge?: RemoteCanvasMerge,
  options?: RemoteCanvasApplyOptions,
) => boolean;

type CanvasFlush = () => Promise<boolean>;
type CanvasRevisionReader = () => number | null;

export interface LocalProjectionPayload {
  projectionKey: string;
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  metadata?: Record<string, unknown> | null;
}

type LocalProjectionApplier = (
  projection: LocalProjectionPayload,
) => boolean;
type LocalProjectionRemover = (projectionKey: string) => boolean;

interface RemoteCanvasRuntime {
  project: string;
  canvasId: string;
  apply: RemoteCanvasApplier;
  flush?: CanvasFlush;
  applyLocalProjection?: LocalProjectionApplier;
  removeLocalProjection?: LocalProjectionRemover;
  /** Include hydration and local saves in the revision barrier. */
  getRevision?: CanvasRevisionReader;
  appliedRevision: number | null;
}

let currentRuntime: RemoteCanvasRuntime | null = null;
const pendingLocalProjections = new Map<string, LocalProjectionPayload[]>();

function runtimeKey(project: string, canvasId: string): string {
  return `${project}\u0000${canvasId}`;
}

export function registerFreezoneCanvasRuntime(
  project: string,
  canvasId: string,
  apply: RemoteCanvasApplier,
  flush?: CanvasFlush,
  applyLocalProjection?: LocalProjectionApplier,
  removeLocalProjection?: LocalProjectionRemover,
  getRevision?: CanvasRevisionReader,
): () => void {
  const runtime: RemoteCanvasRuntime = {
    project, canvasId, apply, flush, applyLocalProjection, removeLocalProjection,
    getRevision, appliedRevision: null,
  };
  currentRuntime = runtime;
  return () => {
    if (currentRuntime === runtime) {
      currentRuntime = null;
    }
  };
}

export function readFreezoneCanvasRuntimeRevision(
  project: string,
  canvasId: string,
): { revision: number | null } | null {
  if (
    !currentRuntime ||
    currentRuntime.project !== project ||
    currentRuntime.canvasId !== canvasId
  ) {
    return null;
  }
  return { revision: currentRuntime.getRevision?.() ?? null };
}

/** null means no runtime is registered; false means an active runtime refused the data.
 * Callers may use a direct-store fallback only for null, never for a refusal.
 */
export function applyRemoteFreezoneCanvas(
  project: string,
  canvasId: string,
  remote: FreezoneCanvasPayload,
  merge?: RemoteCanvasMerge,
  options?: RemoteCanvasApplyOptions,
): boolean | null {
  const runtime = currentRuntime;
  if (!runtime) return null;
  if (runtime.project !== project || runtime.canvasId !== canvasId) {
    return false;
  }
  const revision = typeof remote.revision === "number" && Number.isFinite(remote.revision)
    ? remote.revision : null;
  const latestRevision = Math.max(runtime.appliedRevision ?? 0, runtime.getRevision?.() ?? 0);
  // Reject stale snapshots before the applier can touch drafts, save sessions,
  // or conflict state. Missing revisions cannot replace a known version either.
  if (latestRevision > 0 && (revision === null || revision < latestRevision)) {
    return false;
  }
  const applied = runtime.apply(remote, merge, options);
  if (applied && revision !== null) runtime.appliedRevision = revision;
  return applied;
}

export async function refreshRemoteFreezoneCanvas(
  project: string,
  canvasId: string,
  options?: RemoteCanvasApplyOptions,
): Promise<boolean> {
  const runtime = currentRuntime;
  if (!runtime || runtime.project !== project || runtime.canvasId !== canvasId) return false;
  const remote = await getFreezoneCanvas(project, canvasId);
  // A response belongs to the registration that requested it, even if the
  // user leaves and later opens the same project/canvas URL again.
  if (currentRuntime !== runtime) return false;
  return applyRemoteFreezoneCanvas(project, canvasId, remote, undefined, options) === true;
}

function applyDeltaItemsById<T extends { id: string }>(
  current: T[],
  upserts: T[],
  removeIds: string[],
): T[] {
  const removeSet = new Set(removeIds);
  const upsertMap = new Map<string, T>();
  const upsertOrder: string[] = [];
  for (const item of upserts) {
    if (!item || typeof item.id !== "string" || !item.id) continue;
    removeSet.delete(item.id);
    if (!upsertMap.has(item.id)) upsertOrder.push(item.id);
    upsertMap.set(item.id, item);
  }
  const merged: T[] = [];
  const seen = new Set<string>();
  for (const item of current) {
    if (removeSet.has(item.id)) continue;
    seen.add(item.id);
    merged.push(upsertMap.get(item.id) ?? item);
  }
  for (const id of upsertOrder) {
    if (!seen.has(id)) {
      const next = upsertMap.get(id);
      if (next) merged.push(next);
    }
  }
  return merged;
}

export async function refreshRemoteFreezoneCanvasDelta(
  project: string,
  canvasId: string,
  sinceRevision: number,
  options?: RemoteCanvasApplyOptions,
): Promise<boolean> {
  const runtime = currentRuntime;
  if (!runtime || runtime.project !== project || runtime.canvasId !== canvasId) return false;
  if (typeof getFreezoneCanvasDelta !== "function") {
    return await refreshRemoteFreezoneCanvas(project, canvasId, options);
  }
  try {
    const delta = await getFreezoneCanvasDelta(project, canvasId, sinceRevision);
    if (currentRuntime !== runtime) return false;
    if (delta.mode === "noop") {
      return true;
    }
    if (delta.mode === "full") {
      const fullPayload: FreezoneCanvasPayload = {
        ...delta.canvas,
        ...(delta.foreign_media ? { foreign_media: delta.foreign_media } : {}),
      };
      return applyRemoteFreezoneCanvas(project, canvasId, fullPayload, undefined, options) === true;
    }
    const storeState = useCanvasStore.getState();
    const mergedNodes = applyDeltaItemsById(
      storeState.nodes,
      (delta.upsert_nodes ?? []) as CanvasNode[],
      delta.remove_node_ids ?? [],
    );
    const mergedEdges = applyDeltaItemsById(
      storeState.edges,
      (delta.upsert_edges ?? []) as CanvasEdge[],
      delta.remove_edge_ids ?? [],
    );
    const remotePayload: FreezoneCanvasPayload = {
      canvas_id: canvasId,
      project_id: project,
      revision: delta.revision,
      nodes: mergedNodes,
      edges: mergedEdges,
      metadata: delta.has_metadata ? (delta.metadata ?? null) : getFreezoneCanvasMetadata(),
      ...(delta.has_viewport ? { viewport: delta.viewport } : { viewport: storeState.currentViewport }),
      ...(delta.foreign_media ? { foreign_media: delta.foreign_media } : {}),
    };
    return applyRemoteFreezoneCanvas(project, canvasId, remotePayload, undefined, options) === true;
  } catch {
    return await refreshRemoteFreezoneCanvas(project, canvasId, options);
  }
}

export async function flushFreezoneCanvasRuntime(
  project: string,
  canvasId: string,
): Promise<boolean | null> {
  if (!currentRuntime || currentRuntime.project !== project || currentRuntime.canvasId !== canvasId) {
    return null;
  }
  if (!currentRuntime.flush) {
    return null;
  }
  return await currentRuntime.flush();
}

export function queueLocalFreezoneProjection(
  project: string,
  canvasId: string,
  projection: LocalProjectionPayload,
): void {
  const key = runtimeKey(project, canvasId);
  const existing = pendingLocalProjections.get(key) ?? [];
  pendingLocalProjections.set(key, [
    ...existing.filter((item) => item.projectionKey !== projection.projectionKey),
    projection,
  ]);
}

export function consumeQueuedLocalFreezoneProjections(
  project: string,
  canvasId: string,
): boolean {
  if (
    !currentRuntime ||
    currentRuntime.project !== project ||
    currentRuntime.canvasId !== canvasId ||
    !currentRuntime.applyLocalProjection
  ) {
    return false;
  }
  const key = runtimeKey(project, canvasId);
  const queued = pendingLocalProjections.get(key);
  if (!queued || queued.length === 0) {
    return false;
  }
  const remaining: LocalProjectionPayload[] = [];
  let applied = false;
  for (const projection of queued) {
    if (currentRuntime.applyLocalProjection(projection)) {
      applied = true;
    } else {
      remaining.push(projection);
    }
  }
  if (remaining.length > 0) {
    pendingLocalProjections.set(key, remaining);
  } else {
    pendingLocalProjections.delete(key);
  }
  return applied;
}

export function removeLocalFreezoneProjection(
  project: string,
  canvasId: string,
  projectionKey: string,
): boolean {
  if (
    !currentRuntime ||
    currentRuntime.project !== project ||
    currentRuntime.canvasId !== canvasId ||
    !currentRuntime.removeLocalProjection
  ) {
    return false;
  }
  return currentRuntime.removeLocalProjection(projectionKey);
}

/** Capture the registered canvas identity before an asynchronous artifact save.
 * A different registration (even the same URL after leaving) must not receive
 * nodes from a previous canvas session.
 */
export function captureFreezoneCanvasScope(project: string, canvasId: string): () => boolean {
  const runtime = currentRuntime;
  return () => Boolean(runtime && currentRuntime === runtime && runtime.project === project && runtime.canvasId === canvasId);
}
