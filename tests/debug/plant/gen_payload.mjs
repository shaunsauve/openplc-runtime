// Compile one corpus program with STruCpp's library API into the payload the editor uploads
// (core/generated/): generated.hpp, one .cpp per POU plus configuration.cpp,
// generated_debug.cpp, debug-info.json, defines.h and strucpp_runtime/include. The CLI writes one
// concatenated .cpp only, which is not the runtime's input.
//
// Usage: node tests/debug/plant/gen_payload.mjs <strucpp-checkout> <program.st> <out-dir>
//
// See also:
//   tests/debug/plant/e2e.sh - builds the runtime with this payload and runs the harness.
import { readFileSync, writeFileSync, mkdirSync, cpSync } from "fs";
import { join, basename, resolve } from "path";
import { createHash } from "crypto";

const [strucppDir, stPath, outDir] = process.argv.slice(2);
if (!outDir) {
  console.error("usage: gen_payload.mjs <strucpp-checkout> <program.st> <out-dir>");
  process.exit(2);
}
const { compile } = await import(resolve(strucppDir, "dist/index.js"));
const { discoverStlibs } = await import(resolve(strucppDir, "dist/node/library-loader.js"));

const source = readFileSync(stPath, "utf-8");
const md5 = createHash("md5").update(source).digest("hex");
const result = compile(source, {
  headerFileName: "generated.hpp",
  fileName: basename(stPath),
  libraries: discoverStlibs(resolve(strucppDir, "libs")),
  md5,
  // Explicit: STruCpp's default has flipped between builds of dist/, and a probe-off payload
  // silently turns every run-control check into NO_PROBES.
  debugProbes: true,
});
if (!result.success) {
  for (const e of result.errors) console.error(`${e.line}:${e.column}: ${e.message}`);
  process.exit(1);
}
mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, "generated.hpp"), result.headerCode);
for (const f of result.cppFiles) writeFileSync(join(outDir, f.name), f.content);
if (result.debugTableCpp !== undefined) {
  writeFileSync(join(outDir, "generated_debug.cpp"), result.debugTableCpp);
}
if (result.debugInfo !== undefined) {
  // ADR-002 D1: the gateway reads it from core/generated/; the harness from a build directory.
  writeFileSync(join(outDir, "debug-info.json"), JSON.stringify(result.debugInfo));
}
writeFileSync(join(outDir, "defines.h"), `#define PROGRAM_MD5 "${md5}"\n`);
cpSync(resolve(strucppDir, "src/runtime/include"), join(outDir, "strucpp_runtime/include"), {
  recursive: true,
});
console.log(`${basename(stPath)}: ${result.cppFiles.map((f) => f.name).join(" ")}`);
