(* corpus: consumer-shape
   criteria: A011 A023 A056 A080 A091 A095 A096 A097 A101 A109 A124 A137
     A186 A188
   fc: FC-TST-1 FC-DI-1 FC-DI-3 FC-DI-17 FC-ED-10 FC-HLT-9 FC-IN-8 FC-IN-10
     FC-IN-12 FC-MOD-1 FC-MOD-5 FC-MOD-10 FC-TR-6 FC-TR-14
   witness: Main PROG_IN.witness %QD0
   ASSUMED: "a generated FB" is machine-generated in shape (GenFilter, as the
     consumer's generator emits it); flagging generated code is
     corpus:generated-region's job, so no pragma here.
   ASSUMED: the per-variable formatters A091 reads (byte array as string,
     bitfield word with named bits, coded integer with labels) are declared
     with ADR-002 D6's {attribute 'format'} once STruCpp lexes it (F077); the
     targets are G_PLANT.recipe, G_PLANT.line.status and G_PLANT.alarm_code.
   ASSUMED: the host-style I/O plugin, the Modbus slave and the OPC-UA server
     are runtime plugin configuration (plugins.json beside this file), not ST.

   Modelled on the primary consumer: one 100 ms task running three programs in
   order (input map, logic, output map); nested struct globals reached through
   VAR_EXTERNAL; 50 located scalars (16 %IX, 16 %QX, 8 %IW, 8 %QW, 2 %MW);
   byte arrays used as strings; bitfield words; coded integers. Every scan the
   logic copies the scan counter into the 192-entry mirror (A097).
*)

TYPE
  MotorCmd : STRUCT
    run : BOOL;
    speed_sp : INT;
  END_STRUCT;
  LineState : STRUCT
    m1 : MotorCmd;
    m2 : MotorCmd;
    status : WORD;
  END_STRUCT;
  PlantData : STRUCT
    line : LineState;
    recipe : ARRAY[0..15] OF BYTE;
    alarm_code : INT;
    ai_raw : ARRAY[0..7] OF INT;
    ai_scaled : ARRAY[0..7] OF INT;
    mirror : ARRAY[0..191] OF DINT;
  END_STRUCT;
END_TYPE

FUNCTION_BLOCK GenFilter
  VAR_INPUT
    raw : ARRAY[0..7] OF INT;
  END_VAR
  VAR_OUTPUT
    scaled : ARRAY[0..7] OF INT;
  END_VAR
  VAR
    k : INT;
  END_VAR
  FOR k := 0 TO 7 DO                                       (*@GEN_FOR*)
    scaled[k] := raw[k] / 2;                               (*@GEN_BODY*)
  END_FOR;
END_FUNCTION_BLOCK

PROGRAM InputMap
  VAR
    witness AT %QD0 : DINT;
    di_00 AT %IX0.0 : BOOL;
    di_01 AT %IX0.1 : BOOL;
    di_02 AT %IX0.2 : BOOL;
    di_03 AT %IX0.3 : BOOL;
    di_04 AT %IX0.4 : BOOL;
    di_05 AT %IX0.5 : BOOL;
    di_06 AT %IX0.6 : BOOL;
    di_07 AT %IX0.7 : BOOL;
    di_08 AT %IX1.0 : BOOL;
    di_09 AT %IX1.1 : BOOL;
    di_10 AT %IX1.2 : BOOL;
    di_11 AT %IX1.3 : BOOL;
    di_12 AT %IX1.4 : BOOL;
    di_13 AT %IX1.5 : BOOL;
    di_14 AT %IX1.6 : BOOL;
    di_15 AT %IX1.7 : BOOL;
    ai_0 AT %IW0 : INT;
    ai_1 AT %IW1 : INT;
    ai_2 AT %IW2 : INT;
    ai_3 AT %IW3 : INT;
    ai_4 AT %IW4 : INT;
    ai_5 AT %IW5 : INT;
    ai_6 AT %IW6 : INT;
    ai_7 AT %IW7 : INT;
  END_VAR
  VAR_EXTERNAL
    G_PLANT : PlantData;
    G_SCAN : DINT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  G_SCAN := witness;
  G_PLANT.line.m1.run := di_00;                            (*@IN_M1*)
  G_PLANT.line.m2.run := di_01;
  G_PLANT.ai_raw[0] := ai_0;
  G_PLANT.ai_raw[1] := ai_1;
  G_PLANT.ai_raw[2] := ai_2;
  G_PLANT.ai_raw[3] := ai_3;
  G_PLANT.ai_raw[4] := ai_4;
  G_PLANT.ai_raw[5] := ai_5;
  G_PLANT.ai_raw[6] := ai_6;
  G_PLANT.ai_raw[7] := ai_7;
END_PROGRAM

PROGRAM Logic
  VAR
    filt : GenFilter;
    i : INT;
    hb AT %MW0 : INT;
    mode_word AT %MW1 : WORD;
  END_VAR
  VAR_EXTERNAL
    G_PLANT : PlantData;
    G_SCAN : DINT;
  END_VAR
  filt(raw := G_PLANT.ai_raw);                             (*@LOGIC_FILT*)
  G_PLANT.ai_scaled := filt.scaled;
  G_PLANT.line.status := 0;
  IF G_PLANT.line.m1.run THEN
    G_PLANT.line.status := G_PLANT.line.status OR 16#0001;  (*@STATUS_RUN*)
  END_IF;
  IF G_PLANT.ai_raw[0] > 1000 THEN
    G_PLANT.alarm_code := 3;                               (*@ALARM*)
  ELSE
    G_PLANT.alarm_code := 0;
  END_IF;
  FOR i := 0 TO 191 DO
    G_PLANT.mirror[i] := G_SCAN;                           (*@MIRROR*)
  END_FOR;
  G_PLANT.recipe[0] := 16#50;
  G_PLANT.recipe[1] := 16#4C;
  G_PLANT.recipe[2] := 16#43;
  hb := DINT_TO_INT(G_SCAN MOD 1000);                      (*@LOGIC_LAST*)
END_PROGRAM

PROGRAM OutputMap
  VAR
    do_00 AT %QX0.0 : BOOL;
    do_01 AT %QX0.1 : BOOL;
    do_02 AT %QX0.2 : BOOL;
    do_03 AT %QX0.3 : BOOL;
    do_04 AT %QX0.4 : BOOL;
    do_05 AT %QX0.5 : BOOL;
    do_06 AT %QX0.6 : BOOL;
    do_07 AT %QX0.7 : BOOL;
    do_08 AT %QX1.0 : BOOL;
    do_09 AT %QX1.1 : BOOL;
    do_10 AT %QX1.2 : BOOL;
    do_11 AT %QX1.3 : BOOL;
    do_12 AT %QX1.4 : BOOL;
    do_13 AT %QX1.5 : BOOL;
    do_14 AT %QX1.6 : BOOL;
    do_15 AT %QX1.7 : BOOL;
    ao_0 AT %QW0 : INT;
    ao_1 AT %QW1 : INT;
    ao_2 AT %QW2 : INT;
    ao_3 AT %QW3 : INT;
    ao_4 AT %QW4 : INT;
    ao_5 AT %QW5 : INT;
    ao_6 AT %QW6 : INT;
    ao_7 AT %QW7 : INT;
  END_VAR
  VAR_EXTERNAL
    G_PLANT : PlantData;
  END_VAR
  do_00 := G_PLANT.line.status.0;
  do_01 := G_PLANT.line.status.1;
  do_02 := G_PLANT.line.status.2;
  do_03 := G_PLANT.line.status.3;
  do_04 := G_PLANT.line.status.4;
  do_05 := G_PLANT.line.status.5;
  do_06 := G_PLANT.line.status.6;
  do_07 := G_PLANT.line.status.7;
  do_08 := G_PLANT.line.status.8;
  do_09 := G_PLANT.line.status.9;
  do_10 := G_PLANT.line.status.10;
  do_11 := G_PLANT.line.status.11;
  do_12 := G_PLANT.line.status.12;
  do_13 := G_PLANT.line.status.13;
  do_14 := G_PLANT.line.status.14;
  do_15 := G_PLANT.line.status.15;
  ao_0 := G_PLANT.ai_scaled[0];
  ao_1 := G_PLANT.ai_scaled[1];
  ao_2 := G_PLANT.ai_scaled[2];
  ao_3 := G_PLANT.ai_scaled[3];
  ao_4 := G_PLANT.ai_scaled[4];
  ao_5 := G_PLANT.ai_scaled[5];
  ao_6 := G_PLANT.ai_scaled[6];
  ao_7 := G_PLANT.ai_scaled[7];
END_PROGRAM

CONFIGURATION Config0
  VAR_GLOBAL
    G_PLANT : PlantData;
    G_SCAN : DINT;
  END_VAR
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG_IN WITH Main : InputMap;
    PROGRAM PROG_LOGIC WITH Main : Logic;
    PROGRAM PROG_OUT WITH Main : OutputMap;
  END_RESOURCE
END_CONFIGURATION
