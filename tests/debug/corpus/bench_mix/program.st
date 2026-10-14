(* corpus: bench_mix
   criteria: A010 A027 A211 A212 A213
   fc: FC-TST-1 FC-PERF-1 FC-PERF-6 FC-BP-7 FC-BP-8 FC-BP-11 FC-TR-1 FC-TR-8
   tags: bench
   witness: Main PROG0.witness %QD0

   Copied from the orchestration root's bench/st/bench_mix.st (commit c0465f0)
   with two corpus changes only: the scan witness (first statement) and the
   program instance renamed Inst0 -> PROG0. The A010/A027 bench itself runs
   bench/st; this copy is what corpus criteria run.

   bench_mix - ADR-001 D10.1 realistic scan.
   TON/TOF timers, an IF/ELSIF ladder, a CASE, struct and array access, two
   scalar and one composite shared global (VAR_EXTERNAL), 16 integer divisions
   by a variable, and a 64-iteration FOR loop. One "scan" of that logic is
   repeated REPS times per task scan (the D10.1 sizing knob).
   Shared-global subscripts are plain locals, so ADR-001 D1.3(1) hoisting is a
   no-op here and the probe-off build is byte-identical to the base build.
*)


TYPE
  Axis : STRUCT
    pos : DINT;
    vel : INT;
    target : DINT;
    ok : BOOL;
  END_STRUCT;
END_TYPE

FUNCTION Lim : DINT
  VAR_INPUT v : DINT; hi : DINT; END_VAR
  IF v > hi THEN
    Lim := hi;
  ELSE
    Lim := v;
  END_IF;
END_FUNCTION

FUNCTION_BLOCK BenchRamp
  VAR_INPUT target : DINT; END_VAR
  VAR_OUTPUT out : DINT; END_VAR
  IF out < target THEN
    out := out + 1;
  ELSIF out > target THEN
    out := out - 1;
  END_IF;
END_FUNCTION_BLOCK

FUNCTION_BLOCK Scaler
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR r : BenchRamp; END_VAR
  r(target := Lim(x, 5000));
  y := r.out;
END_FUNCTION_BLOCK

PROGRAM BenchMix
  VAR_EXTERNAL
    gCount : DINT;
    gMode : INT;
    gTable : ARRAY[0..15] OF DINT;
  END_VAR
  VAR
    witness AT %QD0 : DINT;
    t1 : TON;
    t2 : TOF;
    run : BOOL := TRUE;
    ax : ARRAY[0..7] OF Axis;
    buf : ARRAY[0..63] OF DINT;
    rep : DINT; i : DINT; j : DINT; idx : DINT; idx2 : DINT;
    state : DINT; q : DINT; acc : DINT; dv : DINT := 3;
    mode : INT;
    d0 : DINT; d1 : DINT; d2 : DINT; d3 : DINT;
    d4 : DINT; d5 : DINT; d6 : DINT; d7 : DINT;
    sc : Scaler;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  FOR rep := 1 TO 560 DO
    t1(IN := run, PT := T#50ms);
    t2(IN := t1.Q, PT := T#20ms);
    run := NOT t2.Q;
    mode := gMode;
    IF mode = 0 THEN
      state := 0;
    ELSIF mode = 1 THEN
      state := 10;
    ELSIF mode = 2 THEN
      state := state + 1;
    ELSIF mode = 3 THEN
      state := state - 1;
    ELSE
      state := 99;
    END_IF;
    IF state > 1000 THEN
      state := 0;
    END_IF;
    CASE state MOD 4 OF
      0: q := 1;
      1: q := 2;
      2: q := q + 3;
    ELSE
      q := 0;
    END_CASE;
    acc := 0;
    FOR i := 0 TO 63 DO
      buf[i] := i + q;
      j := i MOD 8;
      ax[j].pos := ax[j].pos + buf[i];
      IF ax[j].pos > 100000 THEN
        ax[j].pos := 0;
      END_IF;
      acc := acc + buf[i];
    END_FOR;
    d0 := acc / dv;
    d1 := acc MOD dv;
    d2 := d0 / dv;
    d3 := d0 MOD dv;
    d4 := d2 / dv;
    d5 := d2 MOD dv;
    d6 := d4 / dv;
    d7 := d4 MOD dv;
    d0 := (d0 + d1) / dv;
    d1 := (d2 + d3) / dv;
    d2 := (d4 + d5) / dv;
    d3 := (d6 + d7) / dv;
    d4 := d0 MOD dv;
    d5 := d1 MOD dv;
    d6 := d2 MOD dv;
    d7 := d3 MOD dv;
    ax[rep MOD 8].vel := ax[rep MOD 8].vel + 1;
    ax[rep MOD 8].target := d0 + d4;
    ax[rep MOD 8].ok := ax[rep MOD 8].pos > ax[rep MOD 8].target;
    sc(x := acc);
    idx := rep MOD 16;
    idx2 := state MOD 16;
    gTable[idx] := gTable[idx2] + d6 + d7;
    IF gTable[idx] > 1000000 THEN
      gTable[idx] := 0;
    END_IF;
    gCount := gCount + 1;
    IF gCount > 1000000 THEN
      gCount := 0;
    END_IF;
  END_FOR;
END_PROGRAM

CONFIGURATION Config0
  VAR_GLOBAL
    gCount : DINT;
    gMode : INT := 2;
    gTable : ARRAY[0..15] OF DINT;
  END_VAR
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : BenchMix;
  END_RESOURCE
END_CONFIGURATION
