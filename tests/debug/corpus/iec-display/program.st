(* corpus: iec-display
   criteria: A090 A195
   fc: FC-TST-1 FC-ED-5 FC-IN-3 FC-IN-4 FC-IN-5 FC-IN-11
   witness: Main PROG0.witness %QD0

   Values that must display in IEC form: TIME, DATE, DT, TOD, an enum, a
   STRING, BYTE/WORD/DWORD/LWORD (hex), and a TON and a CTUD shown as named
   fields.
*)

TYPE
  Color : (RED, GREEN, BLUE);
END_TYPE

PROGRAM Display
  VAR
    witness AT %QD0 : DINT;
    start_in AT %IX0.0 : BOOL;
    up_in AT %IX0.1 : BOOL;
    down_in AT %IX0.2 : BOOL;
    done AT %QX0.0 : BOOL;
    cv_out AT %QW0 : INT;
    t : TIME := T#1s200ms;
    d : DATE := D#2026-10-13;
    dt1 : DT := DT#2026-10-13-12:30:00;
    tod1 : TOD := TOD#12:30:15;
    col : Color := GREEN;
    name : STRING(20) := 'pump';
    bt : BYTE := 16#0F;
    wo : WORD := 16#00FF;
    dw : DWORD := 16#DEADBEEF;
    lw : LWORD := 16#0123456789ABCDEF;
    tmr : TON;
    cnt : CTUD;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  tmr(IN := start_in, PT := T#5s);                         (*@TON*)
  cnt(CU := up_in, CD := down_in, R := FALSE, LD := FALSE, PV := 10);
  done := tmr.Q;
  cv_out := cnt.CV;                                        (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Display;
  END_RESOURCE
END_CONFIGURATION
