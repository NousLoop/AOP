/**
 * Finish interrupted api split: remaining domains + barrel.
 * Does NOT wipe existing lib/api/* modules.
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const srcPath = path.join(root, "lib", "api.ts");
const outDir = path.join(root, "lib", "api");
const lines = fs.readFileSync(srcPath, "utf8").split(/\r?\n/);

function idx(pred, from = 0) {
  for (let i = from; i < lines.length; i++) if (pred(lines[i])) return i;
  return -1;
}

function block(startPred, endPred) {
  const s = idx(startPred);
  const e = endPred ? idx(endPred, s + 1) : lines.length;
  if (s < 0) throw new Error(`start not found: ${startPred}`);
  if (e < 0) throw new Error(`end not found after ${s}: ${endPred}`);
  return lines.slice(s, e).join("\n").trimEnd() + "\n";
}

function write(name, content) {
  fs.writeFileSync(path.join(outDir, name), content.endsWith("\n") ? content : content + "\n");
  console.log("wrote", name);
}

if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

write(
  "discovery.ts",
  `import { request } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type DiscoverCandidate"),
      (l) => l.startsWith("// Runtime"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export type SkillManifest"),
      (l) => l.startsWith("export { API_BASE"),
    ),
);

write(
  "runtime.ts",
  `import { request } from "./client";
import type { ArtifactItem } from "./artifacts";

` +
    block(
      (l) => l.startsWith("export type RuntimeEdgeInput"),
      (l) => l.startsWith("// Execution"),
    ),
);

write(
  "visual.ts",
  `import { request, cachedGet } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type VisualGraphNode"),
      (l) => l.startsWith("export function pauseTask"),
    ),
);

write(
  "scheduling.ts",
  `import { request } from "./client";
import type { DiscoverCandidate } from "./discovery";

` +
    block(
      (l) => l.startsWith("export type ScheduleRequirement"),
      (l) => l.startsWith("// Cost"),
    ),
);

write(
  "tenant.ts",
  `import { request } from "./client";
import type { QuotaStatus } from "./billing";
import type { CostAggregate } from "./tasks";

` +
    block(
      (l) => l.startsWith("export type TenantBudgetScope"),
      (l) => l.startsWith("// Skills"),
    ),
);

write(
  "index.ts",
  `/** Domain-split API surface. Prefer \`@/lib/api\` or \`@/lib/api/<domain>\`. */
export * from "./client";
export * from "./preflight";
export * from "./tasks";
export * from "./artifacts";
export * from "./agents";
export * from "./marketplace";
export * from "./auth";
export * from "./evaluation";
export * from "./billing";
export * from "./discovery";
export * from "./runtime";
export * from "./visual";
export * from "./scheduling";
export * from "./tenant";
`,
);

fs.writeFileSync(
  srcPath,
  `/** Compatibility barrel — implementation lives in \`lib/api/\`. */\nexport * from "./api/index";\n`,
);
console.log("replaced lib/api.ts with barrel");
console.log("done");
