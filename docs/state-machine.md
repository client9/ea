# EA Scheduling State Machine

## Overview

Two directions of scheduling exist:

- **Inbound** — someone emails you asking for a meeting; you reply `EA: please schedule`.
- **Outbound** — you want to propose a meeting; you send an email to them with `EA: suggest some times`.

In both cases the EA detects an `EA:` trigger, but the subsequent flow and the party being waited on differ. Completed threads are marked with Gmail labels and removed from the state store. `state.json` only holds threads actively awaiting a reply.

---

## Intents

| Intent | Trigger example | Direction |
|---|---|---|
| `meeting_request` | `EA: please schedule` (reply to inbound email) | Inbound |
| `suggest_times` | `EA: suggest some times to meet` (on an outgoing email) | Outbound |
| `block_time` | `EA: block Thursday 12–1pm for lunch` (email to self) | Self |

---

## Inbound Flow

Someone emails you asking for a meeting. You reply `EA: please schedule`.

```mermaid
flowchart TD
    A([Inbound thread\nEA: please schedule]) --> B[Parse thread\n+ extract datetimes]
    B --> C{evaluate_parsed}

    C -->|ambiguities / no times\nor no duration| D[outcome: ambiguous]
    C -->|all proposed slots busy| E[outcome: busy]
    C -->|free slot in working\nor preferred hours| F[outcome: open]
    C -->|free slot exists\nbut after-hours only| G[outcome: needs_confirmation]

    D --> D1[Email you privately:\nwhat's missing]
    D1 --> D2([Label: ea-notified — Done])

    E --> E1[Email you privately:\nwho's busy and when]
    E1 --> E2([Label: ea-notified — Done])

    F --> F1[create_event\n+ send invite to all attendees]
    F1 --> F2([Label: ea-scheduled — Done])

    G --> G1[Email you privately on\na new private thread\nX-EA-Original-Thread header set]
    G1 --> G2[Write state.json:\ntype: pending_confirmation\noriginal + confirmation thread IDs\nScheduleResult + expiry]
    G2 --> G3([State: pending_confirmation])

    G3 -->|Expiry passes, no reply| EX1[Email you: window lapsed]
    EX1 --> EX2([Remove from state.json\nLabel: ea-expired — Done])

    G3 -->|You reply to private thread| H{Parse your reply}

    H -->|Rejected\n'no' / 'cancel'| H1[Email you: noted,\nlet me know if you want to retry]
    H1 --> H2([Remove from state.json\nLabel: ea-cancelled — Done])

    H -->|Confirmed\n'yes' / 'go ahead'| H3[create_event\n+ send invite to all attendees]
    H3 --> H4([Remove from state.json\nLabel: ea-scheduled — Done])

    H -->|Modified\n'yes but 30 min'\n'try Friday instead'| H5[Re-run evaluate_parsed\nwith updated constraint]

    H5 -->|open| H3
    H5 -->|busy| H6[Reply on same private thread:\nstill busy]
    H6 --> G3
    H5 -->|ambiguous| H7[Reply on same private thread:\nstill unclear]
    H7 --> G3
    H5 -->|needs_confirmation| H8[Reply on same private thread\nwith new proposed slot]
    H8 --> G9[Update state.json in place:\nnew ScheduleResult, reset expiry]
    G9 --> G3
```

---

## Outbound Flow

You are initiating. You compose an email to someone and include `EA: suggest some times to meet`. The EA finds your best available slots and sends them on your behalf. You are now waiting on *their* reply.

```mermaid
flowchart TD
    A([Outbound thread\nEA: suggest some times]) --> B[Find 3 best slots\nin your calendar\npreferred → working → after-hours]
    B --> C[Send email to them\nwith suggested slots\non the existing thread\nX-EA-Original-Thread header set]
    C --> D[Write state.json:\ntype: pending_external_reply\nsuggested_slots\nthread ID + expiry]
    D --> E([State: pending_external_reply])

    E -->|Expiry passes, no reply| EX1[Email you: they haven't replied]
    EX1 --> EX2([Remove from state.json\nLabel: ea-expired — Done])

    E -->|They reply to thread| F{Parse their reply\nwith Claude}

    F -->|Confirmed a specific slot\n'Yes, 11:30 works'| F1[Verify slot still free\nin your calendar]

    F1 -->|Still free| F2[create_event\n+ send invite to all attendees]
    F2 --> F3([Remove from state.json\nLabel: ea-scheduled — Done])

    F1 -->|No longer free| F4[Reply to thread:\nthat slot was just taken,\nhere are new options]
    F4 --> D

    F -->|Counter-proposal with constraints\n'That doesn't work,\ndo you have Friday?'| G[Re-run slot finding\nwith their constraint\nagainst your calendar]
    G --> G1[Reply to thread\nwith new suggested slots]
    G1 --> G2[Update state.json in place:\nnew suggested_slots, reset expiry]
    G2 --> E

    F -->|They state their own availability\n'I'm free Tuesday or Thursday'| H[Cross-reference their stated\navailability against your calendar]
    H -->|Overlap found| H1[Reply to thread:\nconfirming the overlapping slot\nasking them to confirm]
    H1 --> H2[Update state.json:\ntype → pending_external_reply\nwith the single proposed slot]
    H2 --> E
    H -->|No overlap| H3[Reply to thread:\nno overlap found,\npropose new slots or ask\nfor more options]
    H3 --> G2
```

---

## Poll Loop

Each poll cycle runs three passes in order:

### Pass 1 — New `EA:` triggers
Scan threads not yet labeled `ea-*` for an `EA:` reply from your own email address. For each found, detect the intent and run the appropriate pipeline (inbound or outbound).

### Pass 2 — Pending confirmations (inbound)
For each `pending_confirmation` entry in `state.json`, check whether the private confirmation thread has a new reply from you since the last poll. If so, run the confirmation handler.

### Pass 3 — Pending external replies (outbound)
For each `pending_external_reply` entry in `state.json`, check whether the original thread has a new reply from the other party since the last poll. If so, run the external reply handler.

---

## State Store Schema

File: `state.json` (project root)

```json
{
  "<original_gmail_thread_id>": {
    "type": "pending_confirmation | pending_external_reply",
    "confirmation_thread_id": "<gmail_thread_id_of_private_ea_email>",
    "created_at": "<ISO 8601>",
    "expires_at": "<ISO 8601>",

    "schedule_result": {
      "outcome": "needs_confirmation",
      "slot_start": "<ISO 8601>",
      "slot_end": "<ISO 8601>",
      "slot_type": "after_hours",
      "topic": "...",
      "attendees": ["..."],
      "duration_minutes": 30,
      "parsed": {}
    },

    "suggested_slots": [
      { "start": "<ISO 8601>", "end": "<ISO 8601>", "slot_type": "preferred" }
    ]
  }
}
```

`schedule_result` is populated for `pending_confirmation`.
`suggested_slots` is populated for `pending_external_reply`.
Both may be present if a confirmation round followed an outbound suggestion.

---

## Gmail Labels

| Label | Meaning |
|---|---|
| `ea-scheduled` | Event created and invite sent |
| `ea-notified` | You were informed of ambiguity or conflict; no further action needed |
| `ea-cancelled` | You explicitly rejected the proposed slot |
| `ea-expired` | Reply window lapsed with no response |

Labels are applied to the **original thread** (inbound: the email from them; outbound: the email you sent), so the outcome is visible in context.

---

## Email Headers

All EA-initiated outbound emails carry:

```
X-EA-Original-Thread: <original_gmail_thread_id>
```

This provides a stable lookup key back to the state store entry regardless of subject line or body content. The poll loop uses this header when scanning threads to identify which are EA-managed.

---

## Recursive Rounds

Both flows support multiple back-and-forth rounds without opening new threads:

- **Inbound**: your modified reply re-runs `evaluate_parsed`; the result is sent on the same private thread; `state.json` updated in place.
- **Outbound**: their counter-proposal re-runs slot-finding; new suggestions are sent on the same original thread; `state.json` updated in place.

In both cases `expires_at` is reset on each exchange, so a genuine negotiation does not silently time out mid-conversation.
