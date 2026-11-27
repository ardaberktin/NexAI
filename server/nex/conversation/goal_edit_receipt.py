"""Expose the actual changed settings to the model that confirms an edit."""

from datetime import datetime

from ..recall_dates import time_zone

WRITE_EDIT = """The change in this receipt has already been saved. Tell the user what their change
accomplished in one sentence, using their language. Describe only the resulting changed_settings.
A prompt change is the updated task or matching criteria. A frequency_minutes change is the new polling
interval. A scheduled_local change moves the next start. A title change renames the goal. Treat a saved
title as a label. Mention a schedule or time only when it is a changed setting."""

VERIFY_EDIT = """Does the draft tell the user the recorded change took effect, with the correct new value?
A natural paraphrase of a task or filter is sufficient. An explicit saved title is optional. Reject a
claim to have run the task, an unsupported change, a mere proposal, or an incorrect new value.
Reply only YES or NO."""


WRITE_WATCH = """Confirm the saved Gmail arrival watch in one sentence. Use its subject, the saved
frequency in natural language, and what triggers an alert. Distinguish future polling from a current
inbox search. Include an explicit first-run time only if the user requested one. Use the user's language."""
VERIFY_WATCH = """Does the confirmation accurately describe this saved future watch?
Require its subject, its correct polling interval, and notification on a match. If a clock is stated it
must agree with schedule_text. Allow a future description of checking mail. Reject an assertion of past
search results, immediate detection, or different conditions. Reply only YES or NO."""


def changed_settings(before, after, check_in):
    fields = ("title", "prompt", "frequency_minutes")
    changes = {field: after[field] for field in fields if before[field] != after[field]}
    if before["scheduled_at"] != after["scheduled_at"] or before["zone"] != after["zone"]:
        when = datetime.fromisoformat(after["scheduled_at"]) if after["scheduled_at"] else None
        changes["scheduled_local"] = when.astimezone(time_zone(after["zone"])).isoformat(timespec="minutes") if when else None
    if before.get("check_in", False) != check_in:
        changes["check_in"] = check_in
    return changes
