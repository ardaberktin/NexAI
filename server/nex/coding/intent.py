"""Identify computations requested by the speaker, including pasted Python."""

import re
from dataclasses import dataclass
from textwrap import dedent

from ..accounts.intent import account_intent
from ..conversation_intent import SCREEN_REFERENCE
from ..goal_requests import goal_request
from ..wake import LEADING_WAKE
from ..web_intent import is_web_request
from .arithmetic import arithmetic_source

ENGINE = "python_sandbox"
POLITE = re.compile(r"^(?:(?:please|(?:can|could|would|will) you|i(?: want| need|['’]d like) you to)\s+)+",
                    re.IGNORECASE)
RAW = re.compile(r"^(?:(?:run|execute)\s+(?:(?:this|the following)\s+)?(?:in\s+)?"
                 r"(?:python(?:\s+3)?(?:\s+(?:code|script|program|snippet))?|(?:code|script|snippet)(?: in python)?)"
                 r"|python(?:\s+3)?)\s*(?::[ \t]*|\n)", re.IGNORECASE)
FENCE = re.compile(r"`{3}(?:python3?|py)?[ \t]*\r?\n(.*?)\r?\n[ \t]*`{3}", re.IGNORECASE | re.DOTALL)
INLINE = re.compile(r"(?:run|execute)\s+`([^`\n]+)`(?: in python)?[.!?]?", re.IGNORECASE)
RUN_FENCE = re.compile(r"^(?:run|execute|evaluate|what (?:does|will) this (?:print|output|return))\b", re.IGNORECASE)
GENERATE = re.compile(
    r"^(?:(?:use|using|with|in|run|execute) python\b|(?:calculate|compute|simulate)\b|"
    r"(?:write|create|make|implement|code|debug|fix|test|show|give|i(?: want| need|['’]d like))(?: me)? "
    r"(?:(?:a|an|some|the|this|and run|short|simple|small) )*(?:python\b|script\b|code\b|function\b|program\b))|"
    r"\b(?:using|in|with) python(?: 3)?[.!?]*$", re.IGNORECASE,
)
MATH_TASK = re.compile(
    r"^(?:(?:what(?:['’]s| is)|find|give me|work out|determine) (?:the )?"
    r"(?:sum|product|mean|median|average|standard deviation|variance|factorial|square root|"
    r"greatest common (?:factor|divisor)|least common multiple) (?:of|for)\b|"
    r"(?:sort|sum|average|multiply|divide|add|subtract)\b.*\d|"
    r"(?:generate|give me|list|find|what are|show me) (?:the )?(?:first |next )?\d+ (?:primes?|fibonacci)\b|"
    r"(?:count|sort|reverse) (?:the |these |the following )?(?:letters|characters|words|numbers|values)\b|"
    r"(?:what(?:['’]s| is)|how much is) .+ (?:plus|minus|times|divided by) .+)", re.IGNORECASE,
)
WORD_PROBLEM = re.compile(r"^(?:if|suppose)\b.*\bhow (?:many|much|long|far)\b", re.IGNORECASE)
FOLLOWUP = re.compile(
    r"^(?:(?:now |and )?(?:run (?:it|that) again|(?:try|do|repeat|rerun) (?:it|that)"
    r"(?: with| for| again)?|(?:double|triple|halve|multiply|divide) (?:it|that))\b|"
    r"(?:what|how) about (?:[-\d]|using\b)|(?:now )?use .+ instead\b)", re.IGNORECASE,
)
NO_EXECUTION = re.compile(
    r"\b(?:(?:do not|don['’]t|never) (?:run|execute|(?:use|write) (?:any )?(?:python|code|scripts?|tools?))|"
    r"without (?:running|executing|(?:using |writing )?(?:any )?(?:python|code|scripts?|tools?))|"
    r"no (?:python|code execution|tools)|(?:do (?:it|this|that) |work it out )mentally)\b", re.IGNORECASE,
)
REFERENCE = re.compile(r"\b(?:previous|same|that (?:result|code|number|function|script)|it again)\b", re.IGNORECASE)
DISCUSS = re.compile(r"^(?:explain|review|describe|tell me about|why|how (?:do|does|can)|"
                     r"what (?:is|are) (?:a|an|the difference))\b", re.IGNORECASE)


@dataclass(frozen=True)
class PythonRequest:
    task: str
    source: str | None = None
    previous: tuple[str, str] | None = None


def pasted_python(text):
    value = POLITE.sub("", LEADING_WAKE.sub("", text.strip(), count=1))
    if raw := RAW.match(value):
        body = value[raw.end():].strip("\r\n")
        if not body.lstrip().startswith("```"):
            if inline := re.fullmatch(r"`([^`\n]+)`", body.strip()):
                body = inline[1]
            return PythonRequest(text, dedent(body))
    if inline := INLINE.fullmatch(value):
        return PythonRequest(text, inline[1])
    if fenced := FENCE.search(value):
        outside = (value[:fenced.start()] + " " + value[fenced.end():]).strip()
        if not NO_EXECUTION.search(outside) and (not outside or raw or RUN_FENCE.match(POLITE.sub("", outside))):
            return PythonRequest(text, dedent(fenced[1]))
    return None


def python_request(text, history=()):
    if pasted := pasted_python(text):
        return pasted
    value = POLITE.sub("", LEADING_WAKE.sub("", text.strip(), count=1))
    header = FENCE.sub(" ", value).strip()
    if (NO_EXECUTION.search(header) or is_web_request(value) or DISCUSS.match(header)
            or SCREEN_REFERENCE.search(header) or account_intent(header) or goal_request(text)):
        return None
    if source := arithmetic_source(value):
        return PythonRequest(text, source)
    previous = None
    followup = FOLLOWUP.match(header)
    if ((followup or REFERENCE.search(header)) and len(history) >= 2
            and history[-2]["kind"] == "question" and history[-1]["kind"] == "answer"
            and history[-1].get("engine") == ENGINE):
        previous = (history[-2]["text"], history[-1]["text"])
    word_problem = WORD_PROBLEM.search(header) and len(re.findall(r"\d+(?:\.\d+)?", header)) >= 2
    if GENERATE.search(header) or MATH_TASK.search(header) or word_problem or (previous and followup):
        return PythonRequest(text, previous=previous)
    return None
