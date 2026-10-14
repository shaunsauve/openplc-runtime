(* corpus: print-builtin
   criteria: A025 A065 A100
   fc: FC-TST-1 FC-DI-17 FC-TR-2
   witness: Main PROG0.witness %QD0
   build: expect-fail F089: no runtime print/log builtin exists in STruCpp yet
   ASSUMED: the builtin is LOG_PRINT(msg : STRING) and is non-blocking; rename
     here when F089 names it.

   Calls the runtime print/log builtin; the plant selects the rate as
   messages per scan (%IW0, 0..1000), so a 1 ms task reaches 1,000 msg/s at 1
   and 10x that at 10 (A099's rates, which A100 reuses).
*)

PROGRAM Printer
  VAR
    witness AT %QD0 : DINT;
    per_scan AT %IW0 : INT;
    sent AT %QD1 : DINT;
    i : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  FOR i := 1 TO per_scan DO
    LOG_PRINT(CONCAT('scan ', DINT_TO_STRING(witness)));   (*@PRINT*)
    sent := sent + 1;
  END_FOR;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Printer;
  END_RESOURCE
END_CONFIGURATION
