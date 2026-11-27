import json
import unittest
from unittest.mock import AsyncMock

from nex.conversation.goal_edit_fields import edit_schema
from nex.conversation.goal_lookup import PAGE_SIZE, clarified_edit_target
from nex.conversation.goal_management_prompts import RESOLVE_PDF_REMOVAL
from nex.conversation.goal_pdf_removal import decide_page, remove_pdf


class GoalSubjectLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_clarification_separates_requested_and_protected_targets(self):
        candidates = [{"target": 3, "title": "Greenhouse humidity"}, {"target": 7, "title": "Greenhouse temperature"}]
        context = {"question": "Humidity, please. Leave temperature alone.",
                   "clarification": [{"content": "Move the greenhouse check to noon."}]}
        for quote, expected in (("Humidity", candidates[:1]), ("Watering", [])):
            model = AsyncMock(return_value=json.dumps({"target": 3, "reference": quote}))
            self.assertEqual(await clarified_edit_target(candidates, context, model), expected)
            model.assert_awaited_once()

    async def test_edit_field_authorization_can_only_quote_the_current_request_or_its_clarification(self):
        data = {"latest_edit_request": "The humidity one.", "selected_goal": {"title": "Greenhouse humidity"}}
        edit = AsyncMock(return_value=json.dumps({"fields": ["start"], "authorization": "Move it to noon"}))
        self.assertIsNone(await edit_schema(data, edit))
        clarification = [{"role": "user", "content": "Move it to noon please."},
                         {"role": "assistant", "content": "Which greenhouse check?"}]
        selected = await edit_schema(data | {"clarification": clarification}, edit)
        self.assertEqual(set(selected["properties"]), {"start", "timing"})
        edit.return_value = json.dumps({"fields": ["query"], "authorization": "Which greenhouse check?"})
        self.assertIsNone(await edit_schema(data | {"clarification": clarification}, edit))

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
