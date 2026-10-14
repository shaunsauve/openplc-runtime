(* corpus: online-change-pair
   criteria: A076 A143 A144 A145 A146 A148 A183 A184 A215 A216
   fc: FC-TST-1 FC-TST-6 FC-BP-13 FC-DAP-5 FC-DOC-3 FC-ED-14 FC-FUT-3 FC-FUT-5
     FC-FUT-6
   witness: Main PROG0.witness %QD0

   The base program of an online-change pair, and three variants:
     variant-inplace.st       one statement changed (out_step := 2): in place
     variant-layout.st        Cell gains a member (re-initialise Cell instances)
                              and @CHANGED edits out_step to 3
     variant-incompatible.st  the program instance is renamed PROG0 -> PROG1:
                              refused CONFIG_CHANGED (ADR-006 D1.2, Proposed)
   counter must continue from its pre-change value across an in-place change (A143).
   Lines @CHANGED and @CELL_Y are the ones the variants edit; @COUNTER,
   @CALL_CELL and @LAST are unchanged (A146: 3 re-verify, 2 do not).
*)

FUNCTION_BLOCK Cell
  VAR_INPUT
    x : INT;
  END_VAR
  VAR_OUTPUT
    y : INT;
  END_VAR
  VAR
    acc : INT;
  END_VAR
  acc := acc + x;                                          (*@CELL_ACC*)
  y := acc MOD 1000;                                       (*@CELL_Y*)
END_FUNCTION_BLOCK

PROGRAM Changer
  VAR
    witness AT %QD0 : DINT;
    counter AT %QD1 : DINT;
    out_step AT %QW0 : INT;
    out_cell AT %QW1 : INT;
    cell : Cell;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  counter := counter + 1;                                  (*@COUNTER*)
  out_step := 1;                                           (*@CHANGED*)
  cell(x := out_step);                                     (*@CALL_CELL*)
  out_cell := cell.y;                                      (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Changer;
  END_RESOURCE
END_CONFIGURATION
