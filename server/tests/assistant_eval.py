"""Run synthetic prompts through Nex against a disposable llama-server."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx

from assistant_eval_cases import BOUNDARY_CASES, SCHEDULED_CASES, TOOL_CASES
from assistant_eval_fixtures import (
    REMOVAL_FOLLOWUPS,
    REMOVAL_GROUPS,
    execute,
    execute_scheduled,
    removal_clarification,
)
from nex.conversation.goal_management_prompts import RESOLVE_PDF_REMOVAL
from nex.conversation.plan import Turn, choose
from nex.goal_writing import json_object
from nex.live.dialogue import Dialogue


class MeasuredModel:
    def __init__(self, dialogue):
        self.dialogue = dialogue
        self.calls = []
        self.removal_decisions = 0

    async def generate(self, messages, tokens):
        data = {"messages": messages, "max_tokens": tokens, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False}}
        if schema := getattr(messages, "response_format", None):
            data["response_format"] = schema
        started = monotonic()
        client = self.dialogue.local
        if budget := getattr(self.dialogue, "budget_completion", None):
            data = await budget(client, data)
        response = await client.post("/v1/chat/completions", json=data)
        if not response.is_success:
            self.calls.append({"tokens": 0, "ms": round((monotonic() - started) * 1000), "retry": False,
                               "output": response.text})
            response.raise_for_status()
        body = response.json()
        answer = body["choices"][0]["message"]["content"].strip()
        payload = messages[1]["content"]
        revision = json.loads(payload).get("revision_needed") if payload.startswith('{"') else None
        instructions = messages[0]["content"]
        removal = instructions.startswith(RESOLVE_PDF_REMOVAL)
        if removal:
            self.removal_decisions += 1
        repair = ((len(messages) > 2 and not removal) or (removal and instructions != RESOLVE_PDF_REMOVAL)
                  or bool(revision) or instructions.startswith("The first plan was invalid.")
                  or "\nAnswer each part using its own source." in instructions
                  or "\nYour draft repeats the previous reply verbatim." in instructions
                  or "\nCheck the next run against schedule_text" in instructions)
        self.calls.append({"tokens": body["usage"]["prompt_tokens"], "ms": round((monotonic() - started) * 1000),
                           "retry": repair, "output": answer})
        return answer


def cases():
    for name, prompts in TOOL_CASES.items():
        for index, prompt in enumerate(prompts):
            yield name, prompt, () if name == "answer" else (name,), "en", index % 2 == 1
    for name, prompt, expected, language in BOUNDARY_CASES:
        yield name, prompt, expected, language, False


async def run_scheduled(dialogue, groups, anchor):
    loop = asyncio.get_running_loop()
    failed = 0
    for group, prompts in SCHEDULED_CASES.items():
        if groups and group not in groups:
            continue
        for prompt in prompts:
            model = MeasuredModel(dialogue)
            turn = Turn(str(uuid4()), "mac", prompt, "en", "UTC", started=anchor)
            started = monotonic()

            def generate(messages, tokens, model=model):
                return asyncio.run_coroutine_threadsafe(model.generate(messages, tokens), loop).result()

            expected = set() if group == "reminder" else {group}
            plan = []
            try:
                delivery = await asyncio.to_thread(execute_scheduled, turn, generate)
                drafts = [json_object(call["output"]) for call in model.calls]
                plan = next((draft["tools"] for draft in drafts if "tools" in draft), [])
                passed = ({tool.get("name") for tool in plan if isinstance(tool, dict)} == expected
                          and delivery["passed"] and (group != "create_pdf" or delivery["pdf"]))
            except (httpx.HTTPError, ValueError) as error:
                passed, delivery = False, {"error": str(error)}
            failed += not passed
            print(json.dumps({"group": group, "surface": "scheduled", "prompt": prompt, "expected": sorted(expected),
                              "plan": plan, "passed": passed, "ms": round((monotonic() - started) * 1000),
                              "delivery": delivery, "calls": model.calls}, ensure_ascii=False), flush=True)
    return failed


async def run(socket, groups, execute_tools, scheduled):
    anchor = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=2)
    dialogue = Dialogue(None, socket, None)
    failed = 0
    try:
        if scheduled:
            return await run_scheduled(dialogue, groups, anchor)
        for group, prompt, expected, language, spoken in cases():
            if groups and group not in groups:
                continue
            model = MeasuredModel(dialogue)
            turn = Turn(str(uuid4()), "mac", prompt, "auto", "UTC", started=anchor, spoken=spoken)
            started = monotonic()
            if group in REMOVAL_FOLLOWUPS:
                turn.history = removal_clarification(turn, group)
            selected, status, delivery = (), "", None
            try:
                plan = await choose(turn, model.generate)
                selected, status = plan.tools, plan.error
                passed = {item["name"] for item in selected} == set(expected) and not status and turn.language == language
                if passed and execute_tools:
                    delivery = await execute(turn, group, plan, model.generate)
                    passed = delivery["passed"]
                    if group in REMOVAL_GROUPS:
                        delivery["removal_decisions"] = model.removal_decisions
                        passed = delivery["passed"] = passed and model.removal_decisions == 1
            except (httpx.HTTPError, ValueError) as error:
                passed, status = False, str(error)
            failed += not passed
            print(json.dumps({"group": group, "surface": "voice" if spoken else "chat", "prompt": prompt,
                              "expected": expected, "plan": selected, "status": status, "passed": passed,
                              "language": turn.language, "ms": round((monotonic() - started) * 1000),
                              "delivery": delivery, "calls": model.calls}, ensure_ascii=False), flush=True)
    finally:
        await dialogue.close()
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path, help="Path to a disposable llama-server Unix socket")
    parser.add_argument("groups", nargs="*", help="Optionally restrict the benchmark to these case group names")
    parser.add_argument("--execute", action="store_true", help="Run matched plans with disposable data and providers")
    parser.add_argument("--scheduled", action="store_true", help="Evaluate scheduled planning and delivery instead")
    args = parser.parse_args()
    if not args.socket.is_socket():
        parser.error("provide an existing Unix socket")
    raise SystemExit(bool(asyncio.run(run(args.socket, set(args.groups), args.execute, args.scheduled))))


if __name__ == "__main__":
    main()
