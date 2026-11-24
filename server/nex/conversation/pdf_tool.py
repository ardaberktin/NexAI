"""Create private PDFs from the turn, authorized tool results, and saved goal context."""

import asyncio

from ..goal_writing import json_object, model_messages
from ..goals import Goals
from ..pdf import artifacts
from ..pdf.document import WRITE_DOCUMENT, Document, PDFError
from ..pdf.render import render
from ..pdf.web import WRITE_WEB_DOCUMENT, document_with_sources
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


async def create_pdf(store, turn, tool, evidence, generate):
    identifier = artifacts.request_id(turn.device, turn.id)
    if receipt := await asyncio.to_thread(load_receipt, store, identifier):
        return receipt
    web = evidence.get("web")
    if isinstance(web, dict) and (failure := web.get("unavailable")):
        return {"status": "web_unavailable", "detail": failure}
    goal = None
    try:
        if tool.get("goal_query"):
            goal = await find_goal(store, turn, tool["goal_query"], generate)
        context = turn.prompt() | {"document_brief": tool.get("query", ""), "evidence": evidence}
        if goal:
            context["saved_goal"] = {key: goal[key] for key in ("title", "prompt", "latest")}
        if web:
            context["cite_web_with_numbers"] = True
        instructions = WRITE_WEB_DOCUMENT if web else WRITE_DOCUMENT
        revision = ""
        for attempt in range(2):
            raw = await generate(model_messages(instructions + revision, context), 2600)
            if not raw:
                context["revision_needed"] = "Return the complete document as concise JSON using the supplied evidence."
                status = "model_unavailable"
                continue
            try:
                document = Document.read(raw)
                if web:
                    document = await document_with_sources(document, turn, evidence, generate, revised=bool(attempt))
                pdf = await asyncio.to_thread(render, document)
            except PDFError as error:
                if error.code == "renderer_unavailable":
                    raise
                context["revision_needed"] = str(error)
                status = error.code
                if status == "source_verification_failed":
                    revision = ("\nRevise using concise, directly supported claims. Omit any disputed detail. "
                                "Keep the requested layout and the numbered citations.")
            else:
                return await asyncio.to_thread(save, store, identifier, goal["id"] if goal else None, pdf)
        detail = ("The drafted claims could not be verified using the retrieved pages."
                  if status == "source_verification_failed" else context["revision_needed"])
        return {"status": status, "detail": detail}
    except PDFError as error:
        return {"status": error.code, "detail": str(error)}
    except (FeatureDisabled, WorkspaceMissing, ValueError) as error:
        return {"status": "goal_unavailable", "detail": str(error)}
