# Lens: correctness

Find defects that make the changed code behave incorrectly for a concrete input or state.
Report a defect only when you can trace it from the change to a wrong result.

## What to look for

- Logic errors: inverted or incomplete conditions, off-by-one bounds, wrong operator, wrong
  variable, swapped arguments, unreachable or shadowed branches.
- Absent values: a value that can be null, undefined, empty or missing on a path the change
  introduces or exposes, and is then dereferenced or assumed present.
- Error handling: exceptions or rejected promises swallowed so that failure looks like
  success, wrong exception type, cleanup skipped on an early return or on a failure path.
- Resources: connections, streams, locks or transactions opened and not released on every path.
- State and ordering: mutation of an argument or shared value, check-then-act on state that can
  change in between, operations reordered so a dependency is used before it is ready.
- Contract changes: a changed return value, default, error behaviour or signature whose callers,
  found by search, still assume the old behaviour.
- Data handling: integer overflow or truncation, rounding, encoding, time zones, collection
  semantics (ordering, duplicates, mutation while iterating).
- Asynchrony: a missing await, a fire-and-forget call whose failure matters, a callback invoked
  twice or never.

## Before you report

1. Read the enclosing code shown in the packet and locate the input that triggers the defect.
2. Search for the guard that would make it impossible: a validation in the caller, a constructor
   invariant, a framework guarantee, a test that pins the behaviour, an existing abstraction.
3. If a guard exists, the finding is void. If you cannot tell after two expansions, drop it.
4. State the trigger as `input or condition -> wrong behaviour`. Without one, do not report.

## Severity

- `important`: wrong results, data loss, crashes or hangs on a realistic input.
- `nit`: a real but minor defect with limited effect, or a fragile construct that fails only
  under an unlikely input.
- `pre_existing`: the defect is in code the change did not touch but the change makes it
  reachable or worse.

## Do not report

- Style, naming, formatting, comment wording or a preference for another design.
- Missing tests, missing documentation, performance ideas without a concrete hot path.
- Anything the compiler, type checker, linter or an existing CI check already enforces.
- Hypothetical failures that need an input the code cannot receive.
