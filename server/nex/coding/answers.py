"""Generate Python when needed and answer from the sandbox's actual output."""

import asyncio

from ..store import excerpt
from .function_examples import FUNCTION_INSTRUCTIONS, TEST_RUNNER, wants_function_tests
from .intent import ENGINE, FENCE
from .sandbox import MAX_OUTPUT, run_python

CODE_TOKENS = 600
ENVIRONMENT = (
    "Write one self-contained Python 3 program to fulfill the user's request. Return only the program "
    "in a single python code block. It will run in a fresh offline directory with the standard library, "
    "no additional packages, no network, and no interactive input. "
)
ANSWER_INSTRUCTIONS = (
    "Compute the result from the supplied data using Python operations. Print the answer, including units if "
    "relevant. If writing a function, print a demonstration. Carry forward supplied values when the latest "
    "request changes one of them. Never substitute example numbers for missing inputs. In that case print a "
    "specific question asking for the missing value. Use simulated records only if asked for a simulation or "
    "example, and label them as simulated. Never claim to have checked an account or created a reminder."
)
STATUS_TEXT = {
    "busy": "The Python sandbox is busy. Try again in a moment.",
    "unavailable": "The isolated Python runtime is unavailable on Excalibur.",
    "source_limit": "This program exceeds the Python sandbox’s 12,000-byte source limit.",
    "timeout": "Python stopped at the execution time limit.",
    "output_limit": f"Python stopped after {MAX_OUTPUT:,} bytes of output.",
}


def program_from(answer):
    if not answer or not answer.strip():
        return None
    if "```" in answer:
        blocks = FENCE.findall(answer)
        return blocks[0] if len(blocks) == 1 and blocks[0].strip() else None
    return answer.strip()


def generation_messages(request, language, today=None, *, test_function=None):
    if test_function is None:
        test_function = wants_function_tests(request.task)
    if test_function:
        instructions = FUNCTION_INSTRUCTIONS
        if language != "en":
            instructions += f" Use language code {language} for user-facing prose."
    else:
        instructions = ENVIRONMENT + ANSWER_INSTRUCTIONS + f" Use language code {language} for printed prose."
        if today:
            instructions += f" For relative dates, today in the user's time zone is {today}."
    messages = [{"role": "system", "content": instructions}]
    if request.previous:
        question, answer = request.previous
        if not answer:
            messages.append({"role": "user", "content": "Earlier input:\n" + excerpt(question, 1400)
                             + "\n\nLatest request:\n" + request.task})
            return messages
        messages.append({"role": "user", "content": excerpt(question, 1400)})
        messages.append({"role": "assistant", "content": excerpt(answer, 3200)})
    messages.append({"role": "user", "content": request.task})
    return messages


def result_text(run):
    if run.status == "ok":
        return run.output or "Python finished without printing any output."
    status = STATUS_TEXT.get(run.status) or f"Python exited with status {run.exit_code}."
    return status + ("\n\n" + run.output if run.output else "")


def repair_prompt(run):
    instructions = "Correct the program for the original request and print the result."
    if run.status == "error" and "EOFError: EOF when reading a line" in run.output:
        instructions += (" The sandbox has no stdin. Replace interactive prompts with direct calls using your "
                         "own test cases for a function, or print a clarification for a missing calculation value.")
    elif run.status == "error" and "AssertionError" in run.output:
        instructions += (" A test failed. Work out the correct answer for that exact input from the original "
                         "request. Check both the implementation and the expected value. Keep the failing case "
                         "and its assertion, correct the mistake, and rerun the checks.")
    diagnostic = run.output[-1000:] or "The program printed nothing."
    return instructions + "\n\nSandbox diagnostic:\n" + diagnostic


def reply(text, language, emit, speech=None):
    if emit:
        emit(speech or text, language)
    return {"text": text, "language": language, "engine": ENGINE, "sources": []}


async def generate_and_run(request, language, generate, today):
    test_function = wants_function_tests(request.task)
    messages = generation_messages(request, language, today, test_function=test_function)
    result = None
    for _ in range(2):
        authored = program_from(await generate(messages))
        if not authored:
            break
        source = authored.rstrip() + TEST_RUNNER if test_function else authored
        run = await run_python(source)
        result = source, run
        needs_repair = run.status == "error" or (run.status == "ok" and not run.output)
        if not needs_repair:
            break
        messages.extend((
            {"role": "assistant", "content": authored},
            {"role": "user", "content": repair_prompt(run)},
        ))
    return result


async def python_reply(request, language, generate, emit=None, *, today=None):
    if request.source is None:
        result = await generate_and_run(request, language, generate, today)
        if result is None:
            return reply("I couldn’t get a Python program from the local model. You can also paste a script "
                         "starting with “Run Python:”.", language, emit)
        source, run = result
    else:
        source = request.source
        run = await run_python(source)
    text = result_text(run)
    speech = text if run.status == "ok" else text.split("\n", 1)[0]
    if len(speech) > 240:
        speech = excerpt(speech, 200) + " The output is in Chat."
    if request.source is None and run.status in {"ok", "error", "timeout", "output_limit"}:
        text += "\n\n```python\n" + source.rstrip() + "\n```"
    return reply(text, language, emit, speech)


def chat_python(request, language, generate, emit, *, today=None):
    # The archive HTTP server and recorded voice workers are synchronous. Each
    # owns its thread while live voice awaits the same cancellable runner directly.
    async def complete(messages):
        return generate(messages, CODE_TOKENS)

    return asyncio.run(python_reply(request, language, complete, emit, today=today))
