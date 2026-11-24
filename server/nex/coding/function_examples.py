"""Ask the model for executable examples and check them with Python's doctest."""

import ast
import doctest

FUNCTION_INSTRUCTIONS = (
    " For requests to implement reusable functions, include two short doctest examples in the function's "
    "docstring when appropriate. Follow explicit requests to omit tests or examples. "
    "Each example has a >>> function call followed on the next line by its expected Python output. "
    "Use simple examples in the requested domain. For a predicate show both a True and a False case. "
    "Nex runs supplied doctests. When you include them, omit input(), a main block, and a test runner."
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


def has_examples(source):
    try:
        tree = ast.parse(source)
        parser = doctest.DocTestParser()
        return any(parser.get_examples(ast.get_docstring(node) or "") for node in ast.walk(tree)
                   if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    except (SyntaxError, ValueError):
        return False
