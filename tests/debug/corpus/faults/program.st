(* corpus: faults
   criteria: A047 A060 A064 A073 A107 A181 A182 A190 A207 A214
   fc: FC-TST-1 FC-TST-6 FC-BP-11 FC-DAP-1 FC-DAP-3 FC-DIAG-3 FC-HLT-5
     FC-HLT-11 FC-HLT-13 FC-RC-6 FC-ST-11 FC-TR-1 FC-TR-2 FC-TR-3
   witness: Main PROG0.witness %QD0
   ASSUMED: a Python block is the editor's generated stub, an FB whose body is
     native code calling the runtime's Python loader (ADR-008 Context). PyBlock
     stands in for that stub: its faults are a SIGSEGV in the stub's own code
     (mode 1) and in a function the stub calls (mode 2).
   ASSUMED: "dangling POINTER TO" is a pointer to an address outside every
     mapping, built by XWORD arithmetic on ADR(); a pointer to a returned stack
     slot stays mapped and would not fault.
   ASSUMED: C block code names ST members in upper case (STruCpp emits MODE,
     RAN), as the generated POU bodies do.

   Plant-selectable faults, one per value of %IW0 (0 = none):
     1 division by zero          6 SIGSEGV inside a native C block
     2 array index out of bounds 7 fault inside the Python block
     3 null REF_TO dereference   8 SIGSEGV in a C helper two calls below a C block
     4 watchdog overrun          9 fault in a function the Python block calls
     5 dangling POINTER TO in ST 10 dangling POINTER TO in a second ST POU (AfterC),
                                    reached right after the C block returns (A214)
   Checked faults (1-4) locate to a line; 5-10 to the enclosing POU only
   (ADR-008 D5.2, precision "pou").
*)

FUNCTION Divide : DINT
  VAR_INPUT
    a : DINT;
    b : DINT;
  END_VAR
  Divide := a / b;                                         (*@DIV*)
END_FUNCTION

FUNCTION_BLOCK CBlock
  VAR_INPUT
    mode : INT;
  END_VAR
  VAR_OUTPUT
    ran : DINT;
  END_VAR
  ran := ran + 1;                                          (*@CBLOCK_ST*)
  {external
    if (MODE == 1) {
      volatile int *p = nullptr;
      *p = 1;
    }
    if (MODE == 2) {
      auto inner = [](volatile int *p) __attribute__((noinline)) { *p = 2; };
      auto outer = [&inner](volatile int *p) __attribute__((noinline)) { inner(p); };
      outer(nullptr);
    }
  }
END_FUNCTION_BLOCK

FUNCTION_BLOCK PyBlock
  VAR_INPUT
    mode : INT;
  END_VAR
  VAR_OUTPUT
    ran : DINT;
  END_VAR
  ran := ran + 1;                                          (*@PY_ST*)
  {external
    if (MODE == 1) {
      volatile int *p = nullptr;
      *p = 3;
    }
    if (MODE == 2) {
      auto script_call = [](volatile int *p) __attribute__((noinline)) { *p = 4; };
      script_call(nullptr);
    }
  }
END_FUNCTION_BLOCK

FUNCTION_BLOCK AfterC
  VAR_INPUT
    on : BOOL;
  END_VAR
  VAR
    v : DINT;
    p : POINTER TO DINT;
  END_VAR
  IF on THEN
    p := ADR(v) XOR 16#7FFF000000000000;                   (*@AFTER_C_PTR*)
    v := p^;                                               (*@AFTER_C_DEREF*)
  END_IF;
END_FUNCTION_BLOCK

PROGRAM Faults
  VAR
    witness AT %QD0 : DINT;
    fault_sel AT %IW0 : INT;
    out_q AT %QD1 : DINT;
    sel : INT;
    divisor : DINT;
    q : DINT;
    idx : INT;
    arr : ARRAY[0..3] OF DINT;
    r : REF_TO DINT;
    v : DINT;
    p : POINTER TO DINT;
    spins : DINT;
    t0 : DT;
    cb : CBlock;
    py : PyBlock;
    after : AfterC;
  END_VAR
  witness := witness + 1;                                  (*@WITNESS*)
  sel := fault_sel;
  divisor := 1;
  IF sel = 1 THEN
    divisor := 0;
  END_IF;
  q := Divide(100, divisor);                               (*@DIV_CALL*)
  idx := 0;
  IF sel = 2 THEN
    idx := 10;
  END_IF;
  arr[idx] := q;                                           (*@OOB*)
  IF sel = 3 THEN
    v := r^;                                               (*@NULL*)
  END_IF;
  IF sel = 4 THEN
    t0 := CURRENT_DT();
    WHILE CURRENT_DT() - t0 < T#2s DO
      spins := spins + 1;                                  (*@OVERRUN*)
    END_WHILE;
  END_IF;
  IF sel = 5 THEN
    p := ADR(v) XOR 16#7FFF000000000000;
    v := p^;                                               (*@DANGLING*)
  END_IF;
  IF sel = 6 THEN
    cb(mode := 1);                                         (*@CALL_C_INLINE*)
  ELSIF sel = 8 THEN
    cb(mode := 2);                                         (*@CALL_C_DEEP*)
  ELSE
    cb(mode := 0);                                         (*@CALL_C*)
  END_IF;
  after(on := sel = 10);                                   (*@CALL_AFTER*)
  IF sel = 7 THEN
    py(mode := 1);                                         (*@CALL_PY_INLINE*)
  ELSIF sel = 9 THEN
    py(mode := 2);                                         (*@CALL_PY_DEEP*)
  END_IF;
  out_q := q;                                              (*@LAST*)
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
    PROGRAM PROG0 WITH Main : Faults;
  END_RESOURCE
END_CONFIGURATION
