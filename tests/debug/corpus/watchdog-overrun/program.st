(* corpus: watchdog-overrun
   criteria: A050
   fc: FC-TST-1 FC-HLT-2
   witness: Main PROG0.witness %QD0

   A plant input that makes exactly one scan overrun the watchdog: a 2 s busy
   loop on CURRENT_DT() is 20 due ticks of a 100 ms task, past the stuck-task
   rule's 10 (ADR-004 Context; ADR-008 Context). Once per run.
*)

PROGRAM Overrun
  VAR
    witness AT %QD0 : DINT;
    overrun_in AT %IX0.0 : BOOL;
    busy_out AT %QX0.0 : BOOL;
    done_once : BOOL;
    spins : DINT;
    t0 : DT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  IF overrun_in AND NOT done_once THEN                     (*@OVR_IF*)
    done_once := TRUE;
    busy_out := TRUE;
    t0 := CURRENT_DT();
    WHILE CURRENT_DT() - t0 < T#2s DO
      spins := spins + 1;                                  (*@BUSY*)
    END_WHILE;
  END_IF;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Overrun;
  END_RESOURCE
END_CONFIGURATION
