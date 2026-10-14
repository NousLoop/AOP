"use client";

import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { TaskNode, TaskPlan } from "@/lib/api";
import { reactFlowOnError, useClientMounted } from "@/lib/reactFlow";
import { STATUS_COLOR, flowStatusLabel, flowVisualStatus } from "./taskFlowStatus";
import {
  TASK_FLOW_EDGE_TYPES,
  TASK_FLOW_NODE_TYPES,
  type DagNodeData,
} from "./taskFlowTypes";

/** Friendly labels for work agents (plan hop is agent_key). */
const AGENT_LABEL: Record<string, string> = {
  "claude-code": "Claude Code",
  "deepseek-harness": "DeepSeek",
  openclaw: "OpenClaw",
  hermes: "Hermes",
  pi: "Pi",
  "aopd-claude-code": "aopd Claude Code",
  "aopd-deepseek-harness": "aopd DeepSeek Harness",
  "aopd-openclaw": "aopd OpenClaw",
  "aopd-hermes": "aopd Hermes",
  "aopd-pi": "aopd Pi",
};

export function hopAgentKey(node: {
  id?: string;
  agent_key?: string | null;
  agent_name?: string | null;
}): string {
  const assigned = (node.agent_key || "").trim();
  if (assigned) return assigned;
  return (node.agent_name || node.id || "agent").trim();
}

export function agentDisplayName(key?: string | null, fallbackName?: string | null): string {
  const k = (key || "").trim();
  if (fallbackName && fallbackName.trim()) return fallbackName.trim();
  if (!k) return "Agent";
  return AGENT_LABEL[k] || k;
}

function minimapNodeColor(n: Node) {
  const st = (n.data as DagNodeData | undefined)?.status;
  return STATUS_COLOR[st || ""] || "#7eaea0";
}

type Props = {
  plan?: TaskPlan | null;
  nodes: TaskNode[];
  selectedNodeId?: string | null;
  onSelectNode?: (id: string | null) => void;
};

function buildLayout(plan: TaskPlan | null | undefined, nodes: TaskNode[]) {
  const planNodes =
    plan?.nodes ||
    nodes.map((n) => ({
      id: n.id,
      agent_key: n.agent_key,
      depends_on: [] as string[],
    }));
  const ids = planNodes.map((n) => n.id);
  const deps = new Map<string, string[]>();
  for (const n of planNodes) deps.set(n.id, n.depends_on || []);

  const depth = new Map<string, number>();
  function d(id: string, stack = new Set<string>()): number {
    if (depth.has(id)) return depth.get(id)!;
    if (stack.has(id)) return 0;
    stack.add(id);
    const parents = deps.get(id) || [];
    const val = parents.length ? 1 + Math.max(...parents.map((p) => d(p, stack))) : 0;
    stack.delete(id);
    depth.set(id, val);
    return val;
  }
  ids.forEach((id) => d(id));

  const layers = new Map<number, string[]>();
  for (const id of ids) {
    const layer = depth.get(id) || 0;
    const arr = layers.get(layer) || [];
    arr.push(id);
    layers.set(layer, arr);
  }

  const statusMap = new Map(nodes.map((n) => [n.id, n]));
  const planAgent = new Map(
    planNodes.map((n) => [n.id, n.agent_key || ""]),
  );

  const rfNodes: Node<DagNodeData>[] = [];
  const rfEdges: Edge[] = [];
  const rowH = 130;
  const colW = 230;

  for (const [layer, layerIds] of layers) {
    layerIds.forEach((id, idx) => {
      const live = statusMap.get(id);
      const st = live?.status || "pending";
      const err = live?.error_message || live?.output_excerpt;
      const visual = flowVisualStatus(st, err);
      const agentKey = hopAgentKey({
        id,
        agent_key: live?.agent_key || planAgent.get(id),
        agent_name: live?.agent_name,
      });
      rfNodes.push({
        id,
        type: "dagNode",
        position: { x: 40 + idx * colW, y: 40 + layer * rowH },
        data: {
          nodeId: id,
          agentKey,
          agentLabel: agentDisplayName(agentKey, live?.agent_name),
          status: visual,
          statusLabel: flowStatusLabel(st, err),
          attempt: live?.attempt,
        },
        selected: false,
      });
    });
  }

  for (const n of planNodes) {
    for (const parent of n.depends_on || []) {
      const childStatus = statusMap.get(n.id)?.status;
      rfEdges.push({
        id: `${parent}-${n.id}`,
        source: parent,
        target: n.id,
        animated: childStatus === "running" || childStatus === "ready",
        style: { stroke: "#7eaea0" },
        markerEnd: { type: MarkerType.ArrowClosed, color: "#7eaea0" },
      });
    }
  }

  return { rfNodes, rfEdges };
}

const LEGEND = [
  ["ready", "就绪"],
  ["running", "执行中"],
  ["success", "完成"],
  ["waiting_for_user", "待系统审"],
  ["waiting_for_agent", "待 Agent 审"],
  ["failed", "失败"],
] as const;

export const TaskFlowDag = memo(function TaskFlowDag({
  plan,
  nodes,
  selectedNodeId,
  onSelectNode,
}: Props) {
  const layout = useMemo(() => buildLayout(plan, nodes), [plan, nodes]);
  const [rfNodes, setNodes, onNodesChange] = useNodesState(layout.rfNodes);
  const [rfEdges, setEdges, onEdgesChange] = useEdgesState(layout.rfEdges);

  useEffect(() => {
    setNodes(layout.rfNodes);
    setEdges(layout.rfEdges);
  }, [layout, setNodes, setEdges]);

  useEffect(() => {
    setNodes((prev) =>
      prev.map((n) => ({
        ...n,
        selected: n.id === selectedNodeId,
        style:
          n.id === selectedNodeId
            ? { ...n.style, outline: "2px solid #3dffa8", outlineOffset: 2 }
            : { ...n.style, outline: undefined },
      })),
    );
  }, [selectedNodeId, setNodes]);

  const onNodeClick = useCallback(
    (_: unknown, node: Node) => {
      onSelectNode?.(node.id);
    },
    [onSelectNode],
  );

  const onPaneClick = useCallback(() => {
    onSelectNode?.(null);
  }, [onSelectNode]);

  // Pin identities for this instance (survives parent re-renders / HMR churn).
  const nodeTypes = useRef(TASK_FLOW_NODE_TYPES).current;
  const edgeTypes = useRef(TASK_FLOW_EDGE_TYPES).current;
  const mounted = useClientMounted();

  return (
    <div className="relative h-full min-h-[360px] w-full overflow-hidden rounded-xl border border-white/10 bg-ink-950/40">
      <div className="pointer-events-none absolute left-3 top-3 z-10 flex flex-wrap gap-2">
        {LEGEND.map(([key, label]) => (
          <span
            key={key}
            className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-ink-900/80 px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.12em] text-mist-300"
          >
            <span
              className="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: STATUS_COLOR[key] }}
            />
            {label}
          </span>
        ))}
      </div>
      {mounted ? (
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          fitView
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          onError={reactFlowOnError}
        >
          <Background color="#3dffa822" gap={22} />
          <Controls showInteractive={false} />
          <MiniMap nodeColor={minimapNodeColor} maskColor="rgba(10,18,16,0.7)" />
        </ReactFlow>
      ) : null}
    </div>
  );
});
