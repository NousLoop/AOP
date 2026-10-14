"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  apiErrorMessage,
  approveTask,
  cancelTask,
  createTask,
  evaluateTask,
  listTasks,
  rejectTask,
  replayTaskNode,
  type TaskSummary,
} from "@/lib/api";
import { useTaskLive } from "@/hooks/useTaskLive";
import { TaskDetailDrawer } from "@/components/tasks/TaskDetailDrawer";

const TaskNetworkPanel = dynamic(
  () =>
    import("@/components/visual-runtime/TaskNetworkPanel").then((m) => ({
      default: m.TaskNetworkPanel,
    })),
  {
    ssr: false,
    loading: () => (
      <div className="flex min-h-[320px] flex-1 items-center justify-center font-mono text-xs text-mist-400">
        加载协作图…
      </div>
    ),
  },
);

function tasksHref(status: string, taskId?: string | null) {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (taskId) q.set("task", taskId);
  const qs = q.toString();
  return qs ? `/tasks?${qs}` : "/tasks";
}

const FILTERS = [
  { key: "", label: "全部" },
  { key: "running", label: "运行中" },
  { key: "waiting_for_user,waiting_for_agent", label: "待审批" },
  { key: "completed", label: "成功" },
  { key: "failed", label: "失败" },
  { key: "cancelled", label: "已取消" },
];

export function TasksWorkspace({ initialTaskId }: { initialTaskId?: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const statusFilter = searchParams.get("status") || "";
  const urlTask = searchParams.get("task") || initialTaskId || null;

  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [listQuery, setListQuery] = useState("");
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(urlTask);
  const [busy, setBusy] = useState(false);

  const { task, events, artifacts, evaluation, memories, error: liveError, reload, liveMode, wsConnected } =
    useTaskLive(selectedId);

  const loadList = useCallback(async () => {
    if (typeof document !== "undefined" && document.hidden) return;
    try {
      const data = await listTasks({
        status: statusFilter || undefined,
        limit: 40,
      });
      setTasks(data.tasks || []);
      setListError(null);
    } catch (err) {
      setListError(apiErrorMessage(err, "list failed"));
    } finally {
      setListLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void loadList();
    const t = setInterval(loadList, 12000);
    const onVis = () => {
      if (!document.hidden) void loadList();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [loadList]);

  useEffect(() => {
    setSelectedId(urlTask);
  }, [urlTask]);

  const filteredTasks = useMemo(() => {
    const q = listQuery.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter(
      (t) =>
        t.task_id.toLowerCase().includes(q) ||
        (t.title || "").toLowerCase().includes(q) ||
        (t.status || "").toLowerCase().includes(q),
    );
  }, [tasks, listQuery]);

  function selectTask(id: string) {
    setSelectedId(id);
    router.replace(tasksHref(statusFilter, id), { scroll: false });
  }

  async function onCancel() {
    if (!selectedId || busy || !canCancel) return;
    setBusy(true);
    try {
      await cancelTask(selectedId);
      await loadList();
    } catch (err) {
      setListError(apiErrorMessage(err, "cancel failed"));
    } finally {
      setBusy(false);
    }
  }

  async function onRetry() {
    if (!task || busy) return;
    const content = task.input_json?.content || task.plan_json?.goal || "";
    if (!content.trim()) return;
    setBusy(true);
    try {
      const created = await createTask(content.trim());
      selectTask(created.task_id);
      await loadList();
    } catch (err) {
      setListError(apiErrorMessage(err, "retry failed"));
    } finally {
      setBusy(false);
    }
  }

  async function onEvaluate() {
    if (!selectedId || busy) return;
    setBusy(true);
    try {
      await evaluateTask(selectedId);
      reload();
      await loadList();
    } catch (err) {
      setListError(apiErrorMessage(err, "evaluate failed"));
    } finally {
      setBusy(false);
    }
  }

  async function onApprove(note?: string) {
    if (!selectedId || busy) return;
    setBusy(true);
    try {
      await approveTask(selectedId, undefined, note);
      reload();
      await loadList();
    } catch (err) {
      setListError(apiErrorMessage(err, "approve failed"));
    } finally {
      setBusy(false);
    }
  }

  async function onReject() {
    if (!selectedId || busy) return;
    setBusy(true);
    try {
      await rejectTask(selectedId);
      reload();
      await loadList();
    } catch (err) {
      setListError(apiErrorMessage(err, "reject failed"));
    } finally {
      setBusy(false);
    }
  }

  async function onReplayNode(nodeKey: string) {
    if (!selectedId || busy) return;
    setBusy(true);
    try {
      await replayTaskNode(selectedId, nodeKey, { clear_downstream: true });
      reload();
      await loadList();
    } catch (err) {
      setListError(apiErrorMessage(err, "replay failed"));
    } finally {
      setBusy(false);
    }
  }

  const nodeInflight = (task?.nodes || []).some((n) =>
    ["pending", "ready", "running", "retrying", "waiting_for_user", "waiting_for_agent"].includes(n.status),
  );
  const canCancel =
    !!task &&
    !busy &&
    (nodeInflight || !["completed", "failed", "cancelled"].includes(task.status || ""));

  return (
    <div className="flex h-[calc(100vh-3.5rem)] min-h-[560px] flex-col lg:flex-row">
      <aside className="flex w-full shrink-0 flex-col border-b border-white/10 lg:w-64 lg:border-b-0 lg:border-r">
        <div className="border-b border-white/10 px-3 py-3">
          <div className="font-display text-lg text-mist-100">任务</div>
          <input
            id="tasks-list-search"
            name="tasks-list-search"
            value={listQuery}
            onChange={(e) => setListQuery(e.target.value)}
            placeholder="搜索标题 / Task ID / 状态"
            autoComplete="off"
            className="mt-2 w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 font-mono text-xs text-mist-100 outline-none placeholder:text-mist-500 focus:border-signal/40"
          />
          <div className="mt-2 flex flex-wrap gap-1">
            {FILTERS.map((f) => (
              <Link
                key={f.key || "all"}
                href={tasksHref(f.key, selectedId)}
                className={`rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.12em] ${
                  statusFilter === f.key
                    ? "bg-signal/15 text-signal"
                    : "text-mist-400 hover:text-mist-100"
                }`}
              >
                {f.label}
              </Link>
            ))}
          </div>
        </div>
        <div className="flex-1 overflow-auto">
          {listError ? (
            <div className="p-3 font-mono text-[11px] text-signal-warm">{listError}</div>
          ) : null}
          {filteredTasks.length === 0 ? (
            <div className="p-4 font-mono text-xs text-mist-400">
              {listLoading
                ? "加载任务…"
                : tasks.length === 0
                  ? "暂无任务"
                  : "无匹配任务"}
            </div>
          ) : null}
          {filteredTasks.map((t) => (
            <button
              key={t.task_id}
              type="button"
              onClick={() => selectTask(t.task_id)}
              className={`block w-full border-b border-white/5 px-3 py-3 text-left transition hover:bg-white/5 ${
                selectedId === t.task_id ? "bg-signal/10" : ""
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-[11px] text-mist-400">
                  {t.task_id.slice(0, 8)}
                </span>
                <span
                  className={`font-mono text-[10px] uppercase ${
                    t.status === "completed"
                      ? "text-signal"
                      : t.status === "failed"
                        ? "text-red-400"
                        : t.status === "waiting_for_user" || t.status === "waiting_for_agent"
                          ? "text-amber-300"
                          : t.status === "running"
                            ? "text-signal-warm"
                            : "text-mist-400"
                  }`}
                >
                  {t.status === "waiting_for_user"
                    ? "待系统审"
                    : t.status === "waiting_for_agent"
                      ? "待 Agent 审"
                      : t.status}
                </span>
              </div>
              {t.os_state ? (
                <div className="mt-0.5 font-mono text-[9px] uppercase tracking-[0.12em] text-mist-500">
                  os {t.os_state}
                </div>
              ) : null}
              <div className="mt-1 truncate text-sm text-mist-100">
                {t.title || "Untitled task"}
              </div>
              {typeof t.progress === "number" ? (
                <div className="mt-1 font-mono text-[10px] text-mist-400">{t.progress}%</div>
              ) : null}
            </button>
          ))}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-signal">
            Live Agent Network
          </span>
          {selectedId ? (
            <span className="font-mono text-[10px] text-mist-400">{selectedId}</span>
          ) : null}
          <span
            className={`font-mono text-[10px] uppercase tracking-[0.12em] ${
              wsConnected ? "text-signal" : "text-mist-400"
            }`}
            title={wsConnected ? "WebSocket live" : "HTTP poll fallback"}
          >
            {liveMode === "ws" ? "live·ws" : "live·poll"}
          </span>
        </div>
        {selectedId ? (
          <TaskNetworkPanel
            taskId={selectedId}
            createdAt={
              typeof task?.created_at === "string" ? task.created_at : undefined
            }
            onBusyChange={setBusy}
            embedded
          />
        ) : (
          <div className="flex flex-1 items-center justify-center rounded-xl border border-dashed border-white/10 font-mono text-sm text-mist-400">
            选择左侧任务查看 Live Agent Network
          </div>
        )}
        {liveError ? (
          <div className="mt-2 font-mono text-[11px] text-signal-warm">{liveError}</div>
        ) : null}
      </section>

      <TaskDetailDrawer
        taskId={selectedId}
        task={task}
        events={events}
        artifacts={artifacts}
        evaluation={evaluation}
        memories={memories}
        busy={busy}
        canCancel={canCancel}
        onCancel={onCancel}
        onRetry={onRetry}
        onEvaluate={onEvaluate}
        onApprove={onApprove}
        onReject={onReject}
        onReplayNode={onReplayNode}
        onRecovered={() => reload()}
      />
    </div>
  );
}
