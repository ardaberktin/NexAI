"""Keep large external tool catalogs usable within the local model's context budget."""

import json

from ..goal_writing import ask, json_object, model_messages
from .tool import PROMPT, catalog_menu

MENU_BYTES = 4_000
SHORTLIST = PROMPT + """
Identify the MCP tools that could help with the attached request. Each entry lists a server,
a tool name, a short description, and whether it may write. Use context to resolve references and respect named sources.
Return a JSON object with tools containing at most four listed identifiers. Return an empty array
if none are relevant. This step selects candidates only and does not execute or schedule a tool.
"""


def shortlist_requests(catalog, question, *, context=()):
    menu = catalog_menu(catalog)
    if len(json.dumps(menu, ensure_ascii=False).encode()) <= MENU_BYTES:
        return []
    batches, current, length = [], {}, 0
    for name, details in menu.items():
        size = len(json.dumps({name: details}, ensure_ascii=False).encode())
        if current and length + size > MENU_BYTES:
            batches.append(current)
            current, length = {}, 0
        current[name] = details
        length += size
    batches.append(current)
    return [(batch, {"question": question, "context": context, "mcp_tools": batch}, {
        "type": "object", "required": ["tools"], "additionalProperties": False,
        "properties": {"tools": {"type": "array", "maxItems": 4, "items": {"enum": sorted(batch)}}},
    }) for batch in batches]


def selected(raw, batch, catalog):
    names = json_object(raw if isinstance(raw, str) else "").get("tools")
    if not isinstance(names, list):
        return {}
    return {name: catalog[name] for name in names[:4] if isinstance(name, str) and name in batch}


WORKFLOWS = ("other", "general", "mixed", "schedule", "recurring", "document")
CHOOSE_CONNECTED = """Does the latest message require account data or an action that a tool in
mcp_candidates can supply? Decide this before choosing tool numbers. A shared topic is insufficient.
For one requested change, choose only the tool whose operation matches. Creating a new item and completing
an existing item are different operations. Select more than one write tool only for distinct requested changes.
Questions about what a term means, how something works, or how to do a task use no MCP tools.
If the user wants local text, a draft to submit themselves, or a reminder to act, omit tools that
commit the external action. Use an external draft tool only if the user requests that provider's draft.
Choose the workflow, up to four numeric tool numbers, and the reply language (en or tr):
other = no connected data or action needed, an unclear reference, or an edit to an existing goal.
general = look up private or provider-owned data or perform a supported action now.
mixed = use a connected tool plus a separate lookup or action explicitly requested in the same message.
A title, address, date, list, message body, or other argument for a single action is part of that action, not mixed.
schedule = set up a new single future run that uses connected data.
recurring = set up a new repeating lookup or report using connected data, such as every week or every Friday.
document = make or schedule a PDF that requires connected data.
For a candidate numbered 2 that reads supplier invoices:
"What is a supplier invoice?" -> {"workflow":"other","tools":[],"language":"en"}
"Write an order draft so I can submit it myself" -> {"workflow":"other","tools":[],"language":"en"}
"Read the total from the most recent supplier invoice" -> {"workflow":"general","tools":[2],"language":"en"}
"Report the latest supplier invoice every Monday at 9" -> {"workflow":"recurring","tools":[2],"language":"en"}
For a candidate numbered 3 that creates tasks in a task list:
"Put Pick up supplies in my inbox, please" -> {"workflow":"general","tools":[3],"language":"en"}
"Give me wording for a task called Pick up supplies that I'll add myself" -> {"workflow":"other","tools":[],"language":"en"}
""" + PROMPT


async def choose_connected(generate, catalog, question, history):
    requests = shortlist_requests(catalog, question, context=history[-2:])
    batches = [batch for batch, _, _ in requests] or [catalog_menu(catalog)]
    choices, workflows, language = {}, set(), None
    for batch in batches:
        index = dict(enumerate(batch, 1))
        candidates = [{"number": number, "server": batch[alias][0], "tool": batch[alias][1],
                       "purpose": batch[alias][2], "capability": batch[alias][3]} for number, alias in index.items()]
        data = {"latest_message": question, "history": history[-2:], "mcp_candidates": candidates}
        schema = {
            "type": "object", "required": ["workflow", "tools", "language"], "additionalProperties": False,
            "properties": {
                "workflow": {"enum": list(WORKFLOWS)},
                "tools": {"type": "array", "maxItems": 4, "items": {"enum": list(index)}},
                "language": {"enum": ["en", "tr"]},
            },
        }
        raw = await generate(model_messages(CHOOSE_CONNECTED, data, schema=schema), 120)
        outcome = json_object(raw if isinstance(raw, str) else "")
        names = outcome.get("tools")
        if not isinstance(names, list):
            continue
        picks = [index[number] for number in names[:4] if type(number) is int and number in index]
        if picks:
            choices.update({alias: catalog[alias] for alias in picks})
            workflow = outcome.get("workflow")
            if workflow in WORKFLOWS:
                workflows.add(workflow)
            language = outcome.get("language")
    workflow = next(iter(workflows)) if len(workflows) == 1 else "other"
    return choices, workflow, language


def shortlist_goal(generate, catalog, goal):
    requests = shortlist_requests(catalog, goal["goal"], context=goal.get("original_request"))
    if not requests:
        return catalog
    choices = {}
    for batch, data, schema in requests:
        choices.update(selected(ask(generate, SHORTLIST, data, 100, schema=schema), batch, catalog))
    return choices
