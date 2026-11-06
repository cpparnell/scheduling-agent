# Scheduling Agent

Turns the user's text conversations into calendar entries for the plans the user is actually part of.

## Language

**Commitment**:
A plan the user is part of: one they proposed, accepted (including a tentative "maybe"), made themselves (a reservation or ticket confirmation), or that someone made on their behalf without objection.
_Avoid_: event (for the plan itself), "plan I'm in on"

**Suggestion**:
A possible Commitment the system was not sure enough about to put on the calendar; it waits in the tray until the user adds or dismisses it.
_Avoid_: pending event, draft

**Rationale**:
The system's stated reason for a Commitment or Suggestion — the quoted evidence, the day it resolved to and why, and why the user is part of it.
_Avoid_: confidence score (a confidence value is internal routing, not shown)

**Correction**:
A user action in the tray that tells the system it was wrong — a removal with a reason, or a fix such as moving to the right day.
_Avoid_: feedback (too broad), delete

**Correction reason**:
Why the user corrected an entry: one of a fixed set of one-tap reasons, or a short free-text "other".
