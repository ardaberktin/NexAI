"""Run feed and goal work independently of chat and recording jobs."""

import logging
import threading

from .clock_alerts import ClockAlerts
from .feed import Feed
from .goal_follow_up_jobs import GoalFollowUpJobs
from .goal_follow_up_notices import GoalFollowUpNotices
from .goal_follow_up_pdfs import deliver_pdfs
from .goals import Goals
from .push_queue import PushQueue
from .workspace_ideas import WorkspaceIdeas

logger = logging.getLogger(__name__)


def run_until_stopped(job, label, interval, stop):
    while not stop.is_set():
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - keep scheduled jobs running after an individual failure.
            logger.warning("Nex %s failed (%s)", label, type(exc).__name__)
        stop.wait(interval)


def start_workspace_jobs(store, engines, settings, stop):
    feed = Feed(store)
    goals = Goals(store, engines.classify_background, generate=engines.generate_background)
    follow_ups = GoalFollowUpJobs(store, engines.generate_background)
    notices = GoalFollowUpNotices(store, engines.generate_background)
    ideas = WorkspaceIdeas(store)

    def curate(messages, tokens):
        # Curation is optional when chat/recording already owns the model.
        return engines.generate(messages, tokens, settings.read(), wait=False)

    def build_edition():
        feed.run(curate)

    jobs = (
        (build_edition, "daily feed", 5),
        (lambda: ideas.run(engines.generate_background), "workspace ideas", 2),
        (goals.run, "scheduled goals", 5),
        (ClockAlerts(store).run, "alarms and timers", 1),
        (follow_ups.decide, "goal follow-up decisions", 5),
        (follow_ups.deliver, "goal follow-up delivery", 5),
        (lambda: deliver_pdfs(store, engines.generate_background), "goal follow-up PDFs", 5),
        (notices.run, "goal follow-up confirmations", 5),
        (PushQueue(store).run, "iPhone push", 2),
    )
    for job, label, interval in jobs:
        threading.Thread(target=run_until_stopped, args=(job, label, interval, stop),
                         name=label.replace(" ", "-"), daemon=True).start()
