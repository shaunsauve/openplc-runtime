(* corpus: constant-overrun
   criteria: A217
   fc: FC-TST-1 FC-FUT-3 FC-FUT-5
   witness: Main PROG0.witness %QD0
   ASSUMED: "under the watchdog" = each scan overruns by less than the stuck-task
     rule's 10 due ticks; here 150 ms of busy work in a 100 ms task, so every
     tick overruns and the watchdog never trips.

   A task whose scan takes longer than its interval on every tick, selected by
   the plant (%IX0.0), so an online-change window (ADR-006 D3.3) never opens.
*)

PROGRAM Overrunner
  VAR
    witness AT %QD0 : DINT;
    counter AT %QD1 : DINT;
    overrun_on AT %IX0.0 : BOOL;
    spins : DINT;
    t0 : DT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  counter := counter + 1;                                  (*@COUNTER*)
  IF overrun_on THEN
    t0 := CURRENT_DT();
    WHILE CURRENT_DT() - t0 < T#150ms DO
      spins := spins + 1;                                  (*@BUSY*)
    END_WHILE;
  END_IF;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Overrunner;
  END_RESOURCE
END_CONFIGURATION
