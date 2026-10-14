(* corpus: multi-stmt-line
   criteria: A015
   fc: FC-TST-1 FC-DI-2
   witness: Main PROG0.witness %QD0

   Lines holding several statements, and one statement spanning four lines.
   Each statement has its own probe and column range (ADR-002 D3, F013).
*)

PROGRAM MultiStmt
  VAR
    witness AT %QD0 : DINT;
    a AT %QW0 : INT;
    b AT %QW1 : INT;
    c AT %QW2 : INT;
    total AT %QD1 : DINT;
    in_a AT %IW0 : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  a := in_a; b := a + 1; c := b + 1;                       (*@THREE*)
  total := INT_TO_DINT(a)                                  (*@SPAN_START*)
           + INT_TO_DINT(b)
           + INT_TO_DINT(c)
           + 1;                                            (*@SPAN_END*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : MultiStmt;
  END_RESOURCE
END_CONFIGURATION
