// Compile one corpus program with STruCpp's library API into the payload the editor uploads
// (core/generated/): generated.hpp, one .cpp per POU plus configuration.cpp,
// generated_debug.cpp, debug-info.json, defines.h and strucpp_runtime/include. The CLI writes one
// concatenated .cpp only, which is not the runtime's input.
//
// Usage: node tests/debug/plant/gen_payload.mjs <strucpp-checkout> <program.st> <out-dir>
//                     [--probes on|off] [--origins <file.origin.json>]
//
//   --probes off   the probe-off build (A019): no probe code in the payload, so the runtime
//                  advertises no `probes` capability and refuses run control NO_PROBES. Default
//                  `on`.
//   --origins F    the LD/FBD transpiler's source map (ADR-002 D6) as `sourceOrigins`, so the
//                  `origins` an editor-produced program carries reach debug-info.json (A024). The
//                  CLI's own flag is `--source-origins`; the payload path is this script's.
//
// Both flags are off by default, so a run without them builds exactly what it built before.
// See also:
//   tests/debug/plant/e2e.sh - builds the runtime with this payload and runs the harness.
//   tests/debug/plant/session_e2e.sh - the deploy step that passes these flags from the oracles.
import { readFileSync, writeFileSync, mkdirSync, cpSync } from "fs";
import { join, basename, resolve } from "path";
import { createHash } from "crypto";

const argv = process.argv.slice(2);
const [strucppDir, stPath, outDir] = argv;
if (!outDir) {
  console.error(
    "usage: gen_payload.mjs <strucpp-checkout> <program.st> <out-dir> " +
      "[--probes on|off] [--origins <file.origin.json>]",
  );
  process.exit(2);
}
let debugProbes = true;
const sourceOrigins = [];
for (let i = 3; i < argv.length; i++) {
  const arg = argv[i];
  if (arg === "--probes") {
    const value = argv[++i];
    if (value !== "on" && value !== "off") {
      console.error(`--probes takes on|off, not ${JSON.stringify(value)}`);
      process.exit(2);
    }
    debugProbes = value === "on";
  } else if (arg === "--origins") {
    const path = argv[++i];
    if (!path) {
      console.error("--origins needs a <file>.origin.json path");
      process.exit(2);
    }
    sourceOrigins.push(JSON.parse(readFileSync(path, "utf-8")));
  } else {
    console.error(`unknown argument ${JSON.stringify(arg)}`);
    process.exit(2);
  }
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
  debugProbes,
  ...(sourceOrigins.length > 0 ? { sourceOrigins } : {}),
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
