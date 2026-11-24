"""Measured page flow for reports and two-column newspapers."""

from collections import deque
from dataclasses import dataclass, replace

PAGE_WIDTH, PAGE_HEIGHT = 612, 792
MARGIN, GUTTER, BODY_BOTTOM = 46, 30, 60
MAX_PAGES = 16


@dataclass(frozen=True)
class Style:
    font: str
    size: int
    height: int
    color: str


MUTED = Style("NexSans", 9, 14, "#535e63")
BODY = Style("NexSans", 11, 16, "#202629")
HEADLINE = Style("NexBold", 15, 21, "#223b37")
REPORT_TITLE = Style("NexBold", 31, 39, "#223b37")
MASTHEAD = Style("NexBold", 38, 49, "#202629")


@dataclass(frozen=True)
class Line:
    text: str
    style: Style
    before: int = 0

    @property
    def height(self):
        return self.before + self.style.height


@dataclass(frozen=True)
class Placement:
    line: Line
    x: float
    top: float
    width: float

    @property
    def baseline(self):
        return self.top - self.line.style.size

    @property
    def bounds(self):
        return self.x - 2, self.top - self.line.style.height, self.x + self.width + 2, self.top


def wrap(text, style, width, measure):
    words = deque(text.split())
    lines = []
    line = ""
    while words:
        word = words.popleft()
        joined = line + " " + word if line else word
        if measure(joined, style) <= width:
            line = joined
        elif line:
            lines.append(line)
            line = ""
            words.appendleft(word)
        else:
            # Keep every character of an oversized token, such as a URL.
            end = 1
            while end < len(word) and measure(word[:end + 1], style) <= width:
                end += 1
            lines.append(word[:end])
            words.appendleft(word[end:])
    return [Line(line, style) for line in (lines + [line]) if line]


@dataclass(frozen=True)
class Group:
    lines: tuple[Line, ...]
    starts_section: bool = False

    @property
    def height(self):
        return sum(line.height for line in self.lines)


def body_groups(sections, width, measure):
    for index, section in enumerate(sections):
        heading = wrap(section.heading, HEADLINE, width, measure)
        for number, paragraph in enumerate(section.paragraphs):
            lines = wrap(paragraph, BODY, width, measure)
            if heading and number == 0:
                heading[0] = replace(heading[0], before=15 if index else 0)
                yield Group((*heading, *lines[:2]), starts_section=True)
                lines = lines[2:]
            else:
                if number or index:
                    lines[0] = replace(lines[0], before=7)
                yield Group((lines.pop(0),), starts_section=number == 0)
            yield from (Group((line,)) for line in lines)


def column_target(groups, capacity):
    height = sum(group.height for group in groups)
    if height > capacity * 1.8:
        return capacity
    midpoint = height / 2
    best, offset = midpoint, 0
    candidates = []
    for group in groups:
        if group.starts_section and 0 < offset <= capacity and height - offset <= capacity:
            candidates.append(offset)
        offset += group.height
    if candidates:
        boundary = min(candidates, key=lambda point: abs(point - midpoint))
        if abs(boundary - midpoint) <= max(45, height / 5):
            best = boundary
    return min(capacity, best)


def place(lines, x, top, width, placements):
    for line in lines:
        top -= line.before
        placements.append(Placement(line, x, top, width))
        top -= line.style.height
    return top


def pages(document, measure):
    newspaper = document.layout == "newspaper"
    page_width = PAGE_WIDTH - 2 * MARGIN
    width = (page_width - GUTTER) / 2 if newspaper else page_width
    groups = deque(body_groups(document.sections, width, measure))
    page_number = 0
    while groups:
        page_number += 1
        placements = []
        top = PAGE_HEIGHT - MARGIN
        if page_number == 1:
            style = MASTHEAD if newspaper else REPORT_TITLE
            top = place(wrap(document.title, style, page_width, measure), MARGIN, top, page_width, placements)
            top = place(wrap(document.subtitle, MUTED, page_width, measure), MARGIN, top - 3, page_width, placements)
        rule = top - 6
        top -= 22
        capacity = top - BODY_BOTTOM
        # Keep short stories intact when a section break can balance the last spread.
        target = column_target(groups, capacity) if newspaper else capacity
        column_bottoms = []
        for column in range(2 if newspaper else 1):
            y = top
            x = MARGIN + column * (width + GUTTER)
            while groups:
                group = groups[0]
                lines = group.lines
                if (y - group.height < BODY_BOTTOM or
                        (column == 0 and newspaper and y != top and top - y >= target)):
                    break
                if y == top:
                    lines = (replace(lines[0], before=0), *lines[1:])
                groups.popleft()
                y = place(lines, x, y, width, placements)
            column_bottoms.append(y)
        place([Line(str(page_number), MUTED)], MARGIN, 39, page_width, placements)
        divider = min(column_bottoms) if newspaper and all(y < top for y in column_bottoms) else None
        yield placements, rule, divider
