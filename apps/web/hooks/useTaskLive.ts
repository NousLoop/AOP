"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getTask,
  getTaskArtifacts,
  getTaskEvaluation,
  getTaskEvents,
  invalidateTaskCache,
  type ArtifactItem,
  type MemoryEntry,
  type TaskDetail,
  type TaskEvaluation,
  type TaskEvent,
} from "@/lib/api";
import {
  pageVisible,
  TASK_POLL_MS,
  TASK_POLL_MS_WHEN_WS,
} from "@/lib/livePoll";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export type UseTaskLiveOptions = {
  /** True when TaskNetworkPanel / useVisualRuntime WS is connected. */
  graphWsConnected?: boolean;
};

/**
 * HTTP poll for task drawer.
 * Graph WS lives in useVisualRuntime — do not open a second socket.
 * When graph WS is healthy, slow to reconcile-only cadence.
 */
export function useTaskLive(
  taskId: string | null,
  opts?: UseTaskLiveOptions,
) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactItem[]>([]);
  const [evaluation, setEvaluation] = useState<TaskEvaluation | null>(null);
  const [memories] = useState<MemoryEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const aliveRef = useRef(true);
  const shownIdRef = useRef<string | null>(null);
  const wsRef = useRef(!!opts?.graphWsConnected);
  wsRef.current = !!opts?.graphWsConnected;

  const refresh = useCallback(async (id: string, extras = true) => {
    const [t, e] = await Promise.all([getTask(id), getTaskEvents(id)]);
    if (!aliveRef.current) return t;
    shownIdRef.current = id;
    setTask(t);
    setEvents(e.events || []);
    setError(null);
    if (!extras) return t;

    const jobs: Promise<void>[] = [
      getTaskArtifacts(id)
        .then((a) => {
          if (aliveRef.current && shownIdRef.current === id) {
            setArtifacts(a.artifacts || []);
          }
        })
        .catch(() => undefined),
    ];
    if (TERMINAL.has(t.status)) {
      jobs.push(
        getTaskEvaluation(id)
          .then((ev) => {
            if (aliveRef.current && shownIdRef.current === id) setEvaluation(ev);
          })
          .catch(() => {
            if (aliveRef.current && shownIdRef.current === id) setEvaluation(null);
          }),
      );
    } else if (aliveRef.current && shownIdRef.current === id) {
      setEvaluation(null);
    }
    await Promise.all(jobs);
    return t;
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    if (!taskId) {
      shownIdRef.current = null;
      setTask(null);
      setEvents([]);
      setArtifacts([]);
      setEvaluation(null);
      setError(null);
      return;
    }

    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      const ms = wsRef.current ? TASK_POLL_MS_WHEN_WS : TASK_POLL_MS;
      timer = setTimeout(() => void tick(), ms);
    };

    async function tick() {
      if (!aliveRef.current) return;
      if (!pageVisible()) {
        schedule();
        return;
      }
      try {
        const t = await refresh(taskId!);
        if (!aliveRef.current) return;
        if (TERMINAL.has(t.status)) return;
      } catch (err) {
        if (aliveRef.current) {
          setError(err instanceof Error ? err.message : "poll failed");
        }
      }
      if (aliveRef.current) schedule();
    }

    const onVis = () => {
      if (!document.hidden && aliveRef.current) {
        if (timer) clearTimeout(timer);
        void tick();
      }
    };
    document.addEventListener("visibilitychange", onVis);

    void tick();
    return () => {
      aliveRef.current = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [taskId, nonce, refresh]);

  return {
    task,
    events,
    artifacts,
    evaluation,
    memories,
    error,
    setTask,
    reload: () => {
      if (taskId) invalidateTaskCache(taskId);
      setNonce((n) => n + 1);
    },
  };
}
