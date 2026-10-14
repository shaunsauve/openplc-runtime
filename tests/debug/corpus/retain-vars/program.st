(* corpus: retain-vars
   criteria: A044 A201
   fc: FC-TST-1 FC-ED-2 FC-RC-3 FC-RC-4 FC-RC-5 FC-RC-7 FC-RC-9 FC-RC-10 FC-ST-4
   witness: Main PROG0.witness %QD0

   RETAIN and non-RETAIN variables a scenario changes, mirrored to outputs so
   the plant sees what each restart kind kept.
*)

PROGRAM Retain
  VAR RETAIN
    kept : DINT;
  END_VAR
  VAR
    witness AT %QD0 : DINT;
    kept_out AT %QD1 : DINT;
    plain_out AT %QD2 : DINT;
    plain : DINT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  kept_out := kept;                                        (*@KEPT*)
  plain_out := plain;                                      (*@PLAIN*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Retain;
  END_RESOURCE
END_CONFIGURATION
