"""Each tuple is one conversation in a fresh archive. Expectations precede observation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Turn:
    key: str
    prompt: str
    expected: str


@dataclass(frozen=True)
class Scenario:
    turns: tuple[Turn, ...]
    seeds: tuple[str, ...] = ()
    mail: str = "mixed"


SCENARIOS = (
    Scenario((Turn("B1", "I am waiting for a mail to come in.",
                   "Clarify the kind of mail or offer help without claiming a watch or creating one."),)),
    Scenario((Turn("B2", "Check my emails for job postings.",
                   "Read Gmail once. Identify the Northstar opening and exclude the two non-postings. No goal."),)),
    Scenario((Turn("B3", "Any openings buried in my inbox? Just look this once.",
                   "Find the Northstar opening with a one-time inbox read. No watch."),)),
    Scenario((Turn("B4", "How would I set up a goal to watch for recruiter emails?",
                   "Explain setup without reading mail or saving a goal."),)),
    Scenario((Turn("B5", "Check my emails for job postings.",
                   "Report that no matching mail was found after a successful empty read. No goal."),), mail="empty"),
    Scenario((Turn("B6", "Check my emails for job postings.",
                   "Report the Gmail connection problem without claiming the inbox is empty. No goal."),), mail="outage"),
    Scenario((
        Turn("C1", "Every hour, check for new emails with job postings and tell me when one arrives.",
             "Save one active hourly Gmail arrival watch for job postings."),
        Turn("F1", "Actually, check it every 15 minutes instead.",
             "Edit the just-created watch in place. Preserve the subject and Gmail binding."),
        Turn("F2", "Only tell me about remote engineering roles from now on.",
             "Narrow the same watch. Preserve the 15-minute cadence and Gmail binding."),
    )),
    Scenario((Turn("C2", "I’m waiting for an email from Harbor Housing about my lease renewal. Let me know when it comes in.",
                   "Save a future Gmail watch constrained to Harbor Housing and lease renewal."),)),
    Scenario((Turn("C3", "Look through my emails for job postings now, and keep checking for new ones hourly.",
                   "Return the matching posting now and save one hourly Gmail watch."),)),
    Scenario((Turn("C4", "My replacement bank card is coming by post. Ask me tomorrow at 6 pm if it arrived.",
                   "Schedule a user check-in tomorrow at 18:00 Vancouver time about the physical card."),)),
    Scenario((Turn("C5", "Every Monday at 8 am, give me a summary of my calendar for the week ahead.",
                   "Save a weekly Monday 08:00 goal to summarize the upcoming calendar week."),)),
    Scenario((Turn("C6", "Every evening at 7 pm, give me a list of my overdue TickTick tasks.",
                   "Save a daily 19:00 goal for overdue TickTick tasks."),)),
    Scenario((Turn("C7", "Create a goal for tomorrow at 2 pm to remind me to bring my passport to the appointment.",
                   "Save one nonrepeating passport reminder for tomorrow at 14:00."),)),
    Scenario((Turn("C8", "Check my email for new apartment listings every 30 minutes.",
                   "Save the exact cadence if supported or explain the limit and ask before substituting."),)),
    Scenario((Turn("C9", "On weekdays at 8 am, check for new internship emails. Skip weekends.",
                   "Implement weekdays at 08:00 or disclose the limitation and ask. No silent daily substitution."),)),
    Scenario((Turn("E1", 'Rename my "Job leads" goal to "Career radar". Keep everything else the same.',
                   "Change only the title on Job leads, preserving cadence, filter and sibling."),), ("jobs", "rent")),
    Scenario((Turn("E2", "Move my Weekly planning goal to Fridays at 4 pm.",
                   "Move the existing calendar summary to weekly Friday 16:00. Preserve task and sibling."),),
             ("weekly", "jobs")),
    Scenario((Turn("E3", "For Job leads, exclude unpaid internships but keep watching for new postings.",
                   "Edit the existing filter to exclude unpaid internships. Preserve provider, cadence and sibling."),),
             ("jobs", "rent")),
    Scenario((
        Turn("E4", "Also attach a PDF to every update from Weekly planning.",
             "Enable PDF output for Weekly planning. Keep task, schedule and sibling intact."),
        Turn("E5", "Actually, stop attaching the PDFs for that one.",
             "Remove PDF output from the same goal. Preserve its task and schedule."),
    ), ("weekly", "jobs")),
    Scenario((Turn("E6", "My replacement card finally arrived. Mark Card delivery complete.",
                   "Mark only Card delivery completed, with no future execution."),), ("card", "rent")),
    Scenario((Turn("E7", "Delete the Rent listings goal. Keep Job leads.",
                   "Delete Rent listings only and confirm the deletion."),), ("jobs", "rent")),
    Scenario((Turn("E8", "Move my weekly summary to 9 am.",
                   "Ask which of the two weekly summaries should move. Change neither."),), ("weekly", "budget")),
    Scenario((Turn("E9", "Change my gym check-in to 5 pm.",
                   "Identify the missing target or clarify. Preserve existing goals and do not create a gym goal."),),
             ("jobs", "rent")),
    Scenario((Turn("E10", "Pause Job leads for now. I’ll tell you when to start it again.",
                   "Set Job leads to paused, or clearly explain Chat cannot pause it. Preserve its definition and sibling."),),
             ("jobs", "rent")),
    Scenario((Turn("E11", "Don't change anything about Job leads. I just wanted to know what it checks.",
                   "Describe the saved filter without running or changing either goal."),), ("jobs", "rent")),
    Scenario((Turn("C10", "Set up a goal that reminds me to take the recycling bins out every Thursday at 7 pm.",
                   "Save a weekly Thursday 19:00 recycling reminder."),)),
    Scenario((Turn("C11", "Every morning at 7:30, tell me if I need an umbrella for my bike ride in Victoria, BC.",
                   "Save a daily 07:30 Victoria weather check for the bike ride."),)),
    Scenario((Turn("C12", "Please watch my email for a Northstar interview invitation. Don't send any emails.",
                   "Save a Gmail watch limited to Northstar interview invitations. No outbound action."),)),
    Scenario((Turn("E12", "The replacement bank card hasn’t arrived yet. Keep my Card delivery goal open.",
                   "Acknowledge the pending outcome. Preserve the active check-in and the sibling."),), ("card", "rent")),
    Scenario((
        Turn("E13", "Move the job watch to 9 am.",
             "Ask which daily job watch to move. Both titles match equally and neither should change."),
        Turn("F3", "The design one. Keep the engineering one at 8.",
             "Move only Design job watch to 09:00 daily and preserve both filters."),
    ), ("design", "engineering")),
)
