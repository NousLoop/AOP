"use client";

import {
  FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  apiErrorMessage,
  approveTask,
  cancelTask,
  createTask,
  downloadTaskPackage,
  getTask,
  getTaskArtifacts,
  listAgents,
  pauseTask,
  recoverTask,
  rejectTask,
  resumeTask,
  type Agent,
  type ArtifactItem,
  type PreflightAgent,
  type VisualGraphNode,
} from "@/lib/api";
import { useVisualRuntime } from "@/hooks/useVisualRuntime";
import { usePreflight } from "@/hooks/usePreflight";
import { LiveAgentNetwork } from "./LiveAgentNetwork";
import { ChaosPlaybook } from "@/components/ops/ChaosPlaybook";
import {
  AgentInspector,
  ExecutionTimeline,
  GraphReplayBar,
  TaskInspector,
} from "./Inspectors";

const PROMPTS = [
  "Research Vidu S2 and generate a technical report",
  "使用目前所有的 agent，调研 ui2v 这个网页，整理公开资料并输出研究摘要",
  "Discover agents for research and summarize A2A OS architecture",
];

/** Same chrome as idle Network so Suspense fallback hydrates without a flash. */
export function NetworkIdleHero({
  agentChips,
}: {
  agentChips?: ReactNode;
}) {
  return (
    <div className="flex h-full min-h-[52vh] flex-col items-center justify-center px-6 pb-36 pt-16 text-center">
      <div className="font-display text-5xl tracking-tight text-mist-100 md:text-6xl">
        A2A OS
      </div>
      <p className="mt-4 max-w-md font-sans text-sm text-mist-400 md:text-base">
        What do you want to build?
      </p>
      <div className="mt-10 h-40 w-40 rounded-full border border-signal/30 bg-signal/5 shadow-[0_0_80px_rgba(61,255,168,0.15)]" />
      <p className="mt-8 font-mono text-[10px] uppercase tracking-[0.22em] text-mist-400">
        Live Agent Network · idle
      </p>
      {agentChips}
    </div>
  );
}

export function CommandCenter() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlTask = searchParams.get("task");
  const [goal, setGoal] = useState(
    () => searchParams.get("prefill") || PROMPTS[0],
  );
  const [taskId, setTaskId] = useState<string | null>(urlTask);
  const [chipsReady, setChipsReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState<VisualGraphNode | null>(null);
  const [createdAt, setCreatedAt] = useState<string | undefined>();
  const [artifacts, setArtifacts] = useState<ArtifactItem[]>([]);
  const [summary, setSummary] = useState<string | null>(null);
  const [replayCursor, setReplayCursor] = useState<number | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [hitlBusy, setHitlBusy] = useState(false);
  const [packBusy, setPackBusy] = useState(false);
  const replayTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const { graph, events, isConnected, stats, refresh, error: liveError } =
    useVisualRuntime(taskId);
  const { snapshot: preflight } = usePreflight();
  const runBlocked = preflight ? !preflight.can_run : false;
  const waitingAgent =
    (graph?.status || "").toLowerCase() === "waiting_for_agent" ||
    (graph?.task?.status || "").toLowerCase() === "waiting_for_agent";
  const waitingHitl =
    !waitingAgent &&
    ((graph?.status || "").toLowerCase() === "waiting" ||
      (graph?.status || "").toLowerCase() === "waiting_for_user" ||
      (graph?.task?.status || "").toLowerCase() === "waiting_for_user");

  useEffect(() => {
    setTaskId(urlTask);
    const prefill = searchParams.get("prefill");
    if (prefill) setGoal(prefill);
    setChipsReady(true);
  }, [searchParams, urlTask]);

  useEffect(() => {
    listAgents()
      .then((r) => setAgents(r.agents || []))
      .catch(() => setAgents([]));
  }, []);

  useEffect(() => {
    if (!taskId) {
      setArtifacts([]);
      setSummary(null);
      return;
    }
    let alive = true;
    getTask(taskId)
      .then((t) => {
        if (!alive) return;
        setCreatedAt(t.created_at as string | undefined);
        const s = t.result_json?.summary;
        setSummary(typeof s === "string" ? s : null);
      })
      .catch(() => undefined);
    getTaskArtifacts(taskId)
      .then((r) => {
        if (alive) setArtifacts(r.artifacts || []);
      })
      .catch(() => {
        if (alive) setArtifacts([]);
      });
    return () => {
      alive = false;
    };
  }, [taskId, graph?.status]);

  useEffect(() => {
    return () => {
      if (replayTimer.current) clearInterval(replayTimer.current);
    };
  }, []);

  const agentForSelected = useMemo(() => {
    if (!selected?.agent_id) return null;
    return (
      agents.find(
        (a) =>
          a.agent_id === selected.agent_id ||
          a.agent_key === selected.agent_id ||
          a.name === selected.label,
      ) || null
    );
  }, [agents, selected]);

  const readinessForSelected = useMemo((): PreflightAgent | null => {
    const list = preflight?.agents || [];
    if (!list.length || !selected) return null;
    const id = (selected.agent_id || selected.label || "").toLowerCase();
    return (
      list.find((a) => {
        const key = (a.agent_key || "").toLowerCase();
        return key && (id === key || id.includes(key) || key.includes(id));
      }) || null
    );
  }, [preflight, selected]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const content = goal.trim();
    if (!content || loading) return;
    setLoading(true);
    setError(null);
    setSelected(null);
    setReplayCursor(null);
    setReplaying(false);
    try {
      const task = await createTask(content);
      setTaskId(task.task_id);
      setLoading(false);
      router.replace(`/?task=${encodeURIComponent(task.task_id)}`);
    } catch (err) {
      setError(apiErrorMessage(err, "创建任务失败"));
      setLoading(false);
    }
  }

  const onPause = useCallback(async () => {
    if (!taskId) return;
    try {
      await pauseTask(taskId);
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err, "pause failed"));
    }
  }, [taskId, refresh]);

  const onResume = useCallback(async () => {
    if (!taskId) return;
    try {
      await resumeTask(taskId);
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err, "resume failed"));
    }
  }, [taskId, refresh]);

  const onRetry = useCallback(async () => {
    if (!taskId) return;
    try {
      await recoverTask(taskId);
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err, "retry failed"));
    }
  }, [taskId, refresh]);

  const onCancel = useCallback(async () => {
    if (!taskId) return;
    try {
      await cancelTask(taskId);
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err, "cancel failed"));
    }
  }, [taskId, refresh]);

  const onApproveHitl = useCallback(async () => {
    if (!taskId || hitlBusy) return;
    setHitlBusy(true);
    try {
      await approveTask(taskId);
      await refresh();
      const t = await getTask(taskId);
      setSummary(
        typeof t.result_json?.summary === "string" ? t.result_json.summary : null,
      );
    } catch (err) {
      setError(apiErrorMessage(err, "approve failed"));
    } finally {
      setHitlBusy(false);
    }
  }, [taskId, hitlBusy, refresh]);

  const onRejectHitl = useCallback(async () => {
    if (!taskId || hitlBusy) return;
    setHitlBusy(true);
    try {
      await rejectTask(taskId, "rejected from Network");
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err, "reject failed"));
    } finally {
      setHitlBusy(false);
    }
  }, [taskId, hitlBusy, refresh]);

  const startReplay = useCallback(() => {
    if (!events.length) return;
    setReplaying(true);
    setReplayCursor(0);
    if (replayTimer.current) clearInterval(replayTimer.current);
    const max = events.reduce((m, e) => Math.max(m, e.sequence ?? 0), 0);
    let cur = 0;
    replayTimer.current = setInterval(() => {
      cur += 1;
      if (cur > max) {
        if (replayTimer.current) clearInterval(replayTimer.current);
        setReplaying(false);
        setReplayCursor(null);
        return;
      }
      setReplayCursor(cur);
    }, 450);
  }, [events]);

  const stopReplay = useCallback(() => {
    if (replayTimer.current) clearInterval(replayTimer.current);
    setReplaying(false);
    setReplayCursor(null);
  }, []);

  const active = !!taskId;

  return (
    <div className="relative flex min-h-[calc(100vh-4rem)] flex-col">
      {/* Ambient field */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute left-1/2 top-[18%] h-[520px] w-[520px] -translate-x-1/2 rounded-full bg-[radial-gradient(circle,rgba(61,255,168,0.14),transparent_68%)] blur-2xl" />
        <div className="absolute bottom-0 left-0 right-0 h-48 bg-gradient-to-t from-ink-950/90 to-transparent" />
      </div>

      <div className="relative z-10 flex flex-1 flex-col lg:flex-row">
        <section className="relative min-h-[52vh] flex-1 lg:min-h-0">
          {!active ? (
            <NetworkIdleHero
              agentChips={
                chipsReady && preflight?.agents?.length ? (
                  <div className="mt-6 flex max-w-xl flex-wrap justify-center gap-2">
                    {preflight.agents.map((a) => (
                      <span
                        key={a.agent_key}
                        title={a.runner_reason || a.agent_key}
                        className={`rounded-full border px-2.5 py-1 font-mono text-[10px] ${
                          a.runner_ready
                            ? "border-signal/30 text-signal"
                            : a.reachable
                              ? "border-signal-warm/40 text-signal-warm"
                              : "border-white/10 text-mist-400"
                        }`}
                      >
                        {a.agent_key}
                        {a.runner_ready ? "" : a.reachable ? " · stub" : " · off"}
                      </span>
                    ))}
                  </div>
                ) : null
              }
            />
          ) : (
            <>
              <div className="absolute left-4 top-4 z-20 flex items-center gap-3">
                <span
                  className={`h-2 w-2 rounded-full ${isConnected ? "bg-signal" : "bg-signal-warm"}`}
                />
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-mist-400">
                  Live Agent Network · {graph?.status || "running"} · seq{" "}
                  {graph?.sequence ?? 0}
                </span>
              </div>
              <div className="absolute right-4 top-4 z-20 flex gap-2">
                <ControlBtn label="Pause" onClick={onPause} />
                <ControlBtn label="Resume" onClick={onResume} />
                <ControlBtn label="Retry" onClick={onRetry} />
                <ControlBtn label="Cancel" onClick={onCancel} />
                <ControlBtn
                  label="Tasks"
                  onClick={() =>
                    router.push(`/tasks?task=${encodeURIComponent(taskId!)}`)
                  }
                />
              </div>
              {waitingHitl ? (
                <div className="absolute left-4 right-4 top-12 z-20 flex flex-wrap items-center gap-2 rounded-lg border border-signal-warm/40 bg-ink-950/85 px-3 py-2 backdrop-blur md:left-auto md:right-4 md:max-w-md">
                  <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-signal-warm">
                    Waiting for system approval
                  </span>
                  <button
                    type="button"
                    disabled={hitlBusy}
                    onClick={() => void onApproveHitl()}
                    className="rounded-md bg-signal px-2.5 py-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-ink-950 disabled:opacity-40"
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={hitlBusy}
                    onClick={() => void onRejectHitl()}
                    className="rounded-md border border-signal-warm/50 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider text-signal-warm disabled:opacity-40"
                  >
                    Reject
                  </button>
                  <Link
                    href="/tasks?status=waiting_for_user,waiting_for_agent"
                    className="font-mono text-[10px] uppercase tracking-wider text-signal hover:underline"
                  >
                    Open Tasks
                  </Link>
                </div>
              ) : waitingAgent ? (
                <div className="absolute left-4 right-4 top-12 z-20 flex flex-wrap items-center gap-2 rounded-lg border border-signal/30 bg-ink-950/85 px-3 py-2 backdrop-blur md:left-auto md:right-4 md:max-w-md">
                  <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-signal">
                    Waiting for agent approval
                  </span>
                  <Link
                    href="/tasks?status=waiting_for_user,waiting_for_agent"
                    className="font-mono text-[10px] uppercase tracking-wider text-mist-300 hover:text-signal"
                  >
                    Tasks
                  </Link>
                </div>
              ) : null}
              <div className="absolute inset-0 min-h-[52vh]">
                <LiveAgentNetwork
                  snapshot={graph}
                  selectedId={selected?.id}
                  onSelect={setSelected}
                  replayCursor={replayCursor}
                  readiness={preflight?.agents}
                />
              </div>
            </>
          )}
        </section>

        {active ? (
          <aside className="z-20 w-full border-t border-white/10 bg-ink-950/70 px-4 py-4 backdrop-blur-md lg:w-[340px] lg:border-l lg:border-t-0">
            {selected?.type === "agent" ? (
              <AgentInspector
                node={selected}
                agent={agentForSelected}
                readiness={readinessForSelected}
                events={events}
                onPause={onPause}
                onRetry={onRetry}
                onInspect={() =>
                  router.push(`/tasks?task=${encodeURIComponent(taskId!)}`)
                }
              />
            ) : (
              <TaskInspector
                snapshot={graph}
                events={events}
                stats={stats}
                createdAt={createdAt}
                artifacts={artifacts}
                summary={summary}
                onDownloadPackage={
                  taskId
                    ? async () => {
                        setPackBusy(true);
                        setError(null);
                        try {
                          await downloadTaskPackage(taskId);
                        } catch (e) {
                          setError(apiErrorMessage(e, "下载产物包失败"));
                        } finally {
                          setPackBusy(false);
                        }
                      }
                    : undefined
                }
                packageBusy={packBusy}
              />
            )}
            <ExecutionTimeline events={events} />
          </aside>
        ) : null}
      </div>

      {active && events.length > 0 ? (
        <div className="relative z-20 bg-ink-950/80 backdrop-blur-md">
          <GraphReplayBar
            events={events}
            cursor={replayCursor}
            playing={replaying}
            onPlay={startReplay}
            onStop={stopReplay}
            onSeek={(seq) => {
              setReplaying(false);
              if (replayTimer.current) clearInterval(replayTimer.current);
              setReplayCursor(seq);
            }}
          />
        </div>
      ) : null}

      {/* Command bar */}
      <div className="relative z-30 border-t border-white/10 bg-ink-950/85 px-4 py-4 backdrop-blur-md md:px-8">
        {(error || liveError || runBlocked) && (
          <div className="mb-3 space-y-2">
            <div className="font-mono text-xs text-signal-warm">
              {runBlocked
                ? preflight?.hints[0] || "Worker / Outbox 未就绪，任务会停在 ready"
                : error || liveError}
            </div>
            {runBlocked || (preflight?.stuck_running || 0) > 0 ? (
              <ChaosPlaybook steps={preflight?.recover_steps} compact />
            ) : null}
          </div>
        )}
        <form
          onSubmit={onSubmit}
          className="mx-auto flex max-w-4xl items-end gap-3"
        >
          <div className="flex-1">
            <label className="sr-only" htmlFor="a2a-command">
              Ask A2A OS
            </label>
            <textarea
              id="a2a-command"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              rows={2}
              placeholder="Ask A2A OS…"
              className="w-full resize-none rounded-xl border border-white/15 bg-ink-900/60 px-4 py-3 text-sm text-mist-100 outline-none transition focus:border-signal/50"
            />
            <div className="mt-2 flex flex-wrap gap-2">
              {PROMPTS.map((p) => (
                <button
                  key={p.slice(0, 24)}
                  type="button"
                  onClick={() => setGoal(p)}
                  className="truncate rounded-full border border-white/10 px-3 py-1 font-mono text-[10px] text-mist-400 hover:border-signal/30 hover:text-signal"
                >
                  {p.length > 42 ? `${p.slice(0, 42)}…` : p}
                </button>
              ))}
            </div>
          </div>
          <button
            type="submit"
            disabled={loading || !goal.trim() || runBlocked}
            className="shrink-0 rounded-xl bg-signal px-5 py-3 font-mono text-xs font-semibold uppercase tracking-[0.18em] text-ink-950 transition hover:bg-white disabled:opacity-40"
          >
            {loading ? "Planning…" : runBlocked ? "Blocked" : "Run →"}
          </button>
        </form>
      </div>
    </div>
  );
}

function ControlBtn({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border border-white/10 bg-ink-900/70 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-mist-300 backdrop-blur hover:border-signal/40 hover:text-signal"
    >
      {label}
    </button>
  );
}
