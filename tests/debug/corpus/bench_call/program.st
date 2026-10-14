(* corpus: bench_call
   criteria: A010 A027
   fc: FC-TST-1 FC-PERF-1
   tags: bench
   witness: Main PROG0.witness %QD0

   Copied from the orchestration root's bench/st/bench_call.st (commit c0465f0)
   with two corpus changes only: the scan witness (first statement) and the
   program instance renamed Inst0 -> PROG0. The A010/A027 bench itself runs
   bench/st; this copy is what corpus criteria run.

   bench_call - ADR-001 D10.1 call-dense worst case.
   FB chain 8 deep (L1 -> ... -> L8), each level 4 statements and one call to
   the next; L8 calls Leaf, a function with 4 locals and a VAR_TEMP.
   Invoked from a FOR loop; REPS is the D10.1 sizing knob.
*)


FUNCTION Leaf : DINT
  VAR_INPUT x : DINT; END_VAR
  VAR a : DINT; b : DINT; c : DINT; d : DINT; END_VAR
  VAR_TEMP t : DINT; END_VAR
  a := x + 1;
  b := a * 2;
  c := b - x;
  d := c + a;
  t := d - b;
  Leaf := t;
END_FUNCTION

FUNCTION_BLOCK L8
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; END_VAR
  n := n + 1;
  IF n > 1000 THEN
    n := 0;
  END_IF;
  y := Leaf(x + n);
END_FUNCTION_BLOCK

FUNCTION_BLOCK L7
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; t : DINT; nxt : L8; END_VAR
  n := x MOD 7;
  t := x + n;
  nxt(x := t);
  y := nxt.y - n;
END_FUNCTION_BLOCK

FUNCTION_BLOCK L6
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; t : DINT; nxt : L7; END_VAR
  n := x MOD 5;
  t := x + n;
  nxt(x := t);
  y := nxt.y - n;
END_FUNCTION_BLOCK

FUNCTION_BLOCK L5
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; t : DINT; nxt : L6; END_VAR
  n := x MOD 3;
  t := x + n;
  nxt(x := t);
  y := nxt.y - n;
END_FUNCTION_BLOCK

FUNCTION_BLOCK L4
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; t : DINT; nxt : L5; END_VAR
  n := x MOD 7;
  t := x - n;
  nxt(x := t);
  y := nxt.y + n;
END_FUNCTION_BLOCK

FUNCTION_BLOCK L3
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; t : DINT; nxt : L4; END_VAR
  n := x MOD 5;
  t := x - n;
  nxt(x := t);
  y := nxt.y + n;
END_FUNCTION_BLOCK

FUNCTION_BLOCK L2
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; t : DINT; nxt : L3; END_VAR
  n := x MOD 3;
  t := x - n;
  nxt(x := t);
  y := nxt.y + n;
END_FUNCTION_BLOCK

FUNCTION_BLOCK L1
  VAR_INPUT x : DINT; END_VAR
  VAR_OUTPUT y : DINT; END_VAR
  VAR n : DINT; t : DINT; nxt : L2; END_VAR
  n := x MOD 2;
  t := x + n;
  nxt(x := t);
  y := nxt.y - n;
END_FUNCTION_BLOCK

PROGRAM BenchCall
  VAR
    witness AT %QD0 : DINT;
    i : DINT;
    s : DINT;
    top : L1;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  s := 0;
  FOR i := 1 TO 9000 DO
    top(x := i);
    s := s + top.y;
  END_FOR;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : BenchCall;
  END_RESOURCE
END_CONFIGURATION
