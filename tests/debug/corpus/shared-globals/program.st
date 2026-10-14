(* corpus: shared-globals
   criteria: A204
   fc: FC-TST-1 FC-MT-2 FC-MT-6 FC-PERF-3 FC-RC-3 FC-RC-4 FC-RC-5
   witness: TaskA PROG_A.witness %QD0
   witness: TaskB PROG_B.witness %QD1

   Two tasks under STRUCPP_THREADED sharing the composite global G_ARR and the
   scalar G_S, built to reach every shared-global lock site of ADR-001 D1.3:
     S1 scalar read           S5 composite write, F(i) subscript
     S2 scalar write via F    S6 composite bit write, F(i) subscript
     S3 scalar capture via F  S7 units[F(i)](Q => G_ARR[1]) capture
     S4 composite read with the property getter G_FB.P, and G_ARR[F(i)]
     plus G_A[G_B[1]] (a global subscripting a global).
   F has its statement on its own line (F_BODY), so a breakpoint there is
   inside the subscript call. Task B reads and writes G_ARR every scan.
   S8 (calling the shared FB instance G_FB from both tasks) is
   reject-shared-fb-call.st, which the compiler must refuse.
*)

FUNCTION F : INT
  VAR_INPUT
    i : INT;
  END_VAR
  F := i MOD 4;                                            (*@F_BODY*)
END_FUNCTION

FUNCTION_BLOCK Gauge
  VAR
    level : DINT;
  END_VAR
  PROPERTY PUBLIC P : DINT
    GET
      P := level;                                          (*@GETTER*)
    END_GET
  END_PROPERTY
END_FUNCTION_BLOCK

FUNCTION_BLOCK Pulse
  VAR_INPUT
    IN : INT;
  END_VAR
  VAR_OUTPUT
    Q : DINT;
  END_VAR
  Q := INT_TO_DINT(IN) + 1;
END_FUNCTION_BLOCK

PROGRAM ProgA
  VAR
    witness AT %QD0 : DINT;
    out_x AT %QD2 : DINT;
    i : INT;
    x : DINT;
    y : DINT;
    z : DINT;
    units : ARRAY[0..3] OF Pulse;
  END_VAR
  VAR_EXTERNAL
    G_ARR : ARRAY[0..7] OF DINT;
    G_A : ARRAY[0..3] OF DINT;
    G_B : ARRAY[0..3] OF INT;
    G_W : ARRAY[0..3] OF WORD;
    G_S : DINT;
    G_FB : Gauge;
  END_VAR
  witness := witness + 1;                                  (*@A_WITNESS*)
  y := G_S;                                                (*@S1*)
  G_S := INT_TO_DINT(F(i));                                (*@S2*)
  units[0](IN := F(i), Q => G_S);                          (*@S3*)
  x := G_FB.P;                                             (*@S4*)
  x := x + G_ARR[F(i)];                                    (*@S4_ARR*)
  G_ARR[F(i)] := x;                                        (*@S5*)
  G_W[F(i)].3 := TRUE;                                     (*@S6*)
  units[F(i)](IN := 1, Q => G_ARR[1]);                     (*@S7*)
  z := G_A[G_B[1]];                                        (*@NESTED*)
  out_x := x + y + z;
  i := i + 1;                                              (*@A_LAST*)
END_PROGRAM

PROGRAM ProgB
  VAR
    witness AT %QD1 : DINT;
    k : INT;
  END_VAR
  VAR_EXTERNAL
    G_ARR : ARRAY[0..7] OF DINT;
  END_VAR
  witness := witness + 1;                                  (*@B_WITNESS*)
  G_ARR[k] := G_ARR[k] + 1;                                (*@B_RW*)
  k := (k + 1) MOD 8;
END_PROGRAM

CONFIGURATION Config0
  VAR_GLOBAL
    G_ARR : ARRAY[0..7] OF DINT;
    G_A : ARRAY[0..3] OF DINT := [10, 11, 12, 13];
    G_B : ARRAY[0..3] OF INT := [0, 1, 2, 3];
    G_W : ARRAY[0..3] OF WORD;
    G_S : DINT;
    G_FB : Gauge;
  END_VAR
  RESOURCE Res0 ON PLC
    TASK TaskA(INTERVAL := T#20ms, PRIORITY := 1);
    TASK TaskB(INTERVAL := T#10ms, PRIORITY := 2);
    PROGRAM PROG_A WITH TaskA : ProgA;
    PROGRAM PROG_B WITH TaskB : ProgB;
  END_RESOURCE
END_CONFIGURATION
