(* retain-200 variant: body edit only (spare counts scans). Uploaded as a warm
   full download by retain-200/warm-download-and-restart (A215). *)

PROGRAM Retain200
  VAR RETAIN
    r_dint : ARRAY[0..119] OF DINT;
    r_int : ARRAY[0..39] OF INT;
    r_real : ARRAY[0..19] OF REAL;
    r_bool : ARRAY[0..14] OF BOOL;
    r_time : TIME;
    r_lreal : LREAL;
    r_word : WORD;
    r_mw AT %MW10 : INT;
    r_ws : WSTRING;
  END_VAR
  VAR
    witness AT %QD0 : DINT;
    last_dint AT %QD1 : DINT;
    kept_n AT %QD2 : DINT;
    first_dint AT %QD3 : DINT;
    first_kept AT %QD4 : DINT;
    mw_out AT %QW0 : INT;
    first_mw AT %QW1 : INT;
    spare AT %QW2 : INT;
    load AT %IX0.0 : BOOL;
    seed AT %ID0 : DINT;
    k : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  IF witness = 1 THEN
    first_dint := r_dint[119];                             (*@FIRST*)
    first_mw := r_mw;
    first_kept := kept_n;
  END_IF;
  kept_n := 0;                                             (*@COUNT*)
  FOR k := 0 TO 119 DO
    IF r_dint[k] <> 0 THEN kept_n := kept_n + 1; END_IF;
  END_FOR;
  FOR k := 0 TO 39 DO
    IF r_int[k] <> 0 THEN kept_n := kept_n + 1; END_IF;
  END_FOR;
  FOR k := 0 TO 19 DO
    IF r_real[k] <> 0.0 THEN kept_n := kept_n + 1; END_IF;
  END_FOR;
  FOR k := 0 TO 14 DO
    IF r_bool[k] THEN kept_n := kept_n + 1; END_IF;
  END_FOR;
  IF r_time <> T#0s THEN kept_n := kept_n + 1; END_IF;
  IF r_lreal <> 0.0 THEN kept_n := kept_n + 1; END_IF;
  IF r_word <> 0 THEN kept_n := kept_n + 1; END_IF;
  IF r_mw <> 0 THEN kept_n := kept_n + 1; END_IF;
  IF r_ws <> "" THEN kept_n := kept_n + 1; END_IF;
  IF load THEN                                             (*@LOAD*)
    FOR k := 0 TO 119 DO
      r_dint[k] := seed + k;
    END_FOR;
    FOR k := 0 TO 39 DO
      r_int[k] := DINT_TO_INT(seed MOD 1000) + k + 1;
    END_FOR;
    FOR k := 0 TO 19 DO
      r_real[k] := DINT_TO_REAL(seed) + 0.5 * INT_TO_REAL(k);
    END_FOR;
    FOR k := 0 TO 14 DO
      r_bool[k] := TRUE;
    END_FOR;
    r_time := T#42s;
    r_lreal := 3.25;
    r_word := 16#BEEF;
    r_mw := 4242;
    r_ws := "retained";
  END_IF;
  last_dint := r_dint[119];
  mw_out := r_mw;
  spare := spare + 1;                                      (*@SPARE*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Retain200;
  END_RESOURCE
END_CONFIGURATION
