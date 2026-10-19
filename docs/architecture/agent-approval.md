# Agent 审批：四种模式与接入清单

新 Agent 接入时按本文改。平台契约在 Orchestrator；Harness 默认已经实现，**新 profile 一般不用写审批代码**。自研 HTTP Agent 必须自己实现「收信号 + 回调」。

相关代码：

| 层 | 路径 |
|----|------|
| 策略 | `apps/orchestrator/approval/__init__.py` |
| 发信号 | `Scheduler._signal_agent_approval` → `POST {endpoint}/v1/approvals` |
| 系统批准 | `POST /v1/tasks/{id}/approve` · Inbox |
| Harness 收件箱 | `packages/agent-runtime/agent_runtime/harness/approvals.py`（`create_harness_app` 自动挂路由） |
| HTTP 对照 | [api.md §2.3](../reference/api.md) |

---

## 1. 四种 `approval_mode`

节点跑完（Agent 返回 `completed`）之后，Scheduler 用计划门 ∪ 运行时 `hitl` 信号决定是否解锁下游。

| 模式 | 谁批 | 节点/任务状态 | 解锁条件 |
|------|------|----------------|----------|
| `none` | 无 | 直接 `success` | 立即解锁下游 |
| `system` | 仅系统（人 / Inbox / Console） | `waiting_for_user` | `approve` 且 `actor=system`（默认） |
| `agent` | 仅对端 Agent | `waiting_for_agent` | 向 `approver_agent` 发信号后，其对 `approve` 且 `actor=agent` |
| `both` | 系统 **和** 对端 Agent | 先 `waiting_for_user`，系统过后再等 Agent | 两边都通过才解锁 |

别名（`normalize_mode`）：`human`/`inbox` → `system`；`peer`/`a2a` → `agent`；`dual` → `both`。

计划里 `requires_approval: true` 且未写 `approval_mode` 时，视为 **`system`**。  
`approval_mode=agent` 但没写 `approver_agent`：无法投递，**回退成 `system`**，避免任务悬空。

`HITL_SKILLS`（默认 `report-generation,ppt-generation`）仍会给对应 skill 打上系统门。

---

## 2. 怎么声明（两种调用方式）

### 2.1 计划 / Workflow 节点（编排侧）

`plan_json.nodes[]` 或 Flows 定义：

```json
{
  "id": "review-hop",
  "skill": "deepseek-harness",
  "approval_mode": "agent",
  "approver_agent": "claude-code"
}
```

| 字段 | 含义 |
|------|------|
| `approval_mode` | `none` \| `system` \| `agent` \| `both` |
| `approver_agent` | Registry 里的 **`agent_key`**（如 `claude-code`），不是 UUID |
| `requires_approval` | 旧字段；`true` ≡ 系统门 |

### 2.2 运行时信号（Agent 互调时由产出声明）

执行 Agent 在 **结果 JSON**（artifact data / `output_json`）里带 `hitl`。Scheduler 与计划门 **取并集**：计划已是 `system`、信号是 `agent` → 变成 **`both`**。

```json
{
  "hitl": {
    "mode": "agent",
    "approver_agent": "claude-code",
    "reason": "peer review before merge"
  }
}
```

| `hitl` 字段 | 说明 |
|-------------|------|
| `mode` / `approval_mode` | 同上四种 |
| `approver_agent` | 必填（agent 门）；对端 `agent_key` |
| `reason` | 展示给审批方 |
| `requires_approval` | 无 mode 时：有 approver → `agent`，否则 `system` |

**仅 Agent 审批的语义：** A 调 B（或同图上某一跳）时，A 或计划规定「这一跳要审批」→ OS 把请求投到 **指定审批 Agent 的 HTTP 入口** → 该 Agent 进入审批阶段 → 回调 OS。

---

## 3. 调用时序（`agent` / `both`）

```
执行 Agent 完成节点
        │
        ▼
Orchestrator resolve_for_node(计划 ∪ hitl)
        │
        ├─ needs_system → 任务 waiting_for_user（Inbox）
        │
        └─ needs_agent  → POST {approver.endpoint}/v1/approvals
                          事件 agent.approval.requested
                          任务 waiting_for_agent（若系统门已过或无需系统）
        │
        ▼
审批 Agent：入队 →（可选）runner 评审 → 回调
        │
        ├─ 通过  POST {A2A_OS_URL}/v1/tasks/{task_id}/approve
        │        { "node_key": "...", "actor": "agent", "input": "理由" }
        │
        └─ 驳回  POST {A2A_OS_URL}/v1/tasks/{task_id}/reject
                 { "node_key": "...", "reason": "..." }
```

`both`：系统先批（Inbox `actor=system`），再等 Agent `actor=agent`。缺任一侧都不会解锁下游。驳回则任务 `failed`。

Orchestrator 投递 `/v1/approvals` 超时 3s、失败只记事件，**节点仍停在等待**；审批 Agent 稍后决定即可。

鉴权：本机 loopback 默认可投；若设了 `AOP_COLLAB_TOKEN`，OS 会带 `X-AOP-Collab-Token`。

---

## 4. 新 Agent 改什么

### 4.1 推荐：新 Harness profile（Claude / Pi / 同类 CLI）

走 [`agents/harness-agent`](../../agents/harness-agent/) + `create_harness_app`，**审批路由已内置**，不要再抄一套。

清单：

1. `packages/agent-runtime/agent_runtime/harness/profiles.py` 加一行（key / 端口 / runner）
2. `agents/harness-agent/profiles/<key>/`：`agent-card.json`、`system.md`
3. 如需本机 Supervisor：`apps/client/aop-node/plugins/<key>/plugin.toml`（`kind = "cli"`，端口与 profiles 表一致；A2A/`/v1/approvals` 由 aopd 托管）
4. 注册到 Gateway 后 `agent_key` = profile key（审批里写这个）
5. 环境变量（见下）指向 Gateway，否则回调失败

健康检查应出现 `"peer_approval": true`。

内置路由：

```
POST {agent}/v1/approvals
GET  {agent}/v1/approvals?status=pending
GET  {agent}/v1/approvals/{id}
POST {agent}/v1/approvals/{id}/decide   {"approved": true|false, "reason": "..."}
```

OS 投递体（示例）：

```json
{
  "type": "approval_request",
  "task_id": "<uuid>",
  "node_key": "review-hop",
  "approval_mode": "agent",
  "approver_agent": "claude-code",
  "reason": "peer review",
  "hitl": { "mode": "agent", "approver_agent": "claude-code" }
}
```

自动评审：Runner 就绪且 `HARNESS_APPROVAL_REVIEW` 未关闭时，会用审批提示跑一轮，解析：

```json
{ "approved": true, "reason": "short justification" }
```

解析失败则留在 `pending`，可用 `/decide` 人工/脚本补批。

### 4.2 自研 Agent（不用 `create_harness_app`）

必须实现与上表相同的 **收件契约**，以及 **回调 OS**：

1. `POST /v1/approvals`：202，持久化 `task_id` / `node_key`
2. 进入本 Agent 的审批阶段（队列、UI、或模型评审均可）
3. 决定后：
   - 通过：`POST {A2A_OS_URL}/v1/tasks/{task_id}/approve`，**必须** `"actor": "agent"`
   - 驳回：`POST .../reject`，带 `reason`
4. 建议 `GET /health` 声明 `peer_approval: true`，方便排障

不要用 `actor=system` 冒充 Inbox，否则 `both` 会把人审和机审搅在一起。

### 4.3 只当「执行方」、不要当审批人

无需实现 `/v1/approvals`。若某跳需要别人批，在 **自己的产出** 里写 `hitl.approver_agent`，或让计划节点写好 `approval_mode`。

---

## 5. 环境变量

| 变量 | 谁用 | 作用 |
|------|------|------|
| `A2A_OS_URL` / `GATEWAY_URL` | 审批 Agent | 回调 approve/reject 的基址（默认 `:8080`） |
| `A2A_OS_API_KEY` / `GATEWAY_API_KEY` | 审批 Agent | 回调鉴权 |
| `AOP_COLLAB_TOKEN` | OS + Agent | 投递 `/v1/approvals` 与 collab HTTP |
| `HARNESS_APPROVAL_REVIEW` | Harness | 默认开；`0` 只入队不跑 runner |
| `HARNESS_APPROVAL_SYNC` | Harness / 测试 | `1` 时在请求内同步评审 |
| `HITL_SKILLS` | Orchestrator | 默认系统门 skill 列表；`off` 关闭 |

---

## 6. 自测

```powershell
# Harness 收件箱
pytest packages/agent-runtime/tests/test_harness_approvals.py -q

# 四种模式合并规则
pytest apps/orchestrator/tests/test_approval.py -q
```

本机：

```powershell
curl http://127.0.0.1:8011/health
# 应含 peer_approval: true

curl -X POST http://127.0.0.1:8011/v1/approvals -H "Content-Type: application/json" `
  -d '{"task_id":"<real-task-uuid>","node_key":"<node>","approval_mode":"agent","approver_agent":"claude-code"}'
```

真实解锁必须用 **正在 `waiting_for_agent` 的 task_id**。假 UUID 也能入队，回调会 404。

---

## 7. Console

| 表面 | 行为 |
|------|------|
| **Tasks 待审批** | 列出 `waiting_for_user` **和** `waiting_for_agent`。抽屉里「系统批准」只对系统门生效；Agent 门等对端 `actor=agent`，人可以 **Reject**。无独立 Inbox 页。 |
| **Network** | 系统等待可 Approve/Reject；Agent 等待只提示，链到 Tasks。 |
| **Flows** | 节点可设 `approval_mode` / `approver_agent`，保存进 DAG，不会在编辑器里丢掉。 |
| 顶栏 | `waiting_hitl` + `waiting_agent` 合计进 Tasks 角标。 |

`GET /v1/preflight` 带 `waiting_hitl` / `waiting_agent`（不算预检失败）。Network 图上 `waiting_for_agent` **不会**再被折成通用 waiting，避免弹出系统 Approve。
