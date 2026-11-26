"""Generate Python when needed and answer from the sandbox's actual output."""

from ..store import excerpt
from .function_examples import FUNCTION_INSTRUCTIONS, TEST_RUNNER, has_examples
from .intent import FENCE
from .sandbox import Run, run_python

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


def program_from(answer):
    if not answer or not answer.strip():
        return None
    if "```" in answer:
        blocks = FENCE.findall(answer)
        return blocks[0] if len(blocks) == 1 and blocks[0].strip() else None
    return answer.strip()


def generation_messages(request, language, today=None):
    instructions = ENVIRONMENT + ANSWER_INSTRUCTIONS + FUNCTION_INSTRUCTIONS
    instructions += f" Use language code {language} for printed prose."
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


async def execute_python(request, language, generate, *, today=None):
    if request.source is not None:
        return await run_python(request.source), ""
    messages = generation_messages(request, language, today)
    result = Run("no_program"), ""
    for _ in range(3):
        authored = program_from(await generate(messages))
        if not authored:
            break
        source = authored.rstrip() + TEST_RUNNER if has_examples(authored) else authored
        run = await run_python(source)
        attachment = source if run.status in {"ok", "error", "timeout", "output_limit"} else ""
        result = run, attachment
        needs_repair = run.status == "error" or (run.status == "ok" and not run.output)
        if not needs_repair:
            break
        messages.extend((
            {"role": "assistant", "content": authored},
            {"role": "user", "content": repair_prompt(run)},
        ))
    return result
