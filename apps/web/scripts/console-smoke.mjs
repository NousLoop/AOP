#!/usr/bin/env node
/**
 * Hermetic Console smoke: golden-path files exist, nav stays frozen.
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const required = [
  "app/page.tsx",
  "app/inbox/page.tsx",
  "app/tasks/page.tsx",
  "app/agents/page.tsx",
  "app/settings/page.tsx",
  "app/login/page.tsx",
  "components/ops/ChaosPlaybook.tsx",
  "components/shell/PreflightBanner.tsx",
  "components/tasks/agentLabels.ts",
  "hooks/useVisualRuntime.ts",
  "hooks/useTaskLive.ts",
  "hooks/usePreflight.tsx",
  "lib/api.ts",
  "lib/api/index.ts",
  "lib/api/client.ts",
  "lib/api/auth.ts",
  "lib/api/tasks.ts",
  "lib/livePoll.ts",
  "lib/serverGateway.ts",
  "middleware.ts",
  "app/api/auth/login/route.ts",
  "app/api/backend/[...path]/route.ts",
];

const missing = required.filter((rel) => !fs.existsSync(path.join(root, rel)));
if (missing.length) {
  console.error("missing:", missing.join(", "));
  process.exit(1);
}

const shell = fs.readFileSync(path.join(root, "components/shell/AppShell.tsx"), "utf8");
const forbiddenNav = ["/marketplace", "/scheduling", "/inbox", "/workflows"];
const leaked = forbiddenNav.filter((href) => shell.includes(`"${href}"`) || shell.includes(`'${href}'`));
if (leaked.length) {
  console.error("nav freeze broken; AppShell still links", leaked.join(", "));
  process.exit(1);
}
for (const href of ["/", "/tasks", "/agents", "/settings"]) {
  if (!shell.includes(`href: "${href}"`)) {
    console.error("nav missing", href);
    process.exit(1);
  }
}

const apiBarrel = fs.readFileSync(path.join(root, "lib/api.ts"), "utf8");
if (!apiBarrel.includes('from "./api/index"')) {
  console.error("lib/api.ts must re-export ./api/index");
  process.exit(1);
}
const apiSurface = [
  "lib/api/preflight.ts",
  "lib/api/tasks.ts",
  "lib/api/auth.ts",
  "lib/api/client.ts",
]
  .map((rel) => fs.readFileSync(path.join(root, rel), "utf8"))
  .join("\n");
for (const needle of ["recover_steps", "downloadTaskPackage", "listTeammates", "clearLegacySessionToken"]) {
  if (!apiSurface.includes(needle)) {
    console.error("lib/api/* missing", needle);
    process.exit(1);
  }
}

const commandCenter = fs.readFileSync(
  path.join(root, "components/visual-runtime/CommandCenter.tsx"),
  "utf8",
);
if (!commandCenter.includes('dynamic(') || !commandCenter.includes("LiveAgentNetwork")) {
  console.error("CommandCenter must dynamic-import LiveAgentNetwork");
  process.exit(1);
}

const drawer = fs.readFileSync(path.join(root, "components/tasks/TaskDetailDrawer.tsx"), "utf8");
if (drawer.includes('from "@/components/tasks/TaskFlowDag"')) {
  console.error("TaskDetailDrawer must not import TaskFlowDag (pulls React Flow)");
  process.exit(1);
}
if (!drawer.includes('from "@/components/tasks/agentLabels"')) {
  console.error("TaskDetailDrawer must use agentLabels helpers");
  process.exit(1);
}

console.log("console smoke ok", required.length, "files");
