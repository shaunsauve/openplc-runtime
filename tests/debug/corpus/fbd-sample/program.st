(* corpus: fbd-sample
   criteria: A024
   fc: FC-TST-1 FC-TST-14 FC-DI-18 FC-LANG-2
   witness: Main PROG0.witness %QD0
   ASSUMED: as ld-sample: this is the ST the editor's transpiler emits for a
     three-block FBD network (ADD -> LIMIT -> MOVE), with its source map in
     program.st.origin.json. The witness statement is added by hand.
*)

PROGRAM FBD_Main
  VAR
    witness AT %QD0 : DINT;
    a AT %IW0 : INT;
    b AT %IW1 : INT;
    out AT %QW0 : INT;
    _tmp_add : INT;
    _tmp_lim : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  _tmp_add := ADD(a, b);                                   (*@BLK_ADD*)
  _tmp_lim := LIMIT(0, _tmp_add, 1000);                    (*@BLK_LIMIT*)
  out := MOVE(_tmp_lim);                                   (*@BLK_MOVE*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : FBD_Main;
  END_RESOURCE
END_CONFIGURATION
