(* S8 of ADR-001 D1.3 (A134): both tasks call the one FB instance G_CNT.
   STruCpp must refuse this at compile time with a diagnostic naming G_CNT;
   build_all.sh fails if it compiles or the diagnostic does not name it.
   expect-diagnostic: G_CNT
*)

FUNCTION_BLOCK Counter
  VAR_INPUT
    inc : BOOL;
  END_VAR
  VAR_OUTPUT
    count : DINT;
  END_VAR
  IF inc THEN
    count := count + 1;
  END_IF;
END_FUNCTION_BLOCK

PROGRAM FastProg
  VAR
    witness AT %QD0 : DINT;
  END_VAR
  VAR_EXTERNAL
    G_CNT : Counter;
    G_IN : BOOL;
  END_VAR
  witness := witness + 1;
  G_CNT(inc := G_IN);
END_PROGRAM

PROGRAM SlowProg
  VAR
    witness AT %QD1 : DINT;
    cnt_seen AT %QD4 : DINT;
  END_VAR
  VAR_EXTERNAL
    G_CNT : Counter;
    G_IN : BOOL;
  END_VAR
  witness := witness + 1;
  G_CNT(inc := G_IN);
  cnt_seen := G_CNT.count;
END_PROGRAM

CONFIGURATION Config0
  VAR_GLOBAL
    G_CNT : Counter;
    G_IN AT %IX0.0 : BOOL;
  END_VAR
  RESOURCE Res0 ON PLC
    TASK Fast(INTERVAL := T#10ms, PRIORITY := 1);
    TASK Slow(INTERVAL := T#100ms, PRIORITY := 2);
    PROGRAM PROG_FAST WITH Fast : FastProg;
    PROGRAM PROG_SLOW WITH Slow : SlowProg;
  END_RESOURCE
END_CONFIGURATION
