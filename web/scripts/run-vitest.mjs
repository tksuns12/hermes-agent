import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const forwardedArgs = process.argv
  .slice(2)
  .map((arg) => (arg.startsWith("web/") ? arg.slice(4) : arg));

const vitestBinary = resolve(
  process.cwd(),
  "node_modules",
  ".bin",
  process.platform === "win32" ? "vitest.cmd" : "vitest",
);

const result = spawnSync(vitestBinary, forwardedArgs, {
  stdio: "inherit",
  env: process.env,
});

if (result.error) {
  throw result.error;
}

process.exit(result.status ?? 1);
