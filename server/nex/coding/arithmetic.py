"""Turn complete, everyday arithmetic expressions into short Python programs."""

import re

QUESTION = re.compile(r"^(?:what(?:['’]s| is)|how much is|calculate|compute|evaluate)\s+", re.IGNORECASE)
NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
PERCENT_OF = re.compile(rf"({NUMBER})\s*(?:%|percent|per cent)\s*of\s*", re.IGNORECASE)
OPERATORS = {
    "to the power of": "**", "multiplied by": "*", "divided by": "/", "times": "*",
    "plus": "+", "minus": "-", "over": "/", "modulo": "%",
}
SPOKEN = re.compile(r"\b(?:" + "|".join(OPERATORS) + r")\b")
SYMBOLS = str.maketrans({"×": "*", "÷": "/", "−": "-", "^": "**"})
SMALL_NAMES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
               "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
SMALL = {word: number for number, word in enumerate(SMALL_NAMES)}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
NUMERAL = re.compile(r"\b(" + "|".join(TENS) + r")(?:[ -](" + "|".join(SMALL_NAMES[1:10]) + r"))?\b|"
                     r"\b(" + "|".join(SMALL) + r")\b")


def digits(match):
    decade, unit, small = match.groups()
    return str(SMALL[small] if small else TENS[decade] + SMALL.get(unit, 0))


def arithmetic_source(text):
    expression = QUESTION.sub("", text.strip().lower()).rstrip("?! .")
    if not expression or "\n" in expression:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expression):
        return None
    expression = NUMERAL.sub(digits, expression)
    expression = re.sub(r"(?<=\d) point (\d(?: +\d\b)*)\b",
                        lambda match: "." + match[1].replace(" ", ""), expression)
    expression = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expression)
    expression = PERCENT_OF.sub(r"(\1/100)*", expression)
    expression = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", expression)
    expression = SPOKEN.sub(lambda match: OPERATORS[match[0]], expression).translate(SYMBOLS)
    if (not re.fullmatch(r"[0-9.()+*/%\s-]+", expression) or not re.search(r"\d", expression)
            or not re.search(r"[+*/%-]", expression)):
        return None
    # Formatting applies only to natural-language arithmetic. Pasted Python keeps
    # its own output formatting and integer results keep every digit.
    return f'answer = ({expression})\nprint(format(answer, ".15g") if isinstance(answer, float) else answer)'
