"""Execute validated tool calls. Only the approved private path can open owner data."""
import http.client
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from ..accounts.agenda import agenda_evidence
from ..accounts.http import AccountError
from ..accounts.intent import AccountIntent, mail_variants, search_topic
from ..accounts.read import prepare_account
from ..active_memory import ActiveMemory
from ..activities.recall import activity_evidence, selected_activity
from ..age_intent import AgeRequest, age_request, units_in
from ..browser_tool import inspect_page
from ..conversation_history import recent_dialogue
from ..conversation_recall import recent_question_reply, selected_questions
from ..daybook import Daybook, DayContext
from ..goal_writing import mail_context
from ..news_sources import news_date
from ..passages import matching_passage
from ..personal_age import age_reply
from ..personal_files import PersonalFiles, context_excerpt, safe_line
from ..recall_dates import recall_dates, time_key, time_zone
from ..store import excerpt, search_prefixes
from ..tool_dates import date_selection, local_bounds
from ..weather import Weather
from ..weather_intent import WeatherRequest, weather_city
from ..web_answers import news_pages, prepare_lookup, save_pages
from ..web_intent import PRIVATE, web_request
from ..web_transport import WebReadError
from ..workspace_features import FeatureDisabled, require_enabled
from .goal_tools import save_goal
from .weather import hourly


@dataclass
class Evidence:
    data: object
    sources: list = field(default_factory=list)
    engine: str = "local_model"
    goal_id: str | None = None
    attachment: str = ""
    recall_cursor: dict | None = None


def weather_result(store, turn, tool, coordinates):
    city = tool.get("query", "").strip()
    if city and not weather_city(city):
        raise ValueError("Please give a city and region.")
    request = WeatherRequest(city or None)
    weather = Weather(store)
    period = tool.get("period", "current")
    day = turn.started.astimezone(time_zone(turn.zone)).date() if period == "current" else date.fromisoformat(tool["date"])
    place, status = weather.locate(request, turn.device, turn.zone, turn.language, coordinates=coordinates)
    if place is None:
        return Evidence(status.text, engine="weather_city_needed")
    return Evidence(hourly(place, day, turn.zone, period, turn.started), engine="weather")


def change_weather_city(store, turn, tool):
    weather = Weather(store)
    if tool["operation"] == "clear":
        weather.remember(turn.device, None)
        return Evidence({"saved_city": None, "cleared": True}, engine="weather")
    request = WeatherRequest(tool["query"])
    place, status = weather.locate(request, turn.device, turn.zone, turn.language)
    if place is None:
        return Evidence(status.text, engine="weather_city_needed")
    weather.remember(turn.device, place)
    return Evidence({"saved_city": place.label}, engine="weather")


def web_result(store, turn, tool):
    query = tool.get("query", "").strip()
    if "date" in tool:
        zone = time_zone(turn.device_clock.time_zone if turn.device_clock else turn.zone)
        local = turn.device_clock.local_time() if turn.device_clock else turn.started.astimezone(zone)
        day = news_date(tool["date"], local.date())
        pages = news_pages(day, zone, turn.started, query)
        items = [{"number": index, "title": page.title, "url": page.url, "excerpt": page.text}
                 for index, page in enumerate(pages, start=1)]
        sources = [item["url"] for item in items] if turn.private_chat else save_pages(store, pages)
        return Evidence(items, sources, "web_lookup")
    if not query or PRIVATE.search(query):
        raise WebReadError("This lookup needs a public topic without private details.")
    command = "Search the web for " + query
    target = web_request(command)
    if target.url and query not in turn.text:
        raise WebReadError("Please supply the public page address to open.")
    items, sources, error = prepare_lookup(store, command, archive=not turn.private_chat)
    if error or not items or not any(item["excerpt"] for item in items):
        raise WebReadError(error or "The search returned no usable page excerpts.")
    if turn.private_chat:
        sources = [item["url"] for item in items]
    return Evidence([{**item, "excerpt": excerpt(item["excerpt"], 700)} for item in items], sources, "web_lookup")


def account_result(store, turn, tool):
    name, query = tool["name"], tool.get("query", "")
    intent = AccountIntent(name, False, topic=search_topic(query) if name == "gmail" else "",
                           search_variants=mail_variants(tool, turn.text) if name == "gmail" else (),
                           date_selection=date_selection(tool),
                           unread=tool.get("unread", False), lists=tool.get("lists", False),
                           priority=tool.get("priority", False), access_check=tool.get("access_check", False), include_body=name == "gmail")
    _, rows, error = prepare_account(store.root, intent, query, turn.zone, turn.started)
    items = [mail_context(row, 480) for row in rows[:6]] if name == "gmail" else rows[:12]
    return Evidence(error or items, engine="account_read")


def goal_result(store, turn, tool):
    receipt, identifier = save_goal(store, turn, tool)
    return Evidence(receipt, engine="goal_created", goal_id=identifier)


def browser_result(store, turn, tool):
    url = tool.get("url", "").strip()
    if not url or not url.startswith("https://"):
        raise WebReadError("Provide a public HTTPS page address to inspect.")
    page = inspect_page(
        url, action=tool.get("action", "open"), query=tool.get("query", ""),
        max_links=tool.get("max_links", 12),
    )
    return Evidence(page, [page["url"]], "browser")


def memory_result(store, turn, tool):
    name, query = tool["name"], tool.get("query", "")
    files, key = PersonalFiles(store.root), tool.get("file", "memory")
    active = ActiveMemory(store)
    if name == "memory":
        with files.locked():
            item = files.current(datetime.now(UTC)) if key == "now" else files.read(key)
        markdown_excerpt = context_excerpt(key, item["markdown"], query)
        if key == "memory" and active.count() > 0:
            recalled = active.recall(query or turn.text, limit=8)
            if recalled:
                return Evidence({"markdown": markdown_excerpt, "active_facts": recalled})
        return Evidence(markdown_excerpt)
    content = safe_line(query, 500)
    if not content:
        raise ValueError("Missing content to save.")
    files.append(key, content, heading="## Saved on request" if key == "memory" else "## Owner preferences")
    active.upsert(excerpt(content, 48).rstrip("…"), content,
                  category="preference" if key == "soul" else "fact",
                  source_id=turn.id, sync_markdown=False)
    return Evidence({"saved_in": key, "content": content})


def age_result(store, turn, tool):
    query = tool.get("query", "")
    request = age_request(turn.text, turn.history)
    if request and (units := units_in(query)):
        request = AgeRequest(units, request.birth, request.clock)
    if request is None:
        request = AgeRequest(units_in(query) or ("year",))
    result = age_reply(PersonalFiles(store.root), request, turn.history, turn.started, turn.zone)
    return Evidence(result["text"], engine=result["engine"])


def archive_result(store, turn, tool):
    name, query = tool["name"], tool.get("query", "")
    selection = date_selection(tool)
    bounds = tuple(map(time_key, local_bounds(selection, turn.zone))) if selection else None
    if name == "past_questions":
        rows = recent_dialogue(store, turn.started)
        if "count" in tool or "before" in tool or bounds:
            candidates = selected_questions(store, tool.get("count", 1), bounds, turn.history or rows,
                                            turn.started, turn.exclude, before="before" in tool, zone=turn.zone)
            return Evidence(candidates, engine="archive_search")
        scope = recall_dates(turn.text, turn.zone, turn.started)
        if recent := recent_question_reply(store, turn.text, turn.language, scope, asked_at=turn.started,
                                          exclude=turn.exclude, history=turn.history or rows, zone=turn.zone):
            return Evidence(recent["text"], recent["sources"], "archive_search", recall_cursor=recent.get("recall_cursor"))
    elif name == "archive":
        scope = recall_dates(tool.get("date", ""), turn.zone, turn.started)
        filters = {"limit": 5, "bounds": bounds or (scope.bounds if scope else None),
                   "evidence_only": True, "exclude": turn.exclude}
        rows = store.search(query, **filters)
        if not rows and query:
            # FTS uses prefixes. Retry long inflected terms on their common root.
            roots = " ".join(term[:5] for term in search_prefixes(query) if len(term) > 5)
            if roots:
                rows = store.search(roots, **filters)
    elif name == "activities":
        require_enabled(store, "activities")
        if "mode" in tool:
            rows = selected_activity(store, tool, turn.zone, turn.started, turn.exclude)
        else:
            rows = activity_evidence(store, turn.text, turn.zone, turn.started, exclude=turn.exclude,
                                     scope=recall_dates(tool.get("date", ""), turn.zone, turn.started))
    elif name == "daybook":
        require_enabled(store, "daybook")
        day = tool.get("date") or turn.started.astimezone(time_zone(turn.zone)).date().isoformat()
        context = turn.journal or DayContext.parse(day, turn.zone)
        rows = Daybook(store).evidence(context, turn.text, turn.exclude)
    else:
        raise ValueError("Unsupported private tool.")
    items = [{"date": row.get("started"), "kind": row.get("kind"),
              "text": matching_passage(row["text"], query, limit=500)} for row in rows[-8:]]
    return Evidence(items, [row["id"] for row in rows[-8:]], "archive_search")


def agenda_result(store, turn, tool):
    selection = date_selection(tool)
    args = {"selection": selection} if selection else {}
    data = agenda_evidence(store, tool.get("query") or "today", turn.zone, turn.started, **args)
    return Evidence(data, engine="account_read")


PRIVATE_TOOLS = {
    "gmail": account_result, "calendar": account_result, "ticktick": account_result, "agenda": agenda_result,
    "goal": goal_result, "age": age_result, "weather_city": change_weather_city, "save_memory": memory_result, "memory": memory_result,
    "past_questions": archive_result, "archive": archive_result, "daybook": archive_result, "activities": archive_result,
}


def fetch(store, turn, tool, *, private, coordinates):
    name = tool["name"]
    try:
        if name == "web":
            return web_result(store, turn, tool)
        if name == "browser":
            return browser_result(store, turn, tool)
        if name == "weather":
            return weather_result(store, turn, tool, coordinates)
        if private:
            return PRIVATE_TOOLS[name](store, turn, tool)
        raise PermissionError("This tool requires an unlocked workspace.")
    except (WebReadError, AccountError, FeatureDisabled, ValueError, LookupError, TypeError, OSError, http.client.HTTPException) as exc:
        if isinstance(exc, WebReadError) and name == "web":
            return Evidence({"unavailable": str(exc)}, engine="web_lookup")
        return Evidence({"unavailable": str(exc) if isinstance(exc, (WebReadError, FeatureDisabled, ValueError))
                         else f"Nex could not read {name}."})
