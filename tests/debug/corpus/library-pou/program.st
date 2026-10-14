(* corpus: library-pou
   criteria: A020 A040 A196
   fc: FC-TST-1 FC-TST-6 FC-DI-11 FC-ED-3 FC-ST-1 FC-ST-2 FC-ST-3 FC-ST-5
     FC-ST-6 FC-ST-7 FC-ST-8
   witness: Main PROG0.witness %QD0

   Calls to standard-library POUs (R_TRIG, CTU, MAX) and to a user .stlib
   (lib/userlib.st: Debounce and Twice), which build_all.sh compiles with
   --no-source, so those two ship without source.
*)

PROGRAM LibUser
  VAR
    witness AT %QD0 : DINT;
    btn AT %IX0.0 : BOOL;
    count_out AT %QW0 : INT;
    big_out AT %QW1 : INT;
    stable AT %QX0.0 : BOOL;
    edge : R_TRIG;
    presses : CTU;
    db : Debounce;
    doubled : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  edge(CLK := btn);                                        (*@STD_FB*)
  presses(CU := edge.Q, R := FALSE, PV := 100);
  db(raw := btn, n := 3);                                  (*@LIB_FB*)
  stable := db.q;                                          (*@AFTER_LIB_FB*)
  doubled := Twice(presses.CV);                            (*@LIB_FN*)
  big_out := MAX(doubled, 10);                             (*@STD_FN*)
  count_out := presses.CV;                                 (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : LibUser;
  END_RESOURCE
END_CONFIGURATION
