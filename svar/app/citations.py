"""The source numbers an answer cites, and the answer without them."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

#: The three layers a document can come from, in the order they are trusted.
LAYERS = ("human", "agent", "inbox")

_DASH = "-–—"
_PART = rf"\d+(?:\s*[{_DASH}]\s*\d+)?"
_CITATION = re.compile(rf"\[\s*({_PART}(?:\s*[,;]\s*{_PART})*)\s*\]")
_RANGE = re.compile(rf"(\d+)\s*[{_DASH}]\s*(\d+)")
_CITATION_WITH_SPACE = re.compile(rf"\s*{_CITATION.pattern}")


def without_citations(answer: str) -> str:
    """The answer without its source numbers and the spaces before them: "Warned [1][2][4]"
    and "Warned [1] [2] [4]" become "Warned", "5 years [1, 4]." becomes "5 years."."""
    return _CITATION_WITH_SPACE.sub("", answer).strip()


def cited_numbers(answer: str, number_of_sources: int) -> set[int]:
    """The source numbers an answer cites."""
    numbers: set[int] = set()
    for match in _CITATION.finditer(answer):
        for part in re.split(r"[,;]", match.group(1)):
            span = _RANGE.fullmatch(part.strip())
            if span:
                first, last = int(span.group(1)), int(span.group(2))
                numbers.update(range(max(first, 1), min(last, number_of_sources) + 1))
            else:
                numbers.add(int(part))
    return {number for number in numbers if 1 <= number <= number_of_sources}


def cited_layers(sources: Sequence[Mapping[str, Any]]) -> list[str]:
    """The layers of the cited sources, most trusted first."""
    layers = {source["layer"] for source in sources if source["cited"]}
    known = [layer for layer in LAYERS if layer in layers]
    return known + sorted(layers - set(LAYERS))
