// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * Asynchronous IndexedDB persistence for large canvas drafts and undo/redo
 * history stacks. Eliminates synchronous `localStorage` main-thread stalls and
 * the ~1.5-5MB origin quota ceiling when canvases grow to dozens/hundreds of
 * nodes. Falls back cleanly (returns null/false) in environments without
 * IndexedDB (e.g., jsdom tests or private browsing).
 */

const DB_NAME = "supertale-freezone-v1";
const DB_VERSION = 1;
const DRAFTS_STORE = "drafts";
const HISTORY_STORE = "history";

type StoreName = typeof DRAFTS_STORE | typeof HISTORY_STORE;

let dbPromise: Promise<IDBDatabase | null> | null = null;

export function hasIndexedDbSupport(): boolean {
  try {
    return typeof globalThis !== "undefined" && typeof globalThis.indexedDB !== "undefined";
  } catch {
    return false;
  }
}

function openDb(): Promise<IDBDatabase | null> {
  if (!hasIndexedDbSupport()) {
    return Promise.resolve(null);
  }
  if (dbPromise) {
    return dbPromise;
  }
  dbPromise = new Promise<IDBDatabase | null>((resolve) => {
    try {
      const request = globalThis.indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(DRAFTS_STORE)) {
          db.createObjectStore(DRAFTS_STORE);
        }
        if (!db.objectStoreNames.contains(HISTORY_STORE)) {
          db.createObjectStore(HISTORY_STORE);
        }
      };
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => {
          db.close();
          dbPromise = null;
        };
        resolve(db);
      };
      request.onerror = () => {
        dbPromise = null;
        resolve(null);
      };
      request.onblocked = () => {
        resolve(null);
      };
    } catch {
      dbPromise = null;
      resolve(null);
    }
  });
  return dbPromise;
}

async function idbGet<T>(storeName: StoreName, key: string): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  return await new Promise<T | null>((resolve) => {
    try {
      const tx = db.transaction(storeName, "readonly");
      const req = tx.objectStore(storeName).get(key);
      req.onsuccess = () => resolve((req.result as T | undefined) ?? null);
      req.onerror = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
}

async function idbPut(storeName: StoreName, key: string, value: unknown): Promise<boolean> {
  const db = await openDb();
  if (!db) return false;
  return await new Promise<boolean>((resolve) => {
    try {
      const tx = db.transaction(storeName, "readwrite");
      tx.objectStore(storeName).put(value, key);
      tx.oncomplete = () => resolve(true);
      tx.onerror = () => resolve(false);
      tx.onabort = () => resolve(false);
    } catch {
      resolve(false);
    }
  });
}

async function idbDelete(storeName: StoreName, key: string): Promise<void> {
  const db = await openDb();
  if (!db) return;
  await new Promise<void>((resolve) => {
    try {
      const tx = db.transaction(storeName, "readwrite");
      tx.objectStore(storeName).delete(key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
    } catch {
      resolve();
    }
  });
}

async function pruneStore(storeName: StoreName, now: number, ttlMs: number): Promise<void> {
  const db = await openDb();
  if (!db) return;
  await new Promise<void>((resolve) => {
    try {
      const tx = db.transaction(storeName, "readwrite");
      const store = tx.objectStore(storeName);
      const req = store.openCursor();
      req.onsuccess = () => {
        const cursor = req.result;
        if (!cursor) return;
        const val = cursor.value as { updatedAt?: unknown } | null | undefined;
        const updatedAt = typeof val?.updatedAt === "number" ? val.updatedAt : null;
        if (updatedAt === null || now - updatedAt > ttlMs) {
          cursor.delete();
        }
        cursor.continue();
      };
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
    } catch {
      resolve();
    }
  });
}

export function idbCanvasKey(project: string, canvasId: string): string {
  return `${encodeURIComponent(project)}:${encodeURIComponent(canvasId)}`;
}

export async function readIdbDraft<T>(project: string, canvasId: string): Promise<T | null> {
  return await idbGet<T>(DRAFTS_STORE, idbCanvasKey(project, canvasId));
}

export async function writeIdbDraft(project: string, canvasId: string, draft: unknown): Promise<boolean> {
  return await idbPut(DRAFTS_STORE, idbCanvasKey(project, canvasId), draft);
}

export async function deleteIdbDraft(project: string, canvasId: string): Promise<void> {
  await idbDelete(DRAFTS_STORE, idbCanvasKey(project, canvasId));
}

export async function readIdbHistory<T>(project: string, canvasId: string): Promise<T | null> {
  return await idbGet<T>(HISTORY_STORE, idbCanvasKey(project, canvasId));
}

export async function writeIdbHistory(project: string, canvasId: string, history: unknown): Promise<boolean> {
  return await idbPut(HISTORY_STORE, idbCanvasKey(project, canvasId), history);
}

export async function deleteIdbHistory(project: string, canvasId: string): Promise<void> {
  await idbDelete(HISTORY_STORE, idbCanvasKey(project, canvasId));
}

export async function pruneIdbCanvasStorage(now: number, ttlMs: number): Promise<void> {
  if (!hasIndexedDbSupport()) return;
  await Promise.all([
    pruneStore(DRAFTS_STORE, now, ttlMs),
    pruneStore(HISTORY_STORE, now, ttlMs),
  ]);
}
