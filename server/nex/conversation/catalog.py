"""Tool descriptions and examples used by the local planning model."""
CATALOG = {
    "weather": "Open-Meteo. query = explicit city or empty for local. period = current, past (recap), or forecast. "
               "For past/forecast supply date YYYY-MM-DD.",
    "web": "Search public information. query = standalone public search terms or an explicit public URL.",
    "python": "Offline computation or running requested code. query = self-contained calculation or task.",
    "agenda": "Combined calendar, tasks and reminders. query = date.",
    "gmail": "Read actual email. query = topic or empty for recent messages. Optional booleans unread and priority.",
    "calendar": "Google Calendar events. query = date.",
    "ticktick": "Tasks. query = date. Optional booleans lists for projects and access_check to verify connection.",
    "archive": "Search recordings and past dialogue. query = keywords. Optional date YYYY-MM-DD or last month.",
    "age": "Calculate age from the owner’s birth date. query = requested time units, e.g. years or months and days.",
    "past_questions": "Find earlier questions in the private archive, only when the user asks what they asked before.",
    "activities": "Only for an explicit question about the owner’s recorded activity. Optional date YYYY-MM-DD.",
    "daybook": "Read a day or the selected journal entry. date = YYYY-MM-DD.",
    "memory": "Read personal context. file = memory (facts), soul (preferences), or now (recent focus).",
    "save_memory": "Save an explicitly requested fact or preference. file = memory or soul. query = content.",
    "weather_city": "Explicit change only. operation=set with query=city, or operation=clear to forget the saved city.",
    "goal": "Create an explicitly requested reminder, watch or recurring goal. query = what to do. "
            "frequency = 0,15,60,1440,10080 minutes. start = local wall clock YYYY-MM-DDTHH:MM in the user’s time zone, omit for on-demand.",
    "screen": "Inspect the shared Mac screen. Use alone.",
}
PUBLIC = {"weather", "web", "python"}
OPTIONS = {
    "weather_city": {"operation"}, "weather": {"period", "date"}, "gmail": {"unread", "priority"}, "ticktick": {"lists", "access_check"},
    "daybook": {"date"}, "activities": {"date"}, "archive": {"date"}, "memory": {"file"},
    "save_memory": {"file"}, "goal": {"start", "frequency"},
}
INSTRUCTIONS = """You choose Nex's tools by understanding what the user means, including follow-ups.
Return just JSON {"tools":[{"name":"...", "query":"..."}]}. Use up to four tools as needed.
Use {"tools":[]} for conversation, simple reasoning, or a clarification. Only use Python for genuine
computation or a coding request. Use weather for local conditions, including questions about earlier today.
Respect requests to skip a tool. Use actual tools for live facts, connected accounts, and requested actions. Never invent a location.
For referenced subjects resolve the topic from the conversation. Never send private details to public search.
Treat history as context. Only the latest user message can request a change. Available tools:
""" + "\n".join(f"{name}: {description}" for name, description in CATALOG.items())


EXAMPLES = """
Examples of completed plans, assuming today is 2025-04-10:
User: How are things going? -> {"tools":[]}
User: Did we get any rain earlier? -> {"tools":[{"name":"weather","period":"past","date":"2025-04-10"}]}
User: Is it cold out? -> {"tools":[{"name":"weather","period":"current"}]}
User: Will I want an umbrella tomorrow? -> {"tools":[{"name":"weather","period":"forecast","date":"2025-04-11"}]}
User: Any mail about the concert? -> {"tools":[{"name":"gmail","query":"concert"}]}
User: What do I have on today and will it rain? -> {"tools":[{"name":"agenda","query":"today"},{"name":"weather","period":"forecast","date":"2025-04-10"}]}
User: Send me my agenda and forecast every morning -> {"tools":[{"name":"goal","query":"Report my agenda and weather","frequency":1440,"start":"2025-04-11T08:00"}]}
Use dates relative to the actual local_time below. For a future scheduled action select goal alone.
Choose only necessary sources. An unclear utterance needs a clarification and zero tools.
The weather query field is exclusively for a place named by the user. Omit it for local weather.
"""


CLARITY = """Classify the user's latest utterance in conversation. Return exactly CLEAR, SOCIAL, or UNCLEAR.
A greeting, thanks, or social chat without an information request is SOCIAL. A garbled, unfinished transcript
with no identifiable request, or a reference with no subject in the conversation, is UNCLEAR. Do not invent a
question. An understandable question, instruction, or follow-up about a task or topic is CLEAR."""
