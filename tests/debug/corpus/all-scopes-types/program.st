(* corpus: all-scopes-types
   criteria: A013 A046 A088 A089
   fc: FC-TST-1 FC-DI-4 FC-IN-1 FC-IN-2 FC-IN-3 FC-RC-12
   witness: Main PROG0.witness %QD0

   Every variable scope (VAR, VAR_INPUT, VAR_OUTPUT, VAR_IN_OUT, VAR_TEMP, FB
   members, VAR_GLOBAL, VAR_EXTERNAL) and the composite types: nested structs,
   arrays with non-zero bounds, an enum, an array of structs. With the plant at
   %IW0 = 3 the first scan from a cold start is fully determined.
*)

TYPE
  Mode : (IDLE, RUN, FAULT);
  Point : STRUCT
    x : INT;
    y : INT;
  END_STRUCT;
  Seg : STRUCT
    a : Point;
    b : Point;
    tag : Mode;
  END_STRUCT;
END_TYPE

FUNCTION Scale : INT
  VAR_INPUT
    v : INT;
  END_VAR
  VAR_IN_OUT
    acc : INT;
  END_VAR
  VAR_TEMP
    t : INT;
  END_VAR
  t := v * 2;                                              (*@SCALE_FIRST*)
  acc := acc + t;
  Scale := t;
END_FUNCTION

FUNCTION_BLOCK Holder
  VAR_INPUT
    in1 : INT;
  END_VAR
  VAR_OUTPUT
    out1 : INT;
  END_VAR
  VAR_IN_OUT
    io : INT;
  END_VAR
  VAR_EXTERNAL
    G_COUNT : DINT;
    G_SEG : Seg;
  END_VAR
  VAR
    state : Mode := IDLE;
  END_VAR
  VAR_TEMP
    tmp : INT;
  END_VAR
  tmp := in1 + 1;                                          (*@HOLDER_FIRST*)
  io := io + tmp;
  out1 := tmp;
  G_COUNT := G_COUNT + 1;
  G_SEG.a.x := in1;
  state := RUN;
END_FUNCTION_BLOCK

PROGRAM Scopes
  VAR
    witness AT %QD0 : DINT;
    in_v AT %IW0 : INT;
    out_v AT %QW0 : INT;
    h : Holder;
    shared : INT;
    arr : ARRAY[1..4] OF INT;
    segs : ARRAY[0..1] OF Seg;
    grid : ARRAY[0..2] OF Point;
    m : Mode;
    i : INT;
  END_VAR
  VAR_EXTERNAL
    G_COUNT : DINT;
    G_SEG : Seg;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  h(in1 := in_v, io := shared);                            (*@CALL_H*)
  out_v := Scale(h.out1, shared);                          (*@CALL_SCALE*)
  FOR i := 1 TO 4 DO
    arr[i] := i * in_v;
  END_FOR;
  segs[1].b.y := in_v;
  grid[2].x := in_v + DINT_TO_INT(G_COUNT);                (*@GRID*)
  m := h.state;                                            (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  VAR_GLOBAL
    G_COUNT : DINT;
    G_SEG : Seg;
  END_VAR
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Scopes;
  END_RESOURCE
END_CONFIGURATION
