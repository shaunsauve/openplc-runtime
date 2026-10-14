(* corpus: generated-region
   criteria: A026 A040
   fc: FC-TST-1 FC-ST-8
   witness: Main PROG0.witness %QD0
   build: expect-fail F024: STruCpp does not lex the {strucpp generated} pragmas yet (ADR-002 D6)

   A pragma-marked generated POU (GenScale) and a marked region inside a user
   POU. Every statement inside either carries the generated flag (probes.flags
   bit 0); nothing outside does. Markers GEN_* are inside, USER_* outside.
*)

{strucpp generated}
FUNCTION_BLOCK GenScale
  VAR_INPUT
    x : INT;
  END_VAR
  VAR_OUTPUT
    y : INT;
  END_VAR
  y := x * 10;                                             (*@GEN_FB_1*)
  y := y + 1;                                              (*@GEN_FB_2*)
END_FUNCTION_BLOCK

PROGRAM GenUser
  VAR
    witness AT %QD0 : DINT;
    in_x AT %IW0 : INT;
    out_y AT %QW0 : INT;
    gs : GenScale;
    a : INT;
    b : INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  a := in_x;                                               (*@USER_1*)
  {strucpp generated_begin}
  b := a + 100;                                            (*@GEN_REGION_1*)
  b := b * 2;                                              (*@GEN_REGION_2*)
  {strucpp generated_end}
  gs(x := b);                                              (*@USER_CALL*)
  out_y := gs.y;                                           (*@USER_2*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : GenUser;
  END_RESOURCE
END_CONFIGURATION
