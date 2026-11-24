import json
import unittest

from nex.conversation.goal_lookup import PAGE_SIZE
from nex.conversation.goal_pdf_removal import (
    EXTRACT_GOAL_REFERENCE,
    FIND_GOAL_SUBJECTS,
    resolve_subjects,
)


class GoalSubjectLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_shared_subject_on_different_pages_cannot_be_treated_as_unique(self):
        candidates = [{"target": n, "title": f"Task {n}", "task": "Routine update."} for n in range(1, PAGE_SIZE + 2)]
        first, last = candidates[0], candidates[-1]
        first["title"], last["title"] = "Greenhouse humidity", "Greenhouse temperature"
        question = "Please turn off PDFs for the greenhouse."

        async def generate(messages, _tokens):
            prompt = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            if prompt == EXTRACT_GOAL_REFERENCE:
                self.assertEqual(data["latest_message"], question)
                return '{"reference":"greenhouse"}'
            self.assertEqual(prompt, FIND_GOAL_SUBJECTS)
            self.assertEqual(data["user_reference"], "greenhouse")
            numbers = [row["number"] for row in data["goals"] if row["number"] in (first["target"], last["target"])]
            return json.dumps({"evidence": "The user named the greenhouse.", "matches": numbers})

        matches = await resolve_subjects(candidates, {"question": question}, generate)
        self.assertEqual(matches, [first, last])

    async def test_unquoted_model_hint_cannot_replace_the_users_request(self):
        question = "Use text for the one that tracks my pass."
        candidate = {"target": 1, "title": "Pass renewal", "task": "Check on the renewal."}

        async def generate(messages, _tokens):
            if messages[0]["content"] == EXTRACT_GOAL_REFERENCE:
                return '{"reference":"Other goal"}'
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["user_reference"], question)
            return '{"evidence":"The subject is the pass.","matches":[1]}'

        self.assertEqual(await resolve_subjects([candidate], {"question": question}, generate), [candidate])
