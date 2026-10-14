(* constant-overrun variant: in place, counter steps by 2 (@COUNTER). The
   scenario applies it online to a running program.st. *)

PROGRAM Overrunner
  VAR
    witness AT %QD0 : DINT;
    counter AT %QD1 : DINT;
    overrun_on AT %IX0.0 : BOOL;
    spins : DINT;
    t0 : DT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  counter := counter + 2;                                  (*@COUNTER*)
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
