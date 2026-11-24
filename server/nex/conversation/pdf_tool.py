"""Create private PDFs from the turn, authorized tool results, and saved goal context."""

import asyncio
from uuid import NAMESPACE_URL, uuid5

from ..goal_writing import json_object, model_messages
from ..goals import Goals
from ..pdf import artifacts
from ..pdf.document import WRITE_DOCUMENT, Document, PDFError
from ..pdf.render import render
from ..store import now
from ..workspace_features import FeatureDisabled, require
from ..workspace_schema import WorkspaceMissing
from .goal_lookup import describe, saved_goals, shortlist
from .goal_management_prompts import SELECT_GOAL


def load_receipt(store, identifier):
    with store.connect() as db:
        if row := artifacts.existing(db, identifier):
            return artifacts.receipt(row)
    return None


def save(store, identifier, goal, document):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if row := artifacts.existing(db, identifier):
            return artifacts.receipt(row)
        if goal:
            require(db, "goals")
            Goals.find(db, goal)
        artifacts.insert(db, identifier, goal, now(), document)
        return artifacts.receipt(artifacts.existing(db, identifier))


async def find_goal(store, turn, query, generate):
    goals = await asyncio.to_thread(saved_goals, store)
    context = {"question": turn.text, "goal_reference": query, "history": turn.prompt()["history"]}
    descriptions = [describe(goal, index, None) for index, goal in enumerate(goals, start=1)]
    matches = await shortlist(descriptions, context, generate)
    if not matches:
        raise PDFError("goal_unavailable", "No matching saved goal was found.")
    data = context | {"goals": matches, "requested_action": "attach a PDF to the named goal"}
    response = json_object(await generate(model_messages(SELECT_GOAL, data), 160) or "")
    target = response.get("target")
    match = next((row for row in matches if row["target"] == target and row["title"] == response.get("title")), None)
    if type(target) is not int or not match:
        status = "goal_unclear" if response.get("action") == "clarify" else "goal_unavailable"
        raise PDFError(status, "Nex could not identify a single matching goal.")
    return await asyncio.to_thread(Goals(store).detail, goals[target - 1]["id"])


async def create_pdf(store, turn, tool, evidence, generate, verify):
    identifier = str(uuid5(NAMESPACE_URL, f"nex:pdf:{turn.device}:{turn.id}"))
    if receipt := await asyncio.to_thread(load_receipt, store, identifier):
        return receipt
    goal = None
    try:
        if tool.get("goal_query"):
            goal = await find_goal(store, turn, tool["goal_query"], generate)
        context = turn.prompt() | {"document_brief": tool.get("query", ""), "evidence": evidence}
        if goal:
            context["saved_goal"] = {key: goal[key] for key in ("title", "prompt", "latest")}
        if verify:
            context["cite_web_with_numbers"] = True
        for _ in range(2):
            raw = await generate(model_messages(WRITE_DOCUMENT, context), 2600)
            if not raw:
                return {"status": "model_unavailable"}
            try:
                document = Document.read(raw)
                if verify and not await verify(document.text()):
                    raise PDFError("source_verification_failed", "The draft's web claims could not be verified.")
                pdf = await asyncio.to_thread(render, document)
            except PDFError as error:
                if error.code == "renderer_unavailable":
                    raise
                context["revision_needed"] = str(error)
                status = error.code
            else:
                return await asyncio.to_thread(save, store, identifier, goal["id"] if goal else None, pdf)
        return {"status": status, "detail": context["revision_needed"]}
    except PDFError as error:
        return {"status": error.code, "detail": str(error)}
    except (FeatureDisabled, WorkspaceMissing, ValueError) as error:
        return {"status": "goal_unavailable", "detail": str(error)}
