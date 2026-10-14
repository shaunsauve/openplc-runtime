(* retain-vars variant: the RETAIN variable kept is retyped DINT -> LINT, so a
   download starts it at its initial value (A215). *)

PROGRAM Retain
  VAR RETAIN
    kept : LINT;
  END_VAR
  VAR
    witness AT %QD0 : DINT;
    kept_out AT %QD1 : DINT;
    plain_out AT %QD2 : DINT;
    plain : DINT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  kept_out := LINT_TO_DINT(kept);                          (*@KEPT*)
  plain_out := plain;                                      (*@PLAIN*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Retain;
  END_RESOURCE
END_CONFIGURATION
