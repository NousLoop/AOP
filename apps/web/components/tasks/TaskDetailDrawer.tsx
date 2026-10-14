"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  downloadTaskPackage,
  listTaskCheckpoints,
  type ArtifactItem,
  type NodeCheckpoint,
  type TaskDetail,
  type TaskEvent,
  type MemoryEntry,
  type TaskEvaluation,
} from "@/lib/api";
import { agentDisplayName, hopAgentKey } from "@/components/tasks/TaskFlowDag";
import { flowStatusLabel, isBalanceShortage } from "@/components/tasks/taskFlowStatus";
import { TaskTrace } from "@/components/TaskTrace";
import { PptPreview } from "@/components/tasks/PptPreview";
import { ExecutionPanel } from "@/components/tasks/ExecutionPanel";
import { CostPanel } from "@/components/tasks/CostPanel";

const DIMENSION_LABELS: Record<string, string> = {
  structure: "结构",
  prose: "行文",
  citations: "引用",
  layout: "版式",
  execution: "执行",
  completeness: "完整",
  reliability: "可靠",
  artifacts: "产物",
  latency: "耗时",
};

const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  waiting_for_user: "待系统审",
  waiting_for_agent: "待 Agent 审",
  pending: "等待",
  created: "已创建",
};

function isReportSkill(skill: string) {
  return skill.toLowerCase().includes("report");
}

function isPptSkill(skill: string) {
  const s = skill.toLowerCase();
  return s === "ppt" || s === "ppt-generation" || s.includes("ppt-generation");
}

function isWideDetail(skill: string) {
  return isReportSkill(skill) || isPptSkill(skill);
}

function isPptxName(name: string | undefined) {
  const n = (name || "").replace(/\\/g, "/").toLowerCase();
  return n.endsWith(".pptx") || n.endsWith("deck.pptx") || n.endsWith("report.pptx");
}

function statusTone(status?: string, error?: string | null) {
  if (isBalanceShortage(error) || status === "failed") {
    return "text-red-400";
  }
  switch (status) {
    case "completed":
      return "text-signal";
    case "waiting_for_user":
    case "waiting_for_agent":
      return "text-amber-300";
    case "running":
      return "text-signal-warm";
    default:
      return "text-mist-400";
  }
}

function nodeHint(n: { error_message?: string | null; output_excerpt?: string | null }): string | null {
  return n.error_message || n.output_excerpt || null;
}

function taskBalanceError(task: TaskDetail | null | undefined): string | null {
  if (!task) return null;
  for (const n of task.nodes || []) {
    const hint = nodeHint(n);
    if (isBalanceShortage(hint)) return "余额不足";
  }
  const summary = task.result_json?.summary;
  if (typeof summary === "string" && isBalanceShortage(summary)) return "余额不足";
  return null;
}

function formatDuration(start?: string, end?: string, live?: boolean) {
  if (!start) return "—";
  const s = Date.parse(start);
  if (!Number.isFinite(s)) return "—";
  const e = end ? Date.parse(end) : live ? Date.now() : s;
  if (!Number.isFinite(e) || e < s) return "—";
  const sec = (e - s) / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const r = Math.round(sec % 60);
  return `${m}m ${r}s`;
}

function formatWhen(ts?: string) {
  if (!ts) return "—";
  const d = Date.parse(ts);
  if (!Number.isFinite(d)) return "—";
  return new Date(d).toLocaleString();
}

function artifactLabel(a: ArtifactItem) {
  const raw = a.name || a.uri || a.url || "artifact";
  const base = raw.replace(/\\/g, "/").split("/").pop() || raw;
  return base;
}

type Tab = "overview" | "nodes" | "execution" | "cost";

export function TaskDetailDrawer({
  taskId,
  task,
  events,
  artifacts,
  evaluation,
  memories,
  busy,
  canCancel,
  onCancel,
  onRetry,
  onEvaluate,
  onApprove,
  onReject,
  onReplayNode,
  onRecovered,
}: {
  taskId: string | null;
  task: TaskDetail | null;
  events: TaskEvent[];
  artifacts: ArtifactItem[];
  evaluation: TaskEvaluation | null;
  memories: MemoryEntry[];
  onRecovered?: () => void;
  busy: boolean;
  canCancel: boolean;
  onCancel: () => void;
  onRetry: () => void;
  onEvaluate: () => void;
  onApprove: (note: string) => void;
  onReject: () => void;
  onReplayNode: (nodeKey: string) => Promise<void>;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const [goalOpen, setGoalOpen] = useState(false);
  const [resultOpen, setResultOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [packBusy, setPackBusy] = useState(false);
  const [hitlInput, setHitlInput] = useState("");
  const [detailNodeId, setDetailNodeId] = useState<string | null>(null);
  const [checkpoints, setCheckpoints] = useState<NodeCheckpoint[]>([]);
  const [ckptIndex, setCkptIndex] = useState(0);
  const [ckptError, setCkptError] = useState<string | null>(null);

  useEffect(() => {
    setTab("overview");
    setGoalOpen(false);
    setResultOpen(false);
    setHitlInput("");
    setDetailNodeId(null);
    setCheckpoints([]);
    setCkptIndex(0);
    setCkptError(null);
  }, [taskId]);

  useEffect(() => {
    if (!taskId || tab !== "nodes") return;
    let alive = true;
    listTaskCheckpoints(taskId, { limit: 40 })
      .then((r) => {
        if (!alive) return;
        setCheckpoints(r.checkpoints || []);
        setCkptIndex(0);
        setCkptError(null);
      })
      .catch((err) => {
        if (!alive) return;
        setCheckpoints([]);
        setCkptError(err instanceof Error ? err.message : "checkpoints unavailable");
      });
    return () => {
      alive = false;
    };
  }, [taskId, tab, task?.status]);

  const goal = task?.input_json?.content || task?.plan_json?.goal || task?.title || "";
  const terminal = ["completed", "failed", "cancelled"].includes(task?.status || "");
  const waiting =
    task?.status === "waiting_for_user" || task?.status === "waiting_for_agent";
  const live = !!task && !terminal && !waiting;
  const balanceErr = taskBalanceError(task);
  const duration = formatDuration(
    task?.created_at,
    task?.finished_at || (terminal ? task?.updated_at : undefined),
    live,
  );

  const detailNode = useMemo(
    () => (task?.nodes || []).find((n) => n.id === detailNodeId) || null,
    [task, detailNodeId],
  );
  const selectedCkpt = checkpoints.length
    ? checkpoints[Math.min(ckptIndex, checkpoints.length - 1)]
    : null;

  const reportPreviewUrl = useMemo(() => {
    if (!detailNode || !isReportSkill(hopAgentKey(detailNode))) return null;
    const owned = artifacts.filter((item) => item.node_id === detailNode.id);
    const html = owned.find((item) =>
      (item.name || "").replace(/\\/g, "/").endsWith("report.html"),
    );
    const text =
      owned.find((item) => (item.name || "").endsWith("output.md")) ||
      owned.find((item) => (item.name || "").endsWith("output.txt"));
    return html?.url || text?.url || null;
  }, [artifacts, detailNode]);

  const markdownPreviewUrl = useMemo(() => {
    if (!detailNode) return null;
    if (isReportSkill(hopAgentKey(detailNode)) || isPptSkill(hopAgentKey(detailNode))) return null;
    const owned = artifacts.filter((item) => item.node_id === detailNode.id);
    const md =
      owned.find((item) => (item.name || "").endsWith("output.md")) ||
      owned.find((item) => (item.name || "").endsWith("output.txt"));
    return md?.url || null;
  }, [artifacts, detailNode]);

  const pptArtifact = useMemo(() => {
    if (!detailNode || !isPptSkill(hopAgentKey(detailNode))) return null;
    const owned = artifacts.filter((item) => item.node_id === detailNode.id);
    const deck =
      owned.find((item) => (item.name || "").replace(/\\/g, "/").endsWith("deck.pptx")) ||
      owned.find((item) => isPptxName(item.name));
    return deck?.url ? deck : null;
  }, [artifacts, detailNode]);

  const copyId = useCallback(async () => {
    if (!taskId) return;
    try {
      await navigator.clipboard.writeText(taskId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* ignore */
    }
  }, [taskId]);

  function copyArtifactsToClipboard() {
    if (artifacts.length === 0) return;
    const grouped = artifacts.reduce(
      (acc, a) => {
        const node = a.node_id || "unknown";
        if (!acc[node]) acc[node] = [];
        acc[node].push(a);
        return acc;
      },
      {} as Record<string, typeof artifacts>,
    );
    let text = `Task: ${task?.task_id || "unknown"}\nGoal: ${goal}\n\n`;
    Object.entries(grouped).forEach(([nodeId, nodeArtifacts]) => {
      text += `Node: ${nodeId}\n`;
      nodeArtifacts.forEach((a) => {
        text += `  - ${a.name || a.uri}\n    URL: ${a.url || a.uri}\n`;
      });
      text += "\n";
    });
    navigator.clipboard.writeText(text).catch(() => undefined);
  }

  async function downloadPackage() {
    if (!taskId) return;
    setPackBusy(true);
    try {
      await downloadTaskPackage(taskId);
    } catch {
      /* ignore — operator can retry */
    } finally {
      setPackBusy(false);
    }
  }

  const wide = !!(detailNode && isWideDetail(hopAgentKey(detailNode)) && tab === "nodes");

  return (
    <aside
      className={`relative flex w-full shrink-0 flex-col border-t border-white/10 lg:border-l lg:border-t-0 ${
        wide ? "lg:w-[40rem]" : "lg:w-80"
      }`}
    >
      {taskId && task && task.task_id !== taskId ? (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-start justify-center bg-ink-950/50 pt-16 font-mono text-[11px] text-mist-300">
          更新详情…
        </div>
      ) : null}
      <div className="border-b border-white/10 px-4 py-3">
        <div className="flex items-start justify-between gap-2">
          <div className="font-display text-lg text-mist-100">任务详情</div>
          {task ? (
            <span
              className={`mt-1 font-mono text-[10px] uppercase tracking-[0.14em] ${statusTone(task.status, balanceErr)}`}
            >
              {flowStatusLabel(task.status, balanceErr)}
              {task.os_state ? ` · os ${task.os_state}` : ""}
              {typeof task.progress === "number" && !terminal ? ` · ${task.progress}%` : ""}
            </span>
          ) : null}
        </div>

        {task && taskId ? (
          <>
            <button
              type="button"
              onClick={copyId}
              title={taskId}
              className="mt-1 font-mono text-[10px] text-mist-400 hover:text-mist-200"
            >
              {copied ? "已复制" : taskId.slice(0, 8)}
            </button>
            <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 font-mono text-[11px] text-mist-400">
              <div>
                耗时 <span className="text-mist-100">{duration}</span>
              </div>
              <div>
                节点 <span className="text-mist-100">{task.nodes?.length ?? 0}</span>
              </div>
              <div>
                产物 <span className="text-mist-100">{artifacts.length}</span>
              </div>
              <div>
                事件 <span className="text-mist-100">{events.length}</span>
              </div>
              <div className="col-span-2">
                创建 <span className="text-mist-200">{formatWhen(task.created_at)}</span>
              </div>
            </div>
            {goal ? (
              <button
                type="button"
                onClick={() => setGoalOpen((v) => !v)}
                className={`mt-3 w-full text-left text-sm text-mist-200 ${goalOpen ? "" : "line-clamp-3"}`}
              >
                {goal}
              </button>
            ) : null}
          </>
        ) : taskId ? (
          <p className="mt-2 font-mono text-xs text-mist-400">加载任务…</p>
        ) : (
          <p className="mt-2 font-mono text-xs text-mist-400">未选择任务</p>
        )}

        {task ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {canCancel ? (
              <button
                type="button"
                onClick={onCancel}
                className="rounded-lg border border-white/15 px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-mist-200 hover:border-signal-warm/40"
              >
                取消
              </button>
            ) : null}
            {terminal ? (
              <button
                type="button"
                disabled={busy}
                onClick={onRetry}
                className="rounded-lg bg-signal/90 px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-ink-950 disabled:opacity-40"
              >
                重试
              </button>
            ) : null}
            {terminal ? (
              <button
                type="button"
                disabled={busy}
                onClick={onEvaluate}
                className="rounded-lg border border-signal/30 px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-signal hover:bg-signal/10 disabled:opacity-40"
              >
                评分
              </button>
            ) : null}
            {waiting ? (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onApprove(hitlInput)}
                  className="rounded-lg bg-amber-300/90 px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-ink-950 disabled:opacity-40"
                >
                  批准
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={onReject}
                  className="rounded-lg border border-red-400/40 px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-red-300 hover:bg-red-400/10 disabled:opacity-40"
                >
                  驳回
                </button>
              </>
            ) : null}
          </div>
        ) : null}

        {waiting ? (
          <textarea
            id="tasks-hitl-input"
            name="tasks-hitl-input"
            value={hitlInput}
            onChange={(e) => setHitlInput(e.target.value)}
            rows={3}
            placeholder="可选：补充需求 / 修改意见…"
            className="mt-2 w-full resize-y rounded-xl border border-amber-300/30 bg-ink-950/60 px-3 py-2 font-mono text-[11px] text-mist-100 placeholder:text-mist-400/60 focus:border-amber-300/60 focus:outline-none"
          />
        ) : null}
      </div>

      {taskId ? (
        <div className="flex gap-1 border-b border-white/10 px-3 pt-2">
          {(
            [
              ["overview", "概览", null],
              ["nodes", "节点", task?.nodes?.length ?? null],
              ["execution", "执行", null],
              ["cost", "成本", null],
            ] as const
          ).map(([id, label, count]) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={`rounded-t-lg px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.14em] ${
                tab === id
                  ? "bg-white/5 text-signal"
                  : "text-mist-400 hover:text-mist-200"
              }`}
            >
              {label}
              {typeof count === "number" && count > 0 ? ` ${count}` : ""}
            </button>
          ))}
        </div>
      ) : null}

      <div className="flex-1 space-y-4 overflow-auto p-4">
        {taskId && !task ? (
          <p className="font-mono text-xs text-mist-500">加载中…</p>
        ) : null}

        {tab === "overview" && task ? (
          <>
            {evaluation ? (
              <Section title="评分">
                <div className="flex items-baseline gap-3">
                  <span className="font-display text-3xl text-signal">{evaluation.grade}</span>
                  <span className="font-mono text-lg text-mist-100">{evaluation.score}</span>
                  <span className="font-mono text-[10px] text-mist-400">{evaluation.method}</span>
                </div>
                {evaluation.summary ? (
                  <p className="mt-2 text-xs text-mist-300">{evaluation.summary}</p>
                ) : null}
                {evaluation.dimensions ? (
                  <div className="mt-3 grid grid-cols-2 gap-2">
                    {Object.entries(evaluation.dimensions).map(([k, v]) => (
                      <div
                        key={k}
                        className="rounded-lg border border-white/10 bg-ink-900/40 px-2 py-1.5"
                      >
                        <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-mist-400">
                          {DIMENSION_LABELS[k] ?? k}
                        </div>
                        <div className="font-mono text-sm text-mist-100">{v}</div>
                      </div>
                    ))}
                  </div>
                ) : null}
              </Section>
            ) : null}

            {task?.result_json?.summary ? (
              <Section
                title="结果"
                action={
                  String(task.result_json.summary).length > 420 ? (
                    <button
                      type="button"
                      onClick={() => setResultOpen((v) => !v)}
                      className="font-mono text-[9px] uppercase tracking-[0.12em] text-mist-400 hover:text-mist-100"
                    >
                      {resultOpen ? "收起" : "展开"}
                    </button>
                  ) : null
                }
              >
                <pre
                  className={`whitespace-pre-wrap text-xs text-mist-200 ${
                    resultOpen ? "" : "line-clamp-8"
                  }`}
                >
                  {String(task.result_json.summary).slice(0, 2400)}
                </pre>
              </Section>
            ) : terminal ? (
              <p className="font-mono text-xs text-mist-500">无结果摘要</p>
            ) : (
              <p className="font-mono text-xs text-mist-500">运行中，结果将在完成后显示</p>
            )}

            <Section
              title={`产物${artifacts.length ? ` · ${artifacts.length}` : ""}`}
              action={
                <span className="inline-flex gap-3">
                  {taskId ? (
                    <button
                      type="button"
                      disabled={packBusy}
                      onClick={() => void downloadPackage()}
                      className="font-mono text-[9px] uppercase tracking-[0.12em] text-signal hover:text-mist-100 disabled:opacity-50"
                    >
                      {packBusy ? "打包中" : "下载产物包"}
                    </button>
                  ) : null}
                  {artifacts.length > 0 ? (
                    <button
                      type="button"
                      onClick={copyArtifactsToClipboard}
                      className="font-mono text-[9px] uppercase tracking-[0.12em] text-mist-400 hover:text-mist-100"
                    >
                      复制全部
                    </button>
                  ) : null}
                </span>
              }
            >
              {artifacts.length === 0 ? (
                <p className="font-mono text-[11px] text-mist-500">暂无产物</p>
              ) : (
                <ul className="space-y-2">
                  {artifacts.slice(0, 16).map((a) => (
                    <li
                      key={`${a.uri || a.url || a.name}-${a.node_id || ""}`}
                      className="min-w-0"
                    >
                      {a.url ? (
                        <a
                          href={a.url}
                          target="_blank"
                          rel="noreferrer"
                          className="block truncate font-mono text-[11px] text-signal hover:underline"
                          title={a.name || a.uri || a.url}
                        >
                          {artifactLabel(a)}
                        </a>
                      ) : (
                        <span
                          className="block truncate font-mono text-[11px] text-mist-300"
                          title={a.name || a.uri}
                        >
                          {artifactLabel(a)}
                        </span>
                      )}
                      {a.node_id ? (
                        <div className="mt-0.5 font-mono text-[10px] text-mist-500">
                          node · {a.node_id}
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            {memories.length > 0 ? (
              <Section title="Working Memory">
                <ul className="max-h-40 space-y-2 overflow-auto">
                  {memories.slice(0, 8).map((m) => (
                    <li key={m.memory_key} className="rounded-lg border border-white/5 px-2 py-1.5">
                      <div className="font-mono text-[10px] text-signal-dim">{m.memory_key}</div>
                      <p className="mt-0.5 line-clamp-3 text-[11px] text-mist-300">{m.content}</p>
                    </li>
                  ))}
                </ul>
              </Section>
            ) : null}
          </>
        ) : null}

        {tab === "nodes" && taskId && task ? (
          detailNode ? (
            <div className="rounded-xl border border-white/10 bg-ink-900/50 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <button
                  type="button"
                  onClick={() => setDetailNodeId(null)}
                  className="font-mono text-[10px] text-mist-400 hover:text-mist-100"
                >
                  ← 返回
                </button>
                <span className="truncate font-mono text-[10px] text-mist-200">
                  {detailNode.id} ·{" "}
                  {agentDisplayName(hopAgentKey(detailNode), detailNode.agent_name)}
                </span>
              </div>
              {isPptSkill(hopAgentKey(detailNode)) ? (
                pptArtifact?.url ? (
                  <PptPreview url={pptArtifact.url} name={pptArtifact.name} />
                ) : (
                  <p className="text-xs text-mist-400">PPT 还在生成，预览会在 deck.pptx 产出后出现。</p>
                )
              ) : isReportSkill(hopAgentKey(detailNode)) ? (
                reportPreviewUrl ? (
                  <iframe
                    title="报告预览"
                    src={reportPreviewUrl}
                    sandbox="allow-same-origin"
                    className="h-[520px] w-full rounded-lg border border-white/10 bg-white"
                  />
                ) : (
                  <p className="text-xs text-mist-400">报告还在生成，预览会在 report.html 产出后出现。</p>
                )
              ) : (
                <div>
                  <div className="text-sm text-mist-100">
                    {agentDisplayName(hopAgentKey(detailNode), detailNode.agent_name)}
                  </div>
                  <div className="mt-1 font-mono text-[11px] text-mist-400">
                    {hopAgentKey(detailNode)}
                    {" · "}
                    {flowStatusLabel(detailNode.status, nodeHint(detailNode))}
                    {" · attempt="}
                    {detailNode.attempt ?? 0}
                  </div>
                  {detailNode.a2a_task_id ? (
                    <div
                      className="mt-1 truncate font-mono text-[10px] text-mist-400"
                      title={detailNode.a2a_task_id}
                    >
                      a2a={detailNode.a2a_task_id}
                    </div>
                  ) : null}
                  {detailNode.agent_endpoint ? (
                    <div className="mt-1 truncate font-mono text-[10px] text-mist-400">
                      {detailNode.agent_endpoint}
                    </div>
                  ) : null}
                  {isBalanceShortage(nodeHint(detailNode)) ? (
                    <p className="mt-2 text-xs text-red-300">余额不足</p>
                  ) : detailNode.error_message ? (
                    <p className="mt-2 text-xs text-red-300">{detailNode.error_message}</p>
                  ) : null}
                  {detailNode.handoff && typeof detailNode.handoff === "object" ? (
                    <div className="mt-2 rounded-lg border border-white/5 bg-ink-950/40 p-2">
                      <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-mist-400">
                        Handoff
                      </div>
                      <pre className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-mist-300">
                        {JSON.stringify(detailNode.handoff, null, 2).slice(0, 800)}
                      </pre>
                    </div>
                  ) : null}
                  {markdownPreviewUrl ? (
                    <a
                      href={markdownPreviewUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-3 inline-block font-mono text-[10px] text-signal underline underline-offset-2"
                    >
                      打开 output.md
                    </a>
                  ) : null}
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              {checkpoints.length > 0 ? (
                <Section
                  title="Checkpoint"
                  action={
                    <span className="font-mono text-[10px] text-mist-400">
                      {ckptIndex + 1}/{checkpoints.length}
                    </span>
                  }
                >
                  <input
                    id="tasks-checkpoint-slider"
                    name="tasks-checkpoint-slider"
                    type="range"
                    min={0}
                    max={Math.max(0, checkpoints.length - 1)}
                    value={Math.min(ckptIndex, Math.max(0, checkpoints.length - 1))}
                    onChange={(e) => {
                      const i = Number(e.target.value);
                      setCkptIndex(i);
                      const ck = checkpoints[i];
                      if (ck?.node_key) setDetailNodeId(ck.node_key);
                    }}
                    className="w-full accent-signal"
                  />
                  {selectedCkpt ? (
                    <div className="mt-2 space-y-1 font-mono text-[11px] text-mist-300">
                      <div>
                        <span className="text-signal">{selectedCkpt.node_key}</span>
                        {" · "}
                        {selectedCkpt.status}
                        {" · attempt="}
                        {selectedCkpt.attempt}
                      </div>
                      {selectedCkpt.created_at ? (
                        <div className="text-mist-400">
                          {new Date(selectedCkpt.created_at).toLocaleString()}
                        </div>
                      ) : null}
                      {typeof selectedCkpt.snapshot_json?.error === "string" &&
                      selectedCkpt.snapshot_json.error ? (
                        <div className="text-red-300/90">
                          {String(selectedCkpt.snapshot_json.error).slice(0, 160)}
                        </div>
                      ) : null}
                      <div className="flex flex-wrap gap-2 pt-1">
                        <button
                          type="button"
                          className="font-mono text-[10px] text-signal-dim underline underline-offset-2 hover:text-signal"
                          onClick={() => setDetailNodeId(selectedCkpt.node_key)}
                        >
                          查看节点
                        </button>
                        {["failed", "retrying", "success", "cancelled"].includes(
                          selectedCkpt.status,
                        ) ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => onReplayNode(selectedCkpt.node_key)}
                            className="font-mono text-[10px] text-amber-200/90 underline underline-offset-2 hover:text-amber-100 disabled:opacity-40"
                          >
                            从此外重放
                          </button>
                        ) : null}
                      </div>
                    </div>
                  ) : null}
                </Section>
              ) : ckptError ? (
                <div className="font-mono text-[10px] text-mist-500">{ckptError}</div>
              ) : null}

              {(task?.nodes || []).map((node) => (
                <div key={node.id} className="rounded-xl border border-white/10 bg-ink-900/50 p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate text-sm text-mist-100">
                        {agentDisplayName(hopAgentKey(node), node.agent_name)}
                      </div>
                      <div className={`mt-1 font-mono text-[11px] ${statusTone(node.status, nodeHint(node))}`}>
                        {flowStatusLabel(node.status, nodeHint(node))}
                        {" · "}
                        {hopAgentKey(node)}
                        {node.attempt ? ` · #${node.attempt}` : ""}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {["failed", "retrying", "success", "cancelled"].includes(node.status) ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => onReplayNode(node.id)}
                          className="font-mono text-[10px] text-amber-200/90 underline underline-offset-2 hover:text-amber-100 disabled:opacity-40"
                        >
                          重放
                        </button>
                      ) : null}
                      <button
                        type="button"
                        onClick={() => setDetailNodeId(node.id)}
                        className="font-mono text-[10px] text-signal-dim underline underline-offset-2 hover:text-signal"
                      >
                        详情
                      </button>
                    </div>
                  </div>
                  {isBalanceShortage(nodeHint(node)) ? (
                    <p className="mt-2 text-xs text-red-300">余额不足</p>
                  ) : node.error_message ? (
                    <p className="mt-2 text-xs text-red-300">{node.error_message}</p>
                  ) : null}
                </div>
              ))}
              {(task?.nodes || []).length === 0 ? (
                <p className="font-mono text-xs text-mist-500">暂无节点</p>
              ) : null}
            </div>
          )
        ) : null}

        {tab === "execution" && taskId ? (
          <div className="space-y-4">
            <ExecutionPanel taskId={taskId} busy={busy} onRecovered={onRecovered} />
            <div>
              <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
                Scheduler Trace
              </div>
              <TaskTrace events={events} />
            </div>
          </div>
        ) : null}

        {tab === "cost" && taskId ? <CostPanel taskId={taskId} /> : null}
      </div>
    </aside>
  );
}

function Section({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-white/10 bg-ink-900/50 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
          {title}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}
