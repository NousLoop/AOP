"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getTaskVisualGraph,
  type VisualGraphSnapshot,
  type VisualRuntimeEvent,
} from "@/lib/api";
import { useWebSocket, type WebSocketMessage } from "@/hooks/useWebSocket";
import {
  GRAPH_POLL_MS,
  GRAPH_POLL_MS_WHEN_WS,
  pageVisible,
} from "@/lib/livePoll";

type VisualWsMessage = WebSocketMessage & {
  root_task_id?: string;
  sequence?: number;
  graph?: VisualGraphSnapshot;
  events?: VisualRuntimeEvent[];
  event?: VisualRuntimeEvent;
};

const TERMINAL = new Set(["completed", "failed", "cancelled", "canceled"]);

function isTerminalStatus(status: string | undefined | null): boolean {
  return TERMINAL.has(String(status || "").toLowerCase());
}

/**
 * Visual Runtime live state: Snapshot + incremental events.
 * HTTP graph (Postgres SoT) is authoritative; WS is acceleration only.
 */
export function useVisualRuntime(taskId: string | null) {
  const [graph, setGraph] = useState<VisualGraphSnapshot | null>(null);
  const [events, setEvents] = useState<VisualRuntimeEvent[]>([]);
  const [sequence, setSequence] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const seenIds = useRef<Set<string>>(new Set());
  const lastSeq = useRef(0);
  const graphRef = useRef<VisualGraphSnapshot | null>(null);
  const connectedRef = useRef(false);
  const wantIdRef = useRef<string | null>(null);

  const applySnapshot = useCallback((snap: VisualGraphSnapshot) => {
    const want = wantIdRef.current;
    if (want && snap.task_id && snap.task_id !== want) return;
    const prev = graphRef.current;
    const nextSeq = snap.sequence || 0;
    if (
      prev &&
      prev.task_id === snap.task_id &&
      (prev.nodes?.length || 0) > 0 &&
      (snap.nodes?.length || 0) === 0 &&
      nextSeq <= (prev.sequence || 0)
    ) {
      return;
    }
    if (
      prev &&
      prev.task_id === snap.task_id &&
      nextSeq < (prev.sequence || 0) &&
      !isTerminalStatus(snap.status)
    ) {
      return;
    }
    graphRef.current = snap;
    setGraph(snap);
    const evs = snap.events || [];
    seenIds.current = new Set(
      evs.map((e) => e.event_id).filter((id): id is string => !!id),
    );
    lastSeq.current = snap.sequence || 0;
    setEvents(evs);
    setSequence(snap.sequence || 0);
  }, []);

  const onMessage = useCallback(
    (message: WebSocketMessage) => {
      const data = message as VisualWsMessage;
      const type = data.type;
      if ((type === "snapshot" || type === "graph") && data.graph) {
        applySnapshot(data.graph);
        return;
      }
      if (type === "event" && data.event) {
        const ev = data.event;
        const seq = ev.sequence ?? data.sequence;
        if (typeof seq === "number" && seq <= lastSeq.current && seenIds.current.has(ev.event_id || "")) {
          return;
        }
        if (ev.event_id && seenIds.current.has(ev.event_id)) return;
        if (ev.event_id) seenIds.current.add(ev.event_id);
        if (typeof seq === "number") {
          lastSeq.current = Math.max(lastSeq.current, seq);
          setSequence(lastSeq.current);
        }
        setEvents((prev) => [...prev, ev]);
      }
    },
    [applySnapshot],
  );

  const path = taskId
    ? `/ws/tasks/${encodeURIComponent(taskId)}`
    : undefined;

  const { isConnected } = useWebSocket({
    path,
    enabled: !!taskId,
    onMessage,
    maxReconnectAttempts: 40,
    reconnectInterval: 2000,
  });
  connectedRef.current = isConnected;

  const refresh = useCallback(async () => {
    if (!taskId) return;
    const snap = await getTaskVisualGraph(taskId, { bust: true });
    applySnapshot(snap);
  }, [taskId, applySnapshot]);

  useEffect(() => {
    wantIdRef.current = taskId;
    if (!taskId) {
      seenIds.current = new Set();
      lastSeq.current = 0;
      graphRef.current = null;
      setGraph(null);
      setEvents([]);
      setSequence(0);
      setError(null);
      return;
    }

    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let terminalSettle = false;

    const schedule = (bust: boolean) => {
      const interval = connectedRef.current
        ? GRAPH_POLL_MS_WHEN_WS
        : GRAPH_POLL_MS;
      timer = setTimeout(() => void tick(bust), interval);
    };

    const tick = async (bust: boolean) => {
      if (!alive) return;
      if (!pageVisible()) {
        schedule(false);
        return;
      }
      try {
        const snap = await getTaskVisualGraph(taskId, bust ? { bust: true } : undefined);
        if (!alive) return;
        applySnapshot(snap);
        setError(null);
        const done = isTerminalStatus(snap.status || snap.task?.status);
        if (done && (snap.nodes?.length || 0) > 0) {
          if (terminalSettle) return;
          terminalSettle = true;
          // One settle refresh, then stop polling terminal graphs.
          timer = setTimeout(() => void tick(true), GRAPH_POLL_MS_WHEN_WS);
          return;
        }
      } catch (err) {
        if (alive && (!graphRef.current || graphRef.current.task_id !== taskId)) {
          setError(err instanceof Error ? err.message : "graph load failed");
        }
      }
      if (!alive) return;
      schedule(false);
    };

    const onVis = () => {
      if (!document.hidden && alive) {
        if (timer) clearTimeout(timer);
        void tick(true);
      }
    };
    document.addEventListener("visibilitychange", onVis);

    void tick(false);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [taskId, applySnapshot]);

  const stats = useMemo(() => {
    const nodes = graph?.nodes || [];
    const agents = nodes.filter((n) => n.type === "agent");
    const retries = events.filter(
      (e) =>
        e.event_type.includes("retry") ||
        e.original_event_type?.includes("retry"),
    ).length;
    return {
      agents: agents.length,
      executions: nodes.filter((n) => n.type === "execution" || n.execution_id).length || agents.length,
      retries,
      tools: nodes.filter((n) => n.type === "tool").length,
    };
  }, [graph, events]);

  const displayStatus =
    graph?.task?.status && isTerminalStatus(graph.task.status)
      ? String(graph.task.status).toLowerCase() === "canceled"
        ? "cancelled"
        : String(graph.task.status).toLowerCase()
      : graph?.status;

  return {
    graph: graph
      ? { ...graph, status: displayStatus || graph.status }
      : null,
    events,
    sequence,
    isConnected,
    error,
    stats,
    refresh,
    setGraph,
  };
}
