"""Route workspace requests after the shared Nex authentication gate."""

import re
from datetime import date

from . import goal_follow_ups, shared_weather, workspace_features
from .clock_alerts import ClockAlerts
from .feed import Feed
from .goals import IDENTIFIER, Goals
from .push_queue import PushQueue
from .workspace_chat_pages import chat_page, paged_chat_snapshot
from .workspace_content import artifacts, chat_snapshot
from .workspace_ideas import WorkspaceIdeas
from .workspace_sync import watch_chat


def workspace_request(api, method, path, query, device, problem):
    store = api.app.store
    sharing = "/v1/workspace/weather-location"
    if path in (sharing, sharing + "/fix", sharing + "/clear"):
        if device not in ("mac", "iphone") or method != "PUT":
            raise problem(403, "Weather location sharing is available on paired devices.")
        data = api.object(512)
        if path == sharing:
            return shared_weather.enable(store, device)
        if path.endswith("/fix"):
            return shared_weather.update(store, device, data)
        return shared_weather.clear(store, device, data)
    if path == "/v1/workspace/features":
        if method == "GET":
            return workspace_features.settings(store)
        if method == "PUT":
            data = api.object(256)
            return workspace_features.update(store, data.get("feature"), data.get("enabled"))
    if path == "/v1/workspace/clock-alerts" and method == "GET":
        if device not in ("mac", "iphone"):
            raise problem(403, "Use a paired device to receive alarms and timers.")
        return ClockAlerts(store).notification_schedule(
            device, installation=query.get("native_installation") if device == "iphone" else None,
        )
    if path == "/v1/workspace/clock-alerts" and method == "PUT":
        if device != "iphone":
            raise problem(403, "Use the paired iPhone to confirm system alarms.")
        data = api.object(32_000)
        return ClockAlerts(store).report_native(data.get("installation"), data.get("alarms"))
    if method == "POST" and (clock := re.fullmatch(r"/v1/workspace/clocks/(" + IDENTIFIER + ")/cancel", path)):
        api.object(64)
        return ClockAlerts(store).cancel(clock[1])
    section = path.split("/")[3]
    feature = "goals" if section == "goal-alerts" else section
    if feature in {"feed", "ideas", "goals"}:
        workspace_features.require_enabled(store, feature)
    if path == "/v1/workspace/iphone-recording":
        if method == "PUT" and device == "iphone":
            return api.server.recording_status.report(device, api.object(256).get("recording"))
        if method == "GET" and device == "mac":
            return api.server.recording_status.read("iphone", query.get("previous"))
        raise problem(403, "The paired iPhone reports recording status to the paired Mac.")
    if path == "/v1/workspace/recording-status":
        return recording_status_request(api, method, query, device, problem)
    if method == "GET" and path == "/v1/workspace/chat":
        if "before" in query:
            return chat_page(store, query["before"])
        cursor = query.get("cursor")
        paged = query.get("paged") == "1"
        if query.get("wait") == "1":
            return watch_chat(store, cursor, paged=paged)
        snapshot = paged_chat_snapshot if paged else chat_snapshot
        return snapshot(store, cursor)
    if method == "GET" and path == "/v1/workspace/goal-alerts":
        if device != "mac":
            raise problem(403, "Use the paired Mac to read local goal alerts.")
        after = int(query.get("after", "0"))
        if not 0 <= after < 2**63:
            raise ValueError("Invalid goal alert cursor.")
        return Goals(store).alerts(after)
    if path == "/v1/workspace/ideas":
        ideas = WorkspaceIdeas(store)
        if method == "GET":
            return ideas.overview()
        if method == "POST":
            api.object(64)
            return ideas.enqueue()
    zone = query.get("time_zone", "UTC")
    feed = Feed(store)
    goals = Goals(store)
    if path == "/v1/workspace/push":
        if device != "iphone" or method != "PUT":
            raise problem(403, "Use Nex on the paired iPhone to register for push.")
        data = api.object(1_000)
        return PushQueue(store).register(data.get("installation"), data.get("token"),
                                         data.get("environment"), data.get("enabled"))
    if path == "/v1/workspace/feed":
        if method == "GET":
            day = date.fromisoformat(query["day"]).isoformat() if query.get("day") else None
            return feed.overview(zone, day=day)
        if method == "PUT":
            data = api.object(10_000)
            feed.update(data.get("settings", data), refresh_now=data.get("refresh_now", False))
            return feed.overview(zone)
        if method == "POST":
            feed.refresh()
            return feed.overview(zone)
    elif path == "/v1/workspace/feed/saved" and method == "PUT":
        data = api.object(1_000)
        feed.save(data.get("id"), data.get("saved"))
        return feed.overview(zone)
    elif path == "/v1/workspace/goals" and method == "GET":
        overview = {"items": goals.overview()}
        if query.get("clocks") == "1":
            overview["clock_alerts"] = ClockAlerts(store).overview()
        return overview
    elif match := re.fullmatch(r"/v1/workspace/goals/(" + IDENTIFIER + ")(/artifacts|/follow-ups)?", path):
        identifier = match[1]
        if match[2] == "/artifacts":
            if method == "GET":
                return {"items": artifacts(store, identifier)}
        elif match[2] == "/follow-ups":
            if method == "POST":
                data = api.object(1_000)
                follow_up = data.get("follow_up_id")
                if not isinstance(follow_up, str) or not re.fullmatch(IDENTIFIER, follow_up):
                    raise ValueError("Choose a valid follow-up.")
                return goal_follow_ups.answer(store, identifier, follow_up, data.get("choice"))
        elif method == "GET":
            return goals.detail(identifier)
        elif method == "PUT":
            return goals.save(identifier, api.object(10_000))
        elif method == "POST":
            data = api.object(1_000)
            if data.get("action") == "delete":
                return goals.delete(identifier, data.get("revision"))
            return goals.action(identifier, data.get("action"), data.get("revision"))
    elif method == "GET" and path == "/v1/workspace/artifacts":
        return {"items": artifacts(store)}
    raise problem(404, "This Nex workspace endpoint was not found.")


def recording_status_request(api, method, query, device, problem):
    presence = api.server.recording_status
    if method == "GET" and device in ("mac", "iphone"):
        return presence.read(previous=query.get("previous"))
    if method == "PUT" and device in ("mac", "iphone"):
        data = api.object(256)
        source = data.get("source", device)
        if device == "iphone" and source == "watch":
            return presence.report(source, data.get("recording"), valid_for=data.get("valid_for"),
                                   changed_at=data.get("changed_at"))
        if source == device:
            return presence.report(source, data.get("recording"))
    raise problem(403, "Use a paired recorder to update its own recording status.")
