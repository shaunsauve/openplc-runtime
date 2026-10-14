(* corpus: loop-bodies
   criteria: A039 A046 A094 A099 A103 A104 A108 A179 A180 A196 A197 A205
   fc: FC-TST-1 FC-TST-6 FC-BP-1 FC-BP-4 FC-BP-5 FC-BP-7 FC-BP-8 FC-BP-9
     FC-BP-12 FC-ED-3 FC-ED-6 FC-ED-7 FC-IN-7 FC-IN-9 FC-PERF-3 FC-RC-8
     FC-RC-12 FC-ST-1 FC-ST-2 FC-ST-3 FC-ST-5 FC-ST-6 FC-ST-7 FC-ST-8
   witness: Main PROG0.witness %QD0
   ASSUMED: a step off a loop body's last statement lands on the loop header
     line (the ADR-002 loop-condition probe, kind 1) while the loop continues,
     and on the statement after the loop once it ends.

   FOR (10 passes), WHILE (3 passes), REPEAT (3 passes) and a FOR left by EXIT
   on its 5th pass. Every pass count is fixed, so hit counts per scan are exact.
*)

PROGRAM Loops
  VAR
    witness AT %QD0 : DINT;
    out_sum AT %QD1 : DINT;
    i : INT;
    j : INT;
    n : INT;
    r : INT;
    sum : DINT;
    last : INT;
    found : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  sum := 0;
  FOR i := 1 TO 10 DO                                      (*@FOR*)
    sum := sum + i;                                        (*@FOR_BODY*)
    last := i;                                             (*@FOR_LAST*)
  END_FOR;
  n := 0;                                                  (*@AFTER_FOR*)
  WHILE n < 3 DO                                           (*@WHILE*)
    n := n + 1;                                            (*@WHILE_BODY*)
  END_WHILE;
  r := 0;                                                  (*@AFTER_WHILE*)
  REPEAT
    r := r + 2;                                            (*@REPEAT_BODY*)
  UNTIL r >= 6                                             (*@UNTIL*)
  END_REPEAT;
  found := -1;                                             (*@AFTER_REPEAT*)
  FOR j := 0 TO 9 DO                                       (*@EXIT_FOR*)
    IF j = 4 THEN                                          (*@EXIT_IF*)
      found := j;
      EXIT;                                                (*@EXIT*)
    END_IF;
  END_FOR;
  out_sum := sum + found;                                  (*@TAIL*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Loops;
  END_RESOURCE
END_CONFIGURATION
