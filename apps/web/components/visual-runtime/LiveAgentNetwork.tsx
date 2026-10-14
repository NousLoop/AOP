"use client";

import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { PreflightAgent, VisualGraphEdge, VisualGraphNode, VisualGraphSnapshot } from "@/lib/api";
import { reactFlowOnError, useClientMounted } from "@/lib/reactFlow";

const COL_W = 200;
const ROW_H = 140;

function failSubtitle(status: string, error?: string): string {
  const e = (error || "").trim();
  if (!e) return status;
  if (e.includes("余额不足") || /insufficient balance/i.test(e) || /api error:\s*402/i.test(e)) {
    return "余额不足";
  }
  return e.length > 22 ? `${e.slice(0, 22)}…` : e;
}

function statusColor(status: string): string {
  const s = (status || "").toLowerCase();
  if (s === "running" || s === "active") return "#3dffa8";
  if (s === "waiting" || s === "waiting_for_user" || s === "retrying") return "#ffb454";
  if (s === "waiting_for_agent") return "#7dd3fc";
  if (s === "failed" || s === "error" || s === "timeout") return "#ff6b6b";
  if (s === "completed" || s === "succeeded") return "#7eaea0";
  if (s === "discovered") return "#8ab4ff";
  return "#5a7a70";
}

function matchReadiness(
  node: VisualGraphNode,
  readiness: PreflightAgent[] | undefined,
): PreflightAgent | undefined {
  if (!readiness?.length) return undefined;
  const id = (node.agent_id || node.label || "").toLowerCase();
  return readiness.find((a) => {
    const key = (a.agent_key || "").toLowerCase();
    return key && (id === key || id.includes(key) || key.includes(id));
  });
}

function VrNode({ data }: NodeProps) {
  const kind = String(data.kind || "agent");
  const status = String(data.status || "pending");
  const label = String(data.label || "");
  const ready = data.runnerReady as boolean | undefined;
  const hint = typeof data.runnerHint === "string" ? data.runnerHint : "";
  const err = typeof data.error === "string" ? data.error : "";
  const color = statusColor(status);
  const running =
    data.status === "running" || data.status === "active";
  const waiting =
    status === "waiting" ||
    status === "waiting_for_user" ||
    status === "waiting_for_agent" ||
    status === "retrying";
  const size =
    kind === "task" ? 72 : kind === "tool" || kind === "data" ? 40 : kind === "error" ? 48 : 56;
  const radius = kind === "data" ? 6 : kind === "tool" ? 8 : "50%";
  const failed = status === "failed" || status === "error" || status === "timeout";
  const subtitle = failed
    ? failSubtitle(status, err)
    : kind === "agent" && ready === false
      ? hint
        ? hint.length > 22
          ? `${hint.slice(0, 22)}…`
          : hint
        : "stub"
      : status;

  return (
    <div className="relative flex flex-col items-center" style={{ width: size + 48 }}>
      <Handle type="target" position={Position.Top} className="!h-1.5 !w-1.5 !border-0 !bg-mist-400" />
      <div
        className={
          running
            ? "vr-node-running relative flex items-center justify-center border"
            : "relative flex items-center justify-center border"
        }
        style={{
          width: size,
          height: size,
          borderRadius: radius,
          borderColor: ready === false ? "#ffb454" : color,
          borderStyle: waiting || ready === false ? "dashed" : "solid",
          borderWidth: kind === "task" ? 2 : 1.5,
          background:
            kind === "error"
              ? "rgba(255,107,107,0.12)"
              : kind === "task"
                ? "rgba(61,255,168,0.08)"
                : "rgba(15,28,24,0.85)",
          boxShadow: running ? `0 0 24px ${color}44` : "none",
        }}
      >
        <span
          className="font-mono text-[9px] uppercase tracking-[0.14em]"
          style={{ color: ready === false ? "#ffb454" : color }}
        >
          {kind === "task" ? "TASK" : kind === "tool" ? "TOOL" : kind === "error" ? "ERR" : kind === "data" ? "DATA" : "AGT"}
        </span>
      </div>
      <div className="mt-2 max-w-[140px] truncate text-center font-sans text-[11px] text-mist-100">
        {label}
      </div>
      <div
        className="max-w-[140px] truncate text-center font-mono text-[9px] uppercase tracking-wider"
        style={{ color: ready === false ? "#ffb454" : color }}
        title={hint || status}
      >
        {subtitle}
      </div>
      <Handle type="source" position={Position.Bottom} className="!h-1.5 !w-1.5 !border-0 !bg-mist-400" />
    </div>
  );
}

const MemoVrNode = memo(VrNode);
const NODE_TYPES = Object.freeze({ vr: MemoVrNode }) as unknown as NodeTypes;

function isDispatcher(n: VisualGraphNode): boolean {
  const id = (n.agent_id || n.label || n.id || "").toLowerCase();
  return id === "orchestrator" || id.endsWith(":orchestrator") || n.label === "orchestrator";
}

function layout(
  snapshot: VisualGraphSnapshot | null,
  readiness?: PreflightAgent[],
): { nodes: Node[]; edges: Edge[] } {
  const rawNodes = snapshot?.nodes || [];
  const rawEdges = snapshot?.edges || [];
  if (!rawNodes.length) {
    return {
      nodes: [
        {
          id: "idle-task",
          type: "vr",
          position: { x: 280, y: 180 },
          data: { kind: "task", label: "A2A OS", status: "idle" },
        },
      ],
      edges: [],
    };
  }

  const task = rawNodes.find((n) => n.type === "task");
  const agents = rawNodes.filter((n) => n.type === "agent" && !isDispatcher(n));
  const others = rawNodes.filter(
    (n) => n.type !== "task" && n.type !== "agent" && n.type !== "error",
  );

  const positioned: Node[] = [];
  if (task) {
    positioned.push({
      id: task.id,
      type: "vr",
      position: { x: Math.max(0, (agents.length - 1) * (COL_W / 2)), y: 40 },
      data: {
        kind: "task",
        label: task.label,
        status: task.status,
        meta: task,
        error: (task.metadata as { error?: string } | undefined)?.error,
      },
    });
  }

  agents.forEach((a, i) => {
    const row = Math.floor(i / 5);
    const col = i % 5;
    const pf = matchReadiness(a, readiness);
    positioned.push({
      id: a.id,
      type: "vr",
      position: { x: col * COL_W, y: 200 + row * ROW_H },
      data: {
        kind: "agent",
        label: a.label,
        status: a.status,
        meta: a,
        error: (a.metadata as { error?: string } | undefined)?.error,
        visit: (a.metadata as { visit_count?: number } | undefined)?.visit_count,
        runnerReady: pf ? pf.runner_ready : undefined,
        runnerHint: pf && !pf.runner_ready ? pf.runner_reason || (pf.reachable ? "stub" : "offline") : "",
      },
    });
  });

  others.forEach((o, i) => {
    positioned.push({
      id: o.id,
      type: "vr",
      position: { x: (i % 6) * 160, y: 200 + Math.ceil(agents.length / 5) * ROW_H + 120 },
      data: {
        kind: o.type,
        label: o.label,
        status: o.status,
        meta: o,
        error: (o.metadata as { error?: string } | undefined)?.error,
      },
    });
  });

  const idSet = new Set(positioned.map((n) => n.id));
  const taskId = task?.id;
  const remapped = rawEdges.map((e) => {
    let source = e.source;
    let target = e.target;
    if (source.toLowerCase().includes("orchestrator") && taskId) source = taskId;
    if (target.toLowerCase().includes("orchestrator") && taskId) target = taskId;
    return { ...e, source, target };
  });
  const edges: Edge[] = remapped
    .filter((e) => idSet.has(e.source) && idSet.has(e.target) && e.source !== e.target)
    .map((e: VisualGraphEdge) => {
      const active = (e.status || "").toLowerCase() === "active" || (e.status || "") === "running";
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        animated: active || e.type === "delegation",
        style: {
          stroke: statusColor(e.status || "completed"),
          strokeWidth: active ? 2 : 1.25,
          opacity: active ? 0.95 : 0.55,
        },
        label: e.type !== "delegation" ? e.type : undefined,
        labelStyle: { fill: "#7eaea0", fontSize: 9 },
      };
    });

  return { nodes: positioned, edges };
}

type Props = {
  snapshot: VisualGraphSnapshot | null;
  selectedId?: string | null;
  onSelect?: (node: VisualGraphNode | null) => void;
  replayCursor?: number | null;
  readiness?: PreflightAgent[];
};

export function LiveAgentNetwork({ snapshot, selectedId, onSelect, replayCursor, readiness }: Props) {
  const mounted = useClientMounted();
  const nodeCount = snapshot?.nodes?.length || 0;
  const [rf, setRf] = useState<ReactFlowInstance | null>(null);
  const view = useMemo(() => {
    if (replayCursor == null || !snapshot?.events?.length) return snapshot;
    // Replay projects only events up to cursor — UI-only, no re-execution
    const allowed = new Set(
      snapshot.events
        .filter((e) => (e.sequence ?? 0) <= replayCursor)
        .flatMap((e) => {
          const ids: string[] = [`task:${snapshot.task_id}`];
          if (e.agent_id) ids.push(`agent:${e.agent_id}`);
          return ids;
        }),
    );
    const nodes = (snapshot.nodes || []).filter(
      (n) => n.type === "task" || allowed.has(n.id) || (n.agent_id && allowed.has(`agent:${n.agent_id}`)),
    );
    const nodeIds = new Set(nodes.map((n) => n.id));
    const edges = (snapshot.edges || []).filter(
      (e) => nodeIds.has(e.source) && nodeIds.has(e.target),
    );
    return { ...snapshot, nodes, edges };
  }, [snapshot, replayCursor]);

  const { nodes, edges } = useMemo(() => layout(view, readiness), [view, readiness]);
  const [rfNodes, setRfNodes] = useState<Node[]>(nodes);
  const [rfEdges, setRfEdges] = useState<Edge[]>(edges);

  useEffect(() => {
    setRfNodes(
      nodes.map((n) => ({
        ...n,
        selected: n.id === selectedId,
        style: n.id === selectedId ? { outline: "1px solid #3dffa8" } : undefined,
      })),
    );
    setRfEdges(edges);
  }, [nodes, edges, selectedId]);

  useEffect(() => {
    if (!rf || nodeCount === 0) return;
    const t = setTimeout(() => {
      rf.fitView({ padding: 0.35, minZoom: 0.35, maxZoom: 1.4 });
    }, 50);
    return () => clearTimeout(t);
  }, [rf, nodeCount, snapshot?.sequence]);

  const onNodeClick = useCallback(
    (_: unknown, node: Node) => {
      const meta = (node.data as { meta?: VisualGraphNode }).meta || null;
      onSelect?.(meta);
    },
    [onSelect],
  );

  if (!mounted) {
    return (
      <div className="flex h-full items-center justify-center font-mono text-xs text-mist-400">
        initializing network…
      </div>
    );
  }

  return (
    <div className="h-full w-full">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.35, minZoom: 0.35, maxZoom: 1.4 }}
        minZoom={0.2}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
        onInit={setRf}
        onNodeClick={onNodeClick}
        onPaneClick={() => onSelect?.(null)}
        onError={reactFlowOnError}
        nodesDraggable
        elementsSelectable
      >
        <Background gap={28} size={1} color="rgba(232,242,238,0.06)" />
        <Controls
          showInteractive={false}
          className="!overflow-hidden !rounded-lg !border !border-white/10 !bg-ink-900/90"
        />
      </ReactFlow>
    </div>
  );
}
