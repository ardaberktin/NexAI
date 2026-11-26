"""Ask the model for executable examples and check them with Python's doctest."""

import re

FUNCTION_TASK = re.compile(
    r"\b(?:write|define|implement|create|build|make|rewrite|refactor|debug|fix|test|code)\b"
    r"[^.!?\n]{0,96}?\b(?:functions?|methods?|predicates?)\b|"
    r"\b(?:want|need|would like|give me|show me) (?:an? |the |some )?(?:[\w-]+ ){0,2}"
    r"(?:function|method|predicate)\b", re.IGNORECASE,
)
SKIP_TESTS = re.compile(
    r"\b(?:without|skip|omit|no) (?:any |the )?(?:tests?|testing|examples?)\b|"
    r"\b(?:do not|don['’]t) (?:test\b|(?:include|write|add|run) (?:any )?(?:tests?|examples?)\b)",
    re.IGNORECASE,
)
FUNCTION_INSTRUCTIONS = (
    "Write the requested Python 3 function in one python code block. The sandbox has only the "
    "standard library and no stdin. Include two short doctest examples in the function's docstring. "
    "Each example has a >>> function call followed on the next line by its expected Python output. "
    "Choose your own simple inputs from the domain the user specified. For a predicate show both "
    "a True and a False case. Nex runs the examples with doctest. Return only the imports and "
    "the function; omit input(), a main block, and a test runner. Satisfy additional user requirements."
)

# This runs in the guest alongside the generated function. Chat returns the
# same runnable source, so its result can be reproduced without Nex.
TEST_RUNNER = '''

if __name__ == "__main__":
    import doctest

    failed, attempted = doctest.testmod()
    if failed:
        raise AssertionError(f"{failed} of {attempted} function tests failed.")
    if not attempted:
        raise AssertionError("No function tests ran. Add executable >>> examples to the docstring.")
    print(f"{attempted} function test{'s' if attempted != 1 else ''} passed.")
'''


def wants_function_tests(task):
    return bool(FUNCTION_TASK.search(task) and not SKIP_TESTS.search(task))
