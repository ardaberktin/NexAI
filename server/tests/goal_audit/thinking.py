"""Run the paired Qwen goal matrix and record timing and reasoning metadata, excluding thought text."""

import argparse
import json
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

    async def complete(self, messages, limit):
        dialogue = Dialogue(None, self.path, None, local_model_id=lambda: "qwen-3.5-4b" if self.enabled else None)
        dialogue.local.event_hooks["response"].append(self.measure)
        try:
            return await dialogue.complete(dialogue.local, messages, limit, temperature=0)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--mode", choices=("on", "off"), required=True)
    args = parser.parse_args()
    only_model_socket(args.socket)
    model = MeasuredQwen(args.socket, args.mode == "on")
    for scenario in SCENARIOS:
        for row in evaluate(model, scenario, check_follow_up=scenario.turns[0].key == "C4"):
            model.record(row)
    started = monotonic()
    for row in evaluate_polls(model):
        row["seconds"] = round(monotonic() - started, 2)
        model.record(row)
        started = monotonic()


if __name__ == "__main__":
    main()
