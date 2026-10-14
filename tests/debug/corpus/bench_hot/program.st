(* corpus: bench_hot
   criteria: A010 A027
   fc: FC-TST-1 FC-PERF-1
   tags: bench
   witness: Main PROG0.witness %QD0

   Copied from the orchestration root's bench/st/bench_hot.st (commit c0465f0)
   with two corpus changes only: the scan witness (first statement) and the
   program instance renamed Inst0 -> PROG0. The A010/A027 bench itself runs
   bench/st; this copy is what corpus criteria run.

   bench_hot - ADR-001 D10.1 probe-dense worst case.
   Hot loop: 12 one-operation statements on INT/DINT/REAL program members and
   FB members, one IF with one statement, no calls in the body.
   OUTER is the D10.1 sizing knob (probe-off scan >= 100 us on the target).
   Accumulators are reset per outer pass so no DINT/INT ever overflows.
*)


FUNCTION Clamp : DINT
  VAR_INPUT v : DINT; lo : DINT; hi : DINT; END_VAR
  IF v < lo THEN
    Clamp := lo;
  ELSIF v > hi THEN
    Clamp := hi;
  ELSE
    Clamp := v;
  END_IF;
END_FUNCTION

FUNCTION_BLOCK Inner
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  y := Clamp(x, 0, 1000000);
END_FUNCTION_BLOCK

FUNCTION_BLOCK Acc
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR nxt : Inner; END_VAR
  nxt(x := x);
  y := nxt.y;
END_FUNCTION_BLOCK

PROGRAM BenchHot
  VAR
    witness AT %QD0 : DINT;
    i : DINT; j : DINT;
    a : DINT; b : DINT; c : DINT; d : DINT; k : DINT; m : DINT;
    e : INT; e2 : INT;
    f : REAL; g : REAL;
    flag : BOOL;
    acc : Acc;
    result : DINT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  FOR j := 1 TO 40 DO
    a := 0;
    c := 0;
    d := 0;
    e := 0;
    e2 := 0;
    f := 0.0;
    acc.x := 0;
    FOR i := 1 TO 1000 DO
      a := a + i;
      b := a - c;
      c := c + 3;
      flag := b > c;
      IF flag THEN
        e := e + 1;
      END_IF;
      f := f + 1.5;
      g := f * 0.25;
      acc.x := acc.x + 1;
      d := d + e;
      e2 := e2 - 2;
      k := i * 2;
      m := k - i;
    END_FOR;
  END_FOR;
  acc();
  result := acc.y + m;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : BenchHot;
  END_RESOURCE
END_CONFIGURATION
