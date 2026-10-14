(* corpus: plant-io (variant-stale)
   Build B of plant-io/stale-build and of the harness's symbolic-args fixture: program.st with the
   @INT statement changed, so its md5 differs from build A's (ADR-002 D5, STALE_BUILD reason
   source). Never loaded; only its identity is presented.
*)

PROGRAM PlantIO
  VAR
    witness AT %QD0 : DINT;     (* scan witness: +1 as the first statement *)
    counter AT %QD1 : DINT;
    out_dint AT %QD2 : DINT;
    out_real AT %QD3 : REAL;
    out_bool AT %QX0.0 : BOOL;
    out_int AT %QW0 : INT;
    out_a AT %QW1 : INT;
    out_b AT %QW2 : INT;
    x_seen AT %QW3 : INT;
    out_plug AT %QW100 : INT;
    in_enable AT %IX0.0 : BOOL;
    load_x AT %IX0.1 : BOOL;
    dwell_on AT %IX0.2 : BOOL;  (* plant-selected 300 ms dwell before L *)
    in_value AT %IW0 : INT;
    in_dint AT %ID0 : DINT;
    x : INT;
    branch : INT;
    spins : DINT;
    t0 : DT;
  END_VAR

  witness := witness + 1;                                  (*@WITNESS*)
  out_a := x;                                              (*@COPY_A*)
  counter := counter + 1;                                  (*@K*)
  IF in_enable THEN                                        (*@IF*)
    out_bool := TRUE;                                      (*@THEN*)
    branch := 1;
  ELSE
    out_bool := FALSE;                                     (*@ELSE*)
    branch := 2;
  END_IF;
  out_int := in_value + 1;                                 (*@INT*)
  out_dint := in_dint;
  out_real := INT_TO_REAL(in_value) / 2.0;
  out_plug := in_value + 1;
  IF load_x THEN
    x := in_value;                                         (*@LOAD_X*)
  END_IF;
  IF dwell_on THEN                                         (*@DWELL*)
    t0 := CURRENT_DT();
    WHILE CURRENT_DT() - t0 < T#300ms DO
      spins := spins + 1;
    END_WHILE;
  END_IF;
  x_seen := x * 2;                                         (*@L*)
  out_b := x;                                              (*@COPY_B*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : PlantIO;
  END_RESOURCE
END_CONFIGURATION
