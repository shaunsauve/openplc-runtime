(* corpus: large-50k
   criteria: A021
   fc: FC-TST-1 FC-DI-12
   witness: Main PROG0.witness %QD0
   ASSUMED: "50,000 leaf variables (generated)" is met by declarations that
     expand to 50,000 debug-map leaves (1,000 x 40-field records = 40,000, and
     200 FB instances x 50 members = 10,000), not by 50,000 hand-listed lines;
     that also gives the instance count A021's 64 KiB term depends on (ADR-002
     Open item 1).

   Leaf count: bank (40,000) + cells (200 x (49 members + calls) = 10,000)
   + witness, i, sum = 50,003. Few statements, so few probes.
*)

TYPE
  Rec : STRUCT
    f00 : DINT;
    f01 : DINT;
    f02 : DINT;
    f03 : DINT;
    f04 : DINT;
    f05 : DINT;
    f06 : DINT;
    f07 : DINT;
    f08 : DINT;
    f09 : DINT;
    f10 : DINT;
    f11 : DINT;
    f12 : DINT;
    f13 : DINT;
    f14 : DINT;
    f15 : DINT;
    f16 : DINT;
    f17 : DINT;
    f18 : DINT;
    f19 : DINT;
    f20 : DINT;
    f21 : DINT;
    f22 : DINT;
    f23 : DINT;
    f24 : DINT;
    f25 : DINT;
    f26 : DINT;
    f27 : DINT;
    f28 : DINT;
    f29 : DINT;
    f30 : DINT;
    f31 : DINT;
    f32 : DINT;
    f33 : DINT;
    f34 : DINT;
    f35 : DINT;
    f36 : DINT;
    f37 : DINT;
    f38 : DINT;
    f39 : DINT;
  END_STRUCT;
END_TYPE

FUNCTION_BLOCK FB_Cell
  VAR_INPUT
    seed : DINT;
  END_VAR
  VAR
    m00 : DINT;
    m01 : DINT;
    m02 : DINT;
    m03 : DINT;
    m04 : DINT;
    m05 : DINT;
    m06 : DINT;
    m07 : DINT;
    m08 : DINT;
    m09 : DINT;
    m10 : DINT;
    m11 : DINT;
    m12 : DINT;
    m13 : DINT;
    m14 : DINT;
    m15 : DINT;
    m16 : DINT;
    m17 : DINT;
    m18 : DINT;
    m19 : DINT;
    m20 : DINT;
    m21 : DINT;
    m22 : DINT;
    m23 : DINT;
    m24 : DINT;
    m25 : DINT;
    m26 : DINT;
    m27 : DINT;
    m28 : DINT;
    m29 : DINT;
    m30 : DINT;
    m31 : DINT;
    m32 : DINT;
    m33 : DINT;
    m34 : DINT;
    m35 : DINT;
    m36 : DINT;
    m37 : DINT;
    m38 : DINT;
    m39 : DINT;
    m40 : DINT;
    m41 : DINT;
    m42 : DINT;
    m43 : DINT;
    m44 : DINT;
    m45 : DINT;
    m46 : DINT;
    m47 : DINT;
    m48 : DINT;
    calls : DINT;
  END_VAR
  calls := calls + 1;                                      (*@CELL_FIRST*)
  m00 := seed + calls;
END_FUNCTION_BLOCK

PROGRAM Large
  VAR
    witness AT %QD0 : DINT;
    sum AT %QD1 : DINT;
    bank : ARRAY[0..999] OF Rec;
    cells : ARRAY[0..199] OF FB_Cell;
    i : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  FOR i := 0 TO 199 DO
    cells[i](seed := witness);                             (*@CALL_CELL*)
  END_FOR;
  bank[witness MOD 1000].f00 := witness;                   (*@BANK*)
  sum := cells[0].m00 + bank[0].f00;                       (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Large;
  END_RESOURCE
END_CONFIGURATION
