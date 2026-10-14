(* corpus: sparse-lines
   criteria: A018
   fc: FC-TST-1 FC-BP-3 FC-DI-9
   witness: Main PROG0.witness %QD0

   Declarations, comments and blank lines between statements, and a line after
   a POU's last statement. Breakpoint placement (F026, ADR-002 D3) moves an
   unlisted line to the next probe-bearing line of the same POU, or leaves it
   unverified with NO_CODE when the POU has none after it.
*)

FUNCTION_BLOCK Helper
  VAR_INPUT
    x : INT;                                               (*@DECL*)
  END_VAR
  VAR_OUTPUT
    y : INT;
  END_VAR
  y := x * 3;                                              (*@HELPER_FIRST*)
END_FUNCTION_BLOCK

PROGRAM SparseLines
  VAR
    witness AT %QD0 : DINT;
    a AT %QW0 : INT;
    b AT %QW1 : INT;
    h : Helper;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  (* a block comment
     over two lines *)                                     (*@CMT2*)

  a := a + 1;                                              (*@A*)
  // a line comment                                        (*@LC*)


  h(x := a);                                               (*@CALL_H*)
  b := h.y;                                                (*@B*)
  (* trailing comment after the last statement *)          (*@TAIL*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : SparseLines;
  END_RESOURCE
END_CONFIGURATION
