(* online-change-pair variant: layout change, Cell gains the member peak and out_step becomes 3. Not a corpus program of its own; the
   scenarios apply it to a running program.st. *)

FUNCTION_BLOCK Cell
  VAR_INPUT
    x : INT;
  END_VAR
  VAR_OUTPUT
    y : INT;
  END_VAR
  VAR
    acc : INT;
    peak : INT;
  END_VAR
  acc := acc + x;                                          (*@CELL_ACC*)
  IF acc > peak THEN
    peak := acc;
  END_IF;
  y := peak MOD 1000;                                      (*@CELL_Y*)
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
  out_step := 3;                                           (*@CHANGED*)
  cell(x := out_step);                                     (*@CALL_CELL*)
  out_cell := cell.y;                                      (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Changer;
  END_RESOURCE
END_CONFIGURATION
