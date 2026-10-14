(* S8 of ADR-001 D1.3: both tasks call the shared FB instance G_FB.
   STruCpp must refuse this at compile time; build_all.sh fails if it compiles. *)

FUNCTION F : INT
  VAR_INPUT
    i : INT;
  END_VAR
  F := i MOD 4;
END_FUNCTION

FUNCTION_BLOCK Gauge
  VAR
    level : DINT;
  END_VAR
  PROPERTY PUBLIC P : DINT
    GET
      P := level;
    END_GET
  END_PROPERTY
END_FUNCTION_BLOCK

FUNCTION_BLOCK Pulse
  VAR_INPUT
    IN : INT;
  END_VAR
  VAR_OUTPUT
    Q : DINT;
  END_VAR
  Q := INT_TO_DINT(IN) + 1;
END_FUNCTION_BLOCK

PROGRAM ProgA
  VAR
    witness AT %QD0 : DINT;
    out_x AT %QD2 : DINT;
    i : INT;
    x : DINT;
    y : DINT;
    z : DINT;
    units : ARRAY[0..3] OF Pulse;
  END_VAR
  VAR_EXTERNAL
    G_ARR : ARRAY[0..7] OF DINT;
    G_A : ARRAY[0..3] OF DINT;
    G_B : ARRAY[0..3] OF INT;
    G_W : ARRAY[0..3] OF WORD;
    G_S : DINT;
    G_FB : Gauge;
  END_VAR
  witness := witness + 1;
  y := G_S;
  G_S := INT_TO_DINT(F(i));
  units[0](IN := F(i), Q => G_S);
  x := G_FB.P;
  x := x + G_ARR[F(i)];
  G_ARR[F(i)] := x;
  G_W[F(i)].3 := TRUE;
  units[F(i)](IN := 1, Q => G_ARR[1]);
  z := G_A[G_B[1]];
  out_x := x + y + z;
  G_FB();
  i := i + 1;
END_PROGRAM

PROGRAM ProgB
  VAR
    witness AT %QD1 : DINT;
    k : INT;
  END_VAR
  VAR_EXTERNAL
    G_ARR : ARRAY[0..7] OF DINT;
    G_FB : Gauge;
  END_VAR
  witness := witness + 1;
  G_ARR[k] := G_ARR[k] + 1;
  k := (k + 1) MOD 8;
  G_FB();
END_PROGRAM

CONFIGURATION Config0
  VAR_GLOBAL
    G_ARR : ARRAY[0..7] OF DINT;
    G_A : ARRAY[0..3] OF DINT := [10, 11, 12, 13];
    G_B : ARRAY[0..3] OF INT := [0, 1, 2, 3];
    G_W : ARRAY[0..3] OF WORD;
    G_S : DINT;
    G_FB : Gauge;
  END_VAR
  RESOURCE Res0 ON PLC
    TASK TaskA(INTERVAL := T#20ms, PRIORITY := 1);
    TASK TaskB(INTERVAL := T#10ms, PRIORITY := 2);
    PROGRAM PROG_A WITH TaskA : ProgA;
    PROGRAM PROG_B WITH TaskB : ProgB;
  END_RESOURCE
END_CONFIGURATION
