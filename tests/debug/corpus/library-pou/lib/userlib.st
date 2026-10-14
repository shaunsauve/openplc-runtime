(* User library for corpus:library-pou. build_all.sh compiles this directory
   with --compile-lib --no-source, so the program links a POU shipped without
   source (F018, ADR-002 D6: noSource with noSourceReason "library"). *)

FUNCTION_BLOCK Debounce
  VAR_INPUT
    raw : BOOL;
    n : INT;
  END_VAR
  VAR_OUTPUT
    q : BOOL;
  END_VAR
  VAR
    cnt : INT;
  END_VAR
  IF raw THEN
    cnt := cnt + 1;
  ELSE
    cnt := 0;
  END_IF;
  q := cnt >= n;
END_FUNCTION_BLOCK

FUNCTION Twice : INT
  VAR_INPUT
    v : INT;
  END_VAR
  Twice := v * 2;
END_FUNCTION
