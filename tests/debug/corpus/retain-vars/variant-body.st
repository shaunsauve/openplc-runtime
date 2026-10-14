(* retain-vars variant: body edit only (plain_out gets plain + 1). Uploaded as a
   full download by the A215/A216 scenarios. *)

PROGRAM RetainProg
  VAR RETAIN
    kept : DINT;
    kcount : DINT;
  END_VAR
  VAR
    witness AT %QD0 : DINT;
    kept_out AT %QD1 : DINT;
    plain_out AT %QD2 : DINT;
    kcount_out AT %QD3 : DINT;
    pcount_out AT %QD4 : DINT;
    kfirst AT %QD5 : DINT;
    load AT %IX0.0 : BOOL;
    set_k AT %ID0 : DINT;
    set_p AT %ID1 : DINT;
    plain : DINT;
    pcount : DINT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  IF witness = 1 THEN
    kfirst := kcount;                                      (*@KFIRST*)
  END_IF;
  IF load THEN
    kept := set_k;
    kcount := set_k;
    plain := set_p;
    pcount := set_p;
  ELSE
    kcount := kcount + 1;
    pcount := pcount + 1;
  END_IF;
  kcount_out := kcount;
  pcount_out := pcount;
  kept_out := kept;                                        (*@KEPT*)
  plain_out := plain + 1;                                  (*@PLAIN*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : RetainProg;
  END_RESOURCE
END_CONFIGURATION
