// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useSyncExternalStore } from "react";

import {
  fetchFreezoneAudioModels,
  type FreezoneAudioModelInfo,
} from "@/api/ops";
import { readUrl } from "@/lib/url-params";

export interface UseFreezoneAudioModelsResult {
  models: FreezoneAudioModelInfo[];
  isLoading: boolean;
  error: Error | null;
}

const states = new Map<string, UseFreezoneAudioModelsResult>();
const listeners = new Map<string, Set<() => void>>();
const inFlightProjects = new Set<string>();
const pendingRefreshProjects = new Set<string>();

const EMPTY_STATE: UseFreezoneAudioModelsResult = {
  models: [],
  isLoading: false,
  error: null,
};

function emit(project: string) {
  listeners.get(project)?.forEach((fn) => fn());
}

function writeState(project: string, next: UseFreezoneAudioModelsResult) {
  states.set(project, next);
  emit(project);
}

function ensureLoaded(project: string, force = false) {
  if (inFlightProjects.has(project)) {
    if (force) pendingRefreshProjects.add(project);
    return;
  }
  if (!force && states.has(project)) return;
  inFlightProjects.add(project);
  const current = states.get(project);
  states.set(
    project,
    current ? { ...current, isLoading: true, error: null } : { ...EMPTY_STATE, isLoading: true },
  );
  fetchFreezoneAudioModels(project)
    .then((models) => {
      writeState(project, {
        models,
        isLoading: false,
        error: null,
      });
    })
    .catch((error: unknown) => {
      const normalized = error instanceof Error ? error : new Error(String(error));
      writeState(project, {
        models: current?.models ?? [],
        isLoading: false,
        error: normalized,
      });
    })
    .finally(() => {
      inFlightProjects.delete(project);
      if (pendingRefreshProjects.delete(project)) ensureLoaded(project, true);
    });
}

export function getFreezoneAudioModelsSnapshot(
  projectOverride?: string | null,
): UseFreezoneAudioModelsResult {
  const project =
    projectOverride !== undefined ? projectOverride : readUrl().project;
  if (!project) return EMPTY_STATE;
  ensureLoaded(project);
  return states.get(project) ?? EMPTY_STATE;
}

function refreshKnownAudioModelCatalogs() {
  const projects = new Set(states.keys());
  const currentProject = readUrl().project;
  if (currentProject) projects.add(currentProject);
  projects.forEach((project) => ensureLoaded(project, true));
}

if (typeof window !== "undefined") {
  window.addEventListener("media-model-catalog-updated", refreshKnownAudioModelCatalogs);
  import.meta.hot?.dispose(() => {
    window.removeEventListener(
      "media-model-catalog-updated",
      refreshKnownAudioModelCatalogs,
    );
  });
}

function subscribe(project: string | null, callback: () => void) {
  if (!project) return () => {};
  let bucket = listeners.get(project);
  if (!bucket) {
    bucket = new Set();
    listeners.set(project, bucket);
  }
  bucket.add(callback);
  return () => {
    bucket!.delete(callback);
    if (bucket!.size === 0) listeners.delete(project);
  };
}

export function useFreezoneAudioModels(
  projectOverride?: string | null,
): UseFreezoneAudioModelsResult {
  const project =
    projectOverride !== undefined ? projectOverride : readUrl().project;
  useEffect(() => {
    if (project) ensureLoaded(project);
  }, [project]);
  return useSyncExternalStore(
    (cb) => subscribe(project, cb),
    () => getFreezoneAudioModelsSnapshot(project),
    () => EMPTY_STATE,
  );
}
