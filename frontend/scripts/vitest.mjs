// Runs Vitest from a canonical working directory.
//
// On Windows, a lowercase drive letter in the current directory ("d:\..." - common in VS Code
// terminals) makes Vitest load its own module under two path spellings, and hooks registered
// in setup files then fail with "Vitest failed to find the current suite". Re-launching with
// an uppercase drive letter avoids it; on other platforms this is a plain pass-through.
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";

const cwd = process.cwd().replace(/^[a-z]:/, (drive) => drive.toUpperCase());
const require = createRequire(path.join(cwd, "package.json"));
const bin = path.join(path.dirname(require.resolve("vitest/package.json")), "vitest.mjs");
const result = spawnSync(process.execPath, [bin, ...process.argv.slice(2)], { cwd, stdio: "inherit" });
process.exit(result.status ?? 1);
