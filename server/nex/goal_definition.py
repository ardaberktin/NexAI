"""Validate and describe editable goals."""

import json

GOAL_FIELDS = ("id", "title", "prompt", "zone", "scheduled_at", "status", "revision", "next_run",
               "last_checked", "error", "latest")


def field(data, key, length):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > length or "\x00" in value:
        raise ValueError(f"Use up to {length} characters for {key}.")
    return value.strip()


def title_from_description(value):
    title = " ".join(value.split())
    title = title[:1].upper() + title[1:]
    return title[:117].rstrip() + "…" if len(title) > 120 else title


def edited_title(data, prompt, previous):
    if "title" in data:
        title = field(data, "title", 120)
        if not title:
            raise ValueError("Give the goal a name.")
        return title
    if previous and previous["title"] != title_from_description(previous["prompt"]):
        return previous["title"]
    return title_from_description(prompt)


def summary(row):
    goal = {key: row[key] for key in GOAL_FIELDS} | {
        "frequency_minutes": row["interval_minutes"], "can_run_now": True,
    }
    if provider := row["watch_provider"]:
        goal["watch_provider"] = provider
    if instruction := row["pdf_instruction"]:
        goal["pdf_instruction"] = instruction
    if raw_state := dict(row).get("plan_state"):
        try:
            state = json.loads(raw_state)
            if isinstance(state, dict):
                goal["plan_state"] = state
        except ValueError:
            pass
    return goal
