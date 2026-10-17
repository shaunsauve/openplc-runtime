"""Which event of that name an oracle step means, when the step does not say.

`oracle.schema.json` § `$defs.eventStep.properties.first` is the contract; this is the one
executable copy of its default, read by the matcher (`harness/runner.py`) and by the gate
(`corpus/validate.py`). Two statements of a default is how a field ends up documented, validated
and never once read: that is exactly what `first` was — the runner had its own idea of what an
event step meant, the gate had another, and neither was the oracle's.

The default is per event NAME, because the streams differ:

  * `stopped` — one halt, one event. A038's whole criterion is that a step landing on an armed
    breakpoint yields EXACTLY ONE stop, which is a claim about order and cardinality and not about
    a matching body. So the step means the FIRST stop the stream still holds: a runtime that stops at
    the wrong line three times and then at the right one fails instead of passing. A038(a)(b) are
    written against that reading and cannot be written against any other.

  * every other name — the stream pushes on every change (`forces` on every force and `runState` on
    every state change, ADR-005 D5.3), so several of a name are queued before any step reads them.
    Taking the oldest made A116 read the force that was ADDED where it meant the one that EXPIRED
    (plant-io/force-ttl). Those steps mean "an event of this name that matches", with earlier
    non-matching events of the same name walked past to the deadline, and the check is not weakened:
    a stream with no matching event still fails, and with none at all the earliest comes back so the
    failure says what actually arrived.

`first` says either, per step. `takes_first` is where an omitted key is resolved, and it is the only
place it is resolved. What is left after that is the register of `stopped` steps that opt out
(`validate.py firstness_warnings`), where a verdict can still be handed on a later stop.

See also:
  tests/debug/corpus/oracle.schema.json - the format this file is the executable default of.
  tests/debug/corpus/README.md § Which stop a step means - the same thing, in prose.
  tests/debug/harness/runner.py - reads it through `takes_first`, in `_step_event`.
  tests/debug/corpus/validate.py - reads it through `takes_first`, in `steps_over_a_stop`.
"""

from __future__ import annotations

from typing import Mapping, Optional

# The one event name whose order and cardinality are the semantics. A step that lands on an armed
# breakpoint reads the first stop the stream still holds; every other name reads a matching one.
FIRST_STOP_EVENT = "stopped"

__all__ = ["FIRST_STOP_EVENT", "takes_first"]


def takes_first(step: Mapping[str, object]) -> bool:
    """Does this event step mean the FIRST event of its name the stream still holds?

    A step that says nothing follows the default for its event name; a step that says `first` says
    which. A `first` that is not a boolean is a typo the schema refuses (`oracle.schema.json` says
    boolean and `test_validate` proves it), and it is answered here as though it had not been said:
    a malformed oracle must not be able to hand the matcher a mode nobody chose.
    """
    said: Optional[object] = step.get("first")
    if isinstance(said, bool):
        return said
    return step.get("event") == FIRST_STOP_EVENT