# Time Qualifier Support

## Problem

Natural language queries with time qualifiers — "after 1pm", "in the morning", "around 2pm" — were either treated as ambiguous or had the qualifier silently dropped.

Root cause: two-part information loss in the parsing pipeline.

1. **`dateparser` collapses qualifiers to a single point**: "after 1pm" → exactly `1:00 PM`
2. **`find_slots()` had no time-window concept**: it walked from `wh_start` with no constraint from the qualifier

For `suggest_times` ("Do you have time Friday afternoon?"), only the date was extracted for `restrict_to_date`; the time-of-day qualifier was ignored and all of Friday's working hours were searched.

For `meeting_request` busy fallback, alternative slots were found across all working hours ignoring the original "after 1pm" intent.

## Solution

Add a `time_window` qualifier field to `proposed_times` entries in the parser output, preserve it through normalization, and use it to constrain `find_slots()`.

## Architecture

### Parser output schema change

`proposed_times` entries now carry a `time_window` field:

```json
{
  "text": "after 1pm on Friday",
  "normalized": ["Friday at 1pm"],
  "time_window": "after"
}
```

Valid values: `"after"` | `"before"` | `"around"` | `"morning"` | `"afternoon"` | `"evening"` | `null`

- `"after"` — any slot starting at or after the anchor time
- `"before"` — any slot ending at or before the anchor time
- `"around"` — approximately at the anchor time (±1 hour window)
- `"morning"` / `"afternoon"` / `"evening"` — fuzzy period with no anchor time
- `null` — exact time (existing behavior)

The `normalized` phrase is still converted to a UTC datetime by `dateparser` as before. The resulting datetime serves as the **anchor** for directional qualifiers (`after`/`before`/`around`). The `time_window` field is preserved on the entry unchanged (only `normalized` is popped during normalization).

### `time_window_bounds()` helper — `ea/scheduler.py`

Maps qualifier + anchor datetime → `(time_after, time_before)`:

| Qualifier   | time_after         | time_before         |
|------------|-------------------|---------------------|
| `"after"`   | anchor.time()      | None                |
| `"before"`  | None               | anchor.time()       |
| `"around"`  | anchor − 1 hour    | anchor + 1 hour     |
| `"morning"` | 08:00              | 12:00               |
| `"afternoon"`| 12:00             | 17:00               |
| `"evening"` | 17:00              | 20:00               |
| `null`      | None               | None                |

### `find_slots()` changes — `ea/scheduler.py`

New parameters: `time_after: time | None = None`, `time_before: time | None = None`

- Cursor initializes at `max(wh_start, time_after)` rather than always `wh_start`
- Inner loop breaks when `slot_start.time() >= time_before`

### Responder changes — `ea/responder.py`

Both `handle_suggest_times_trigger()` and the busy fallback in `handle_inbound_result()` now extract the `time_window` qualifier and anchor from `proposed_times[0]`, call `time_window_bounds()`, and pass the result to `find_slots()`.

## What is NOT changed

- `evaluate_parsed()` — for `meeting_request` with an exact time, the anchor is still checked first (correct behavior). The improvement is in when `find_slots` is invoked for alternatives or `suggest_times`.
- `validate_parsed()` — `time_window` is an optional string field; absent/null is fine.
- `DateNormalizer` — `dateparser` still converts the anchor phrase to a point-in-time (correct; it's the anchor, not the window).

## Examples

| Input | time_window | anchor | find_slots window |
|-------|-------------|--------|-------------------|
| "after 1pm on Friday" | `"after"` | Friday 1:00 PM | 1pm → end of working hours |
| "in the morning tomorrow" | `"morning"` | tomorrow (no time anchor) | 8am → 12pm |
| "around 2pm Tuesday" | `"around"` | Tuesday 2:00 PM | 1pm → 3pm |
| "before noon" | `"before"` | noon | start of working hours → 12pm |
| "Thursday at 3pm" | `null` | Thursday 3:00 PM | exact slot only (no window) |
