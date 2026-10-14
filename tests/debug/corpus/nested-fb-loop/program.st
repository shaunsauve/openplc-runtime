(* corpus: nested-fb-loop
   criteria: A011 A012 A017 A018 A022 A025 A029 A035 A038 A042 A046 A064 A067
     A068 A070 A071 A072 A073 A074 A078 A079 A083 A085 A086 A088 A093 A105
     A106 A135 A136 A142 A157 A158 A165 A166 A167 A168 A187 A190 A192 A196
     A199 A200
   fc: FC-TST-1 FC-TST-3 FC-TST-8 FC-TST-9 FC-BP-1 FC-BP-2 FC-BP-3 FC-BP-4
     FC-BP-5 FC-BP-6 FC-BP-10 FC-BP-18 FC-DAP-1 FC-DAP-3 FC-DAP-4 FC-DAP-6
     FC-DAP-8 FC-DAP-11 FC-DAP-12 FC-DI-1 FC-DI-3 FC-DI-8 FC-DI-9 FC-DI-12
     FC-DI-13 FC-DI-17 FC-DIAG-2 FC-DOC-5 FC-DOC-6 FC-DOC-7 FC-ED-1 FC-ED-2
     FC-ED-3 FC-ED-4 FC-ED-5 FC-ED-6 FC-ED-7 FC-ED-19 FC-IN-1 FC-IN-2 FC-IN-6
     FC-RC-8 FC-RC-12 FC-SEC-7 FC-ST-1 FC-ST-2 FC-ST-3 FC-ST-5 FC-ST-6 FC-ST-7
     FC-ST-8 FC-ST-11 FC-TR-1 FC-TR-2 FC-TR-3 FC-TR-4 FC-TR-7
   witness: Main PROG0.witness %QD0

   The J1 program: PROG0 -> FB_Motor instances motor1 and motor2 -> each with a
   nested PID FB -> the function Clamp; a FOR loop; comment and blank lines.
   Scan 1 from a cold start is fully determined: motor1.pid.err = 50.0,
   motor1.pid.out = 30.0, motor1.speed = 3.0 after the call; motor2 runs only
   while the plant sets %IX0.0.
*)

FUNCTION Clamp : REAL
  VAR_INPUT
    v : REAL;
    lo : REAL;
    hi : REAL;
  END_VAR
  VAR_TEMP
    r : REAL;
  END_VAR
  r := v;                                                  (*@CLAMP_FIRST*)
  IF r < lo THEN
    r := lo;
  ELSIF r > hi THEN
    r := hi;
  END_IF;
  Clamp := r;                                              (*@CLAMP_RET*)
END_FUNCTION

FUNCTION_BLOCK PID
  VAR_INPUT
    sp : REAL;
    pv : REAL;
  END_VAR
  VAR_OUTPUT
    out : REAL;
  END_VAR
  VAR
    kp : REAL := 0.5;
    ki : REAL := 0.1;
    integ : REAL;
    err : REAL;
    last : REAL;
  END_VAR
  err := sp - pv;                                          (*@PID_FIRST*)
  integ := integ + err * ki;
  out := Clamp(kp * err + integ, 0.0, 100.0);              (*@PID_CALL*)
  last := out;                                             (*@PID_AFTER*)
END_FUNCTION_BLOCK

FUNCTION_BLOCK FB_Motor
  VAR_INPUT
    enable : BOOL;
    target : REAL;
  END_VAR
  VAR_OUTPUT
    speed : REAL;
  END_VAR
  VAR
    pid : PID;
    calls : DINT;
  END_VAR
  calls := calls + 1;                                      (*@MOTOR_FIRST*)

  (* the controller only runs while enabled *) (*@CMT_MOTOR*)
  IF enable THEN                                           (*@MOTOR_IF*)
    pid(sp := target, pv := speed);                        (*@MOTOR_PID*)
    speed := speed + pid.out * 0.1;                        (*@MOTOR_AFTER*)
  ELSE
    speed := 0.0;
  END_IF;
END_FUNCTION_BLOCK

PROGRAM Cell
  VAR
    witness AT %QD0 : DINT;
    total AT %QD1 : DINT;
    speed1 AT %QD2 : REAL;
    run2 AT %IX0.0 : BOOL;
    motor1 : FB_Motor;
    motor2 : FB_Motor;
    i : INT;
  END_VAR

  witness := witness + 1;                                  (*@WITNESS*)
  // two motors share one FB type (*@CMT_MAIN*)
  motor1(enable := TRUE, target := 50.0);                  (*@CALL_M1*)
  motor2(enable := run2, target := 20.0);                  (*@CALL_M2*)

  total := 0;                                              (*@TOTAL*)
  FOR i := 1 TO 3 DO                                       (*@FOR*)
    total := total + i;                                    (*@FOR_BODY*)
  END_FOR;
  speed1 := motor1.speed;                                  (*@AFTER_LOOP*)
END_PROGRAM (*@END_MAIN*)

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Cell;
  END_RESOURCE
END_CONFIGURATION
