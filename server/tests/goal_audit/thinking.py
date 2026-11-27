"""Run the paired Qwen goal matrix and record timing and reasoning metadata, excluding thought text."""

import argparse
import asyncio
import json
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from time import monotonic

from cases import SCENARIOS
from fixtures import LocalModel, only_model_socket
from run import evaluate
from scheduled import evaluate_polls

from nex.live.dialogue import Dialogue


class MeasuredQwen(LocalModel):
    def __init__(self, path, enabled):
        super().__init__(path)
        self.enabled = enabled
        self.measurements = []

    def chat(self, messages, config, language, limit, on_speech, *, thinking, **options):
        if thinking is not self.enabled:
            raise ValueError("Chat lost the requested thinking preference")
        self.calls += 1
        return asyncio.run(self.complete(messages, limit, thinking=thinking))

    async def complete(self, messages, limit, *, thinking=None):
        dialogue = Dialogue(None, self.path, None, local_model_id=lambda: "qwen-3.5-4b")
        dialogue.local.event_hooks["response"].append(self.measure)
        try:
            return await dialogue.complete(dialogue.local, messages, limit, temperature=0,
                                           thinking=self.enabled if thinking is None else thinking)
        finally:
            await dialogue.close()

    async def measure(self, response):
        if response.request.url.path != "/v1/chat/completions":
            return
        response.raise_for_status()
        await response.aread()
        payload = response.json()
        choice = payload["choices"][0]
        options = json.loads(response.request.content)
        self.measurements.append({
            "stage": options["messages"][0]["content"].splitlines()[0][:95],
            "result": choice["message"].get("content"),
            "thinking_enabled": options["chat_template_kwargs"]["enable_thinking"],
            "reasoning_budget_tokens": options.get("reasoning_budget_tokens", 0),
            "reasoning_present": bool(choice["message"].get("reasoning_content")),
            "completion_tokens": payload["usage"]["completion_tokens"],
            "finish_reason": choice["finish_reason"],
        })

    def record(self, row):
        calls = row["model_calls"]
        row["inference"] = self.measurements[:calls]
        if len(self.measurements) > calls:
            if "deferred_follow_up" not in row:
                raise RuntimeError("Unexpected additional inference calls")
            row["deferred_inference"] = self.measurements[calls:]
        if len(row["inference"]) != calls:
            raise RuntimeError("Missing inference measurements")
        self.measurements.clear()
        print(json.dumps(row, ensure_ascii=False), flush=True)


def fixed_clock(instant):
    if instant is None:
        return nullcontext()
    if instant.tzinfo is None:
        raise ValueError("--at needs a date and time with a UTC offset")
    try:
        import time_machine
    except ImportError as exc:
        raise RuntimeError("Install time-machine 3.x for fixed-clock runs") from exc
    from importlib.metadata import version
    if int(version("time-machine").split(".")[0]) < 3:
        raise RuntimeError("Use time-machine 3.x or newer to preserve monotonic timing")
    return time_machine.travel(instant, tick=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--mode", choices=("on", "off"), required=True)
    parser.add_argument("--at", type=datetime.fromisoformat, help="Optional fixed fixture time including UTC offset")
    args = parser.parse_args()
    with fixed_clock(args.at):
        only_model_socket(args.socket)
        run_matrix(args.socket, args.mode)


def run_matrix(socket, mode):
    model = MeasuredQwen(socket, mode == "on")
    for scenario in SCENARIOS:
        for row in evaluate(model, scenario, check_follow_up=scenario.turns[0].key == "C4", thinking=model.enabled):
            model.record(row)
    started = monotonic()
    for row in evaluate_polls(model):
        row["seconds"] = round(monotonic() - started, 2)
        model.record(row)
        started = monotonic()


if __name__ == "__main__":
    main()
