"""Run feed and goal work independently of chat and recording jobs."""

import logging
import threading

from .feed import Feed
from .goals import Goals
from .push_queue import PushQueue

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

    def curate(messages, tokens):
        # Curation is optional when chat/recording already owns the model.
        return engines.generate(messages, tokens, settings.read(), wait=False)

    def build_edition():
        feed.run(curate)

    jobs = (
        (build_edition, "daily feed", 5),
        (goals.run, "scheduled goals", 5),
        (PushQueue(store).run, "iPhone push", 2),
    )
    for job, label, interval in jobs:
        threading.Thread(target=run_until_stopped, args=(job, label, interval, stop),
                         name=label.replace(" ", "-"), daemon=True).start()
