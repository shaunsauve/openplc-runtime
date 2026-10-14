(* corpus: plant-io
   criteria: A014 A019 A031 A033 A034 A036 A037 A041 A043 A045 A047 A049 A055
     A057 A058 A059 A061 A066 A072 A075 A076 A077 A081 A082 A083 A087 A110
     A111 A113 A114 A115 A116 A117 A118 A119 A120 A121 A122 A123 A125 A126
     A127 A128 A136 A138 A139 A140 A147 A152 A156 A159 A161 A169 A170 A171
     A172 A173 A174 A177 A178 A185 A189 A192 A193 A194 A198 A201 A202 A203
     A206 A207 A208 A210 A219
   fc: FC-TST-1 FC-TST-3 FC-TST-4 FC-TST-7 FC-TST-10 FC-BP-1 FC-BP-2 FC-BP-18
     FC-DAP-2 FC-DAP-5 FC-DAP-7 FC-DI-7 FC-DI-10 FC-DIAG-1 FC-DIAG-4 FC-DOC-3
     FC-ED-2 FC-ED-5 FC-ED-8 FC-ED-9 FC-ED-13 FC-ED-15 FC-ED-16 FC-ED-19 FC-ED-20
     FC-HLT-1 FC-HLT-2 FC-HLT-5 FC-HLT-7 FC-HLT-8 FC-HLT-9 FC-HLT-11 FC-HLT-13
     FC-HLT-14 FC-HLT-15 FC-HLT-17 FC-IN-8 FC-IN-10 FC-MOD-1 FC-MOD-2 FC-MOD-3
     FC-MOD-5 FC-MOD-6 FC-MOD-9 FC-RC-1 FC-RC-2 FC-RC-3 FC-RC-4 FC-RC-5 FC-RC-6
     FC-RC-7 FC-RC-9 FC-RC-10 FC-RC-11 FC-RC-12 FC-SEC-1 FC-SEC-2 FC-SEC-3
     FC-SEC-4 FC-SEC-5 FC-ST-4 FC-ST-5 FC-TR-1 FC-TR-3 FC-TR-5 FC-TR-7 FC-TR-8
     FC-TR-9 FC-TR-11 FC-TR-13 FC-TR-15
   witness: Main PROG0.witness %QD0
   ASSUMED: "plugin-exchanged" output = a located output in the plant plugin's
     exchange range (%QW100); "located" = the other %Q outputs. REAL is located
     at %QD3 (32-bit slot), since the image has no REAL table.
   ASSUMED: the 300 ms dwell is a busy loop on CURRENT_DT() (wall time); TIME()
     is frozen within a scan (ADR-004 D4), so it cannot time a dwell.

   The base program of the corpus. One task; every input is plant-driven, every
   output is settable by the plant to a non-zero, non-initial value, and every
   statement has a visible side effect. Marked lines (comment "@NAME" at the end
   of the line) are what oracles name: K is early in the scan, L is after the
   plant-selected dwell, COPY_A/COPY_B bracket the dwell for A111.
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
  out_int := in_value;                                     (*@INT*)
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
