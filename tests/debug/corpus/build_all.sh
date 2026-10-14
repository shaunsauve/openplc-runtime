#!/usr/bin/env bash
# Compile every corpus program with STruCpp, then validate every oracle.
#
# Usage: tests/debug/corpus/build_all.sh
# Env:   STRUCPP  STruCpp checkout (default: ../STruCpp beside this repo)
#        OUT      output directory (default: build/debug-corpus in this repo); per program
#                 <name>/debug/ and <name>/<variant>.debug/ hold the ADR-002 D1 files the
#                 harness's inspect steps and ~build/~lines/~leaves symbols read
# Exit:  0 when every program compiles (or fails as its header's `build: expect-fail` says)
#        and validate.py passes; 1 otherwise. Per program: OK, FAIL, XFAIL, or XPASS
#        (an expect-fail program that now compiles: remove its marker). A reject-*.st file
#        must fail to compile (a construct the compiler refuses), leave no generated code, and,
#        when its header has `expect-diagnostic: <text>`, name that text in the diagnostic;
#        variant-*.st must compile.
#
# See also:
#   tests/debug/corpus/README.md - program header keys read here (sources, build).
#   tests/debug/corpus/validate.py - oracle and header validation, run last.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
STRUCPP=${STRUCPP:-$REPO/../STruCpp}
OUT=${OUT:-$REPO/build/debug-corpus}
CLI="$STRUCPP/dist/node/cli.js"

if [ ! -f "$CLI" ]; then
  echo "== building STruCpp in $STRUCPP (dist/ missing)"
  (cd "$STRUCPP" && npm ci --no-audit --no-fund --silent && npm run build --silent) || {
    echo "FAIL: cannot build STruCpp at $STRUCPP"; exit 1; }
fi
DEBUG_FLAG=()
if node "$CLI" --help 2>&1 | grep -q -- '--debug-artifacts'; then DEBUG_FLAG=(--debug-artifacts); fi

header() {  # header <file> <key>: the value of a header key, first occurrence
  sed -n '1,/\*)/p' "$1" | sed -n "s/^ *$2: *//p" | head -1
}

ok=0; fail=0; xfail=0; xpass=0
for dir in "$HERE"/*/; do
  dir=${dir%/}; name=$(basename "$dir")
  [ -f "$dir/program.st" ] || continue
  out="$OUT/$name"; rm -rf "$out"; mkdir -p "$out"
  expect=$(header "$dir/program.st" build)
  srcs=$(header "$dir/program.st" sources); srcs=${srcs:-program.st}
  libargs=()
  status=0
  if [ -x "$dir/gen.py" ] || [ -f "$dir/gen.py" ]; then
    python3 "$dir/gen.py" --check >"$out/gen.log" 2>&1 || status=1
  fi
  if [ -d "$dir/lib" ]; then
    node "$CLI" --compile-lib "$dir/lib" -o "$out/lib" --lib-name "${name}-lib" --no-source >"$out/lib.log" 2>&1 || status=1
    libargs=(-L "$out/lib")
  fi
  files=(); for s in $srcs; do files+=("$dir/$s"); done
  dbg=(); [ ${#DEBUG_FLAG[@]} -gt 0 ] && dbg=("${DEBUG_FLAG[@]}" "$out/debug")
  node "$CLI" "${files[@]}" -o "$out/program.cpp" ${libargs[@]+"${libargs[@]}"} ${dbg[@]+"${dbg[@]}"} >"$out/build.log" 2>&1 || status=1
  for v in "$dir"/variant-*.st; do
    [ -f "$v" ] || continue
    vn=$(basename "$v" .st)
    vdbg=(); [ ${#DEBUG_FLAG[@]} -gt 0 ] && vdbg=("${DEBUG_FLAG[@]}" "$out/$vn.debug")
    node "$CLI" "$v" -o "$out/$vn.cpp" ${libargs[@]+"${libargs[@]}"} ${vdbg[@]+"${vdbg[@]}"} >"$out/$vn.log" 2>&1 || status=1
  done
  for r in "$dir"/reject-*.st; do
    [ -f "$r" ] || continue
    rn=$(basename "$r" .st)
    if node "$CLI" "$r" -o "$out/$rn.cpp" >"$out/$rn.log" 2>&1; then
      echo "      $rn compiled but must be refused"; status=1
    fi
    if [ -s "$out/$rn.cpp" ]; then echo "      $rn was refused but left $rn.cpp"; status=1; fi
    diag=$(header "$r" expect-diagnostic)
    if [ -n "$diag" ] && ! grep -qF -- "$diag" "$out/$rn.log"; then
      echo "      $rn: the refusal does not name '$diag'"; status=1
    fi
  done
  case "$expect" in
    expect-fail*)
      if [ $status -ne 0 ]; then echo "XFAIL $name (${expect#expect-fail })"; xfail=$((xfail+1))
      else echo "XPASS $name: compiles now, remove its build: expect-fail"; xpass=$((xpass+1)); fi ;;
    *)
      if [ $status -eq 0 ]; then echo "OK    $name"; ok=$((ok+1))
      else echo "FAIL  $name (logs: $out/*.log)"; tail -5 "$out"/*.log | sed 's/^/      /'; fail=$((fail+1)); fi ;;
  esac
done

echo "== validate oracles"
python3 "$HERE/validate.py"; vstatus=$?
echo "build: $ok ok, $fail fail, $xfail expected-fail, $xpass unexpected-pass; validate exit $vstatus"
[ $fail -eq 0 ] && [ $xpass -eq 0 ] && [ $vstatus -eq 0 ]
