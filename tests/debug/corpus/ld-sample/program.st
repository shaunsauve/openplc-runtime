(* corpus: ld-sample
   criteria: A024
   fc: FC-TST-1 FC-TST-14 FC-DI-18 FC-LANG-2
   witness: Main PROG0.witness %QD0
   ASSUMED: R1 keeps this corpus ST-only, so the LD project itself lives with
     the editor; this file is the ST its transpiler emits for a two-rung
     ladder (start/stop seal-in, then a lamp), and program.st.origin.json is
     the source map it emits (ADR-002 D6), mapping each ST statement to its
     rung and element. The witness rung is added by hand (corpus contract).

   Rung 0: motor := (start OR motor) AND NOT stop.   Rung 1: lamp := motor.
*)

PROGRAM LD_Main
  VAR
    witness AT %QD0 : DINT;
    start AT %IX0.0 : BOOL;
    stop AT %IX0.1 : BOOL;
    motor AT %QX0.0 : BOOL;
    lamp AT %QX0.1 : BOOL;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  motor := (start OR motor) AND NOT stop;                  (*@RUNG0*)
  lamp := motor;                                           (*@RUNG1*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : LD_Main;
  END_RESOURCE
END_CONFIGURATION
