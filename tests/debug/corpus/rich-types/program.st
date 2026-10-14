(* corpus: rich-types
   criteria: A016 A112 A139
   fc: FC-TST-1 FC-DI-5 FC-ED-15 FC-MOD-4
   witness: Main PROG0.witness %QD0

   STRING and WSTRING with declared lengths, a multi-dimensional array, a
   subrange, a union, a REF_TO, and an enum for the invalid-enum write (A112).
*)

TYPE
  Pct : INT (0..100);
  Level : (LOW, MID, HIGH);
  Word2 : UNION
    w : WORD;
    b : ARRAY[0..1] OF BYTE;
  END_UNION;
END_TYPE

PROGRAM Rich
  VAR
    witness AT %QD0 : DINT;
    out_p AT %QW0 : INT;
    out_m AT %QW1 : INT;
    s : STRING(16) := 'hello';
    ws : WSTRING(8) := "wide";
    m2 : ARRAY[0..1, 1..3] OF INT;
    p : Pct := 50;
    lv : Level := MID;
    u : Word2;
    target : INT := 7;
    r : REF_TO INT;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  r := REF(target);                                        (*@REF*)
  r^ := r^ + 1;                                            (*@DEREF*)
  m2[1, 3] := target;                                      (*@M2*)
  u.w := 16#1234;                                          (*@UNION*)
  out_p := p;                                              (*@OUT_P*)
  out_m := m2[1, 3];                                       (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Rich;
  END_RESOURCE
END_CONFIGURATION
