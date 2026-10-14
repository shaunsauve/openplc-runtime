(* corpus: multi-task
   criteria: A076 A130 A131 A132 A133 A134 A175 A176 A191
   fc: FC-TST-1 FC-TST-6 FC-DAP-5 FC-DOC-3 FC-ED-11 FC-ED-18 FC-MT-1 FC-MT-2
     FC-MT-3 FC-MT-4 FC-MT-5 FC-MT-6 FC-MT-8
   witness: Fast PROG_FAST.witness %QD0
   witness: Slow PROG_SLOW.witness %QD1
   ASSUMED: "sharing an FB instance" is the global instance G_CNT, called by the
     fast task and read by the slow one. Calling one FB instance from both
     tasks is refused at compile time under STRUCPP_THREADED (ADR-001 D1.3,
     site S8), so the POU both tasks reach is the function Bump (A134 rule 2).

   A fast (10 ms) and a slow (100 ms) task, each with its own scan witness,
   sharing the global G_SHARED, the FB instance G_CNT and the function Bump.
   %IX0.0 (located, configuration scope) feeds both tasks (A134 rule 4).
*)

FUNCTION Bump : DINT
  VAR_INPUT
    v : DINT;
  END_VAR
  Bump := v + 1;                                           (*@BUMP*)
END_FUNCTION

FUNCTION_BLOCK Counter
  VAR_INPUT
    inc : BOOL;
  END_VAR
  VAR_OUTPUT
    count : DINT;
  END_VAR
  IF inc THEN
    count := count + 1;                                    (*@COUNTER*)
  END_IF;
END_FUNCTION_BLOCK

PROGRAM FastProg
  VAR
    witness AT %QD0 : DINT;
    fast_out AT %QD2 : DINT;
  END_VAR
  VAR_EXTERNAL
    G_SHARED : DINT;
    G_CNT : Counter;
    G_IN : BOOL;
  END_VAR
  witness := witness + 1;                                  (*@FAST_WITNESS*)
  G_SHARED := Bump(G_SHARED);                              (*@FAST_BUMP*)
  G_CNT(inc := G_IN);                                      (*@FAST_CNT*)
  fast_out := G_SHARED;                                    (*@FAST_LAST*)
END_PROGRAM

PROGRAM SlowProg
  VAR
    witness AT %QD1 : DINT;
    slow_out AT %QD3 : DINT;
    cnt_seen AT %QD4 : DINT;
    local : DINT;
  END_VAR
  VAR_EXTERNAL
    G_SHARED : DINT;
    G_CNT : Counter;
  END_VAR
  witness := witness + 1;                                  (*@SLOW_WITNESS*)
  local := Bump(local);                                    (*@SLOW_BUMP*)
  slow_out := G_SHARED;                                    (*@SLOW_READ*)
  cnt_seen := G_CNT.count;                                 (*@SLOW_LAST*)
END_PROGRAM

CONFIGURATION Config0
  VAR_GLOBAL
    G_SHARED : DINT;
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
