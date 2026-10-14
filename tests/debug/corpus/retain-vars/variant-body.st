(* retain-vars variant: body edit only (plain_out gets plain + 1). Uploaded as a
   full download by the A215/A216 scenarios. *)

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
  plain_out := plain + 1;                                  (*@PLAIN*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Retain;
  END_RESOURCE
END_CONFIGURATION
