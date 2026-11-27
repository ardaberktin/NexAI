import json
import unittest
from unittest.mock import AsyncMock

from nex.conversation.goal_lookup import PAGE_SIZE, clarified_edit_target
from nex.conversation.goal_management_prompts import RESOLVE_PDF_REMOVAL
from nex.conversation.goal_pdf_removal import decide_page, remove_pdf


class GoalSubjectLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_clarification_separates_requested_and_protected_targets(self):
        candidates = [{"title": "Greenhouse humidity"}, {"title": "Greenhouse temperature"}]
        context = {"question": "Humidity, please. Leave temperature alone.",
                   "clarification": [{"content": "Move the greenhouse check to noon."}]}
        preserve = '{"role":"preserve","words":"Leave temperature alone"}'
        for quote, expected in (("Humidity", candidates[:1]), ("Watering", [])):
            model = AsyncMock(side_effect=[json.dumps({"role": "change", "words": quote}), preserve])
            self.assertEqual(await clarified_edit_target(candidates, context, model), expected)
            self.assertEqual(model.await_count, 2)

    async def test_a_shared_subject_on_different_pages_cannot_be_treated_as_unique(self):
        goals = [{"id": str(n), "target": n, "title": f"Task {n}", "task": "Routine update."}
                 for n in range(1, PAGE_SIZE + 2)]
        first, last = goals[0], goals[-1]
        first["title"], last["title"] = "Greenhouse humidity", "Greenhouse temperature"
        question = "Please turn off PDFs for the greenhouse."
        pages = []

        async def generate(messages, _tokens):
            self.assertEqual(messages[0]["content"], RESOLVE_PDF_REMOVAL)
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["latest_message"], question)
            pages.append(data["goals"])
            selections = {label: label.partition(". ")[2] in (first["title"], last["title"]) for label in data["goals"]}
            return json.dumps({"action": "disable_future_pdfs", "subject": "greenhouse", "goals": selections})

        result = await remove_pdf(None, goals, goals, {"question": question}, generate)
        self.assertEqual([len(page) for page in pages], [PAGE_SIZE, 1])
        self.assertEqual(result.data["goal_choices"], [first["id"], last["id"]])

    async def test_short_answer_and_original_request_reach_one_model_decision(self):
        candidate = {"target": 3, "title": "Pass renewal", "task": "Check on the renewal."}
        exchange = [{"role": "user", "content": "Give me just text from now on."},
                    {"role": "assistant", "content": "Which goal?"}]
        decision = {"action": "disable_future_pdfs", "subject": "the pass", "goals": {"3. Pass renewal": True}}
        generate = AsyncMock(return_value=json.dumps(decision))
        context = {"question": "the pass", "history": exchange, "clarification": exchange}
        self.assertEqual(await decide_page([candidate], context, generate), [candidate])
        generate.assert_awaited_once()
        messages, _ = generate.await_args.args
        self.assertEqual(json.loads(messages[1]["content"]),
                         {"request": exchange[0]["content"], "goals": {"3. Pass renewal": candidate["task"]},
                          "current_settings": {"3. Pass renewal": {"last_discussed": False, "pdf_enabled": False}}})
        self.assertEqual(messages[2:], [exchange[1], {"role": "user", "content": "the pass"}])
