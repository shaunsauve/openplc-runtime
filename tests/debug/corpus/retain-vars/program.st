(* corpus: retain-vars
   criteria: A044 A201 A215 A216
   fc: FC-TST-1 FC-ED-2 FC-RC-3 FC-RC-4 FC-RC-5 FC-RC-7 FC-RC-9 FC-RC-10 FC-ST-4
     FC-FUT-3
   witness: Main PROG0.witness %QD0

   RETAIN and non-RETAIN variables a scenario changes, mirrored to outputs so
   the plant sees what each restart kind kept. While the plant holds %IX0.0
   (load) the program copies %ID0 into kept and the RETAIN counter kcount, and
   %ID1 into plain and the non-RETAIN counter pcount ("driven to a plant-set
   value", A215); released, both counters count scans. kfirst latches kcount
   as the first scan of a load found it, so the plant sees whether a restore
   landed before the first release (ADR-006 D8.6.4).
*)

PROGRAM Retain
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
  plain_out := plain;                                      (*@PLAIN*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Retain;
  END_RESOURCE
END_CONFIGURATION
