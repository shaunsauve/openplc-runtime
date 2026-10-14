(* corpus: ton-timer
   criteria: A036 A052 A053 A054 A083 A152 A169 A170
   fc: FC-TST-1 FC-TST-4 FC-ED-8 FC-HLT-1 FC-HLT-2 FC-HLT-7 FC-HLT-9 FC-HLT-14
     FC-HLT-15 FC-HLT-16 FC-ST-4 FC-TR-7
   witness: Main PROG0.witness %QD0
   ASSUMED: TIME_TO_DINT yields milliseconds, so %QD1 (et_ms) and %QD2 (vt_ms)
     carry the TON's elapsed time and the task's virtual time in ms.

   A 5 s TON started from a plant input, its Q driving an output, plus the
   three clock reads ADR-004 distinguishes: TIME() and the RTC FB (virtual,
   frozen while halted, D4) and CURRENT_DT() (wall, live while halted, D5.1).
*)

PROGRAM Timer
  VAR
    witness AT %QD0 : DINT;
    et_ms AT %QD1 : DINT;
    vt_ms AT %QD2 : DINT;
    lamp AT %QX0.0 : BOOL;
    start_in AT %IX0.0 : BOOL;
    t5 : TON;
    clock : RTC;
    now_v : DT;
    now_w : DT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  t5(IN := start_in, PT := T#5s);                          (*@TON*)
  lamp := t5.Q;                                            (*@LAMP*)
  et_ms := TIME_TO_DINT(t5.ET);                            (*@ET*)
  vt_ms := TIME_TO_DINT(TIME());                           (*@VT*)
  clock(IN := FALSE, PDT := DT#1970-01-01-00:00:00);       (*@RTC*)
  now_v := clock.CDT;                                      (*@NOW_V*)
  now_w := CURRENT_DT();                                   (*@NOW_W*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Timer;
  END_RESOURCE
END_CONFIGURATION
