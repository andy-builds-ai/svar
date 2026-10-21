"""Vard's first responsibility: checking a delivery note against its purchase order.

Vard reads both, compares them position by position and reports to a person, who decides; he
books nothing, and there is no tool to book with. Where the documents come from is handed in:
`scripts/receiving_probe.py` reads them from its case file, a later Vard from the business's
own system.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from vard.engine import Level, Responsibility, Tool

#: The kinds of discrepancy `report_discrepancies` takes.
KINDS = ("wrong_item", "wrong_quantity", "missing_item", "extra_item", "wrong_unit")

#: The tools that close a check. The loop ends after the reply that called one; a check is
#: closed rightly when exactly one of them ran.
CLOSING = ("report_discrepancies", "report_match", "hand_off")

#: Fetching the note and the order and reporting take three steps, a rest delivery a fourth for
#: the earlier deliveries; the rest is room for a model that fetches one thing per step or tries
#: again after a refusal.
MAX_STEPS = 8

TASK = """You check incoming deliveries for {company}.
When a delivery note comes in, fetch it with get_delivery_note, then fetch the purchase order it
names with get_purchase_order, and compare them position by position: article, quantity, unit.
Quantities are written the German way: a point groups thousands and a comma marks decimals, so
"1.250,50" is one thousand two hundred and fifty and a half. A quantity in another unit is no
discrepancy when the pack size on the note makes it the same amount. A delivery marked as
partial is still compared with the whole order, so that what is still open is reported. A
delivery marked as the rest of an earlier one is compared with what is still open of its order
after the earlier deliveries; fetch them with get_earlier_deliveries. A note may name the
supplier's article number; the order lists it beside ours. An article sent as a successor or
replacement of the ordered one is a wrong_item: a person decides whether to keep it.
Then close the check with exactly one tool:
- report_discrepancies, with every discrepancy and nothing else;
- report_match, when everything agrees;
- hand_off, when you cannot compare, for example because the note names no purchase order or
  the order cannot be found.
You do not book anything; a person decides what happens with the delivery. Write in German."""

#: Finds a document by its number; None when there is none.
Find = Callable[[str], Mapping[str, Any] | None]

#: Finds the delivery notes that came in earlier for a purchase order; none, when none did.
FindEarlier = Callable[[str], Sequence[Mapping[str, Any]]]


def text_input(**fields: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {"type": "string", "description": text} for name, text in fields.items()
        },
        "required": list(fields),
    }


NOTE_NUMBER = "The number of the delivery note, e.g. LS-12345."

FINDINGS = {
    "type": "object",
    "properties": {
        "note_number": {"type": "string", "description": NOTE_NUMBER},
        "findings": {
            "type": "array",
            "description": "One entry per discrepancy.",
            "items": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "The article number from the purchase order; for an "
                        "extra position, the one from the delivery note.",
                    },
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "ordered": {
                        "type": "string",
                        "description": "Article, quantity and unit as ordered; empty for an "
                        "extra position.",
                    },
                    "delivered": {
                        "type": "string",
                        "description": "Article, quantity and unit as delivered; empty for a "
                        "missing position.",
                    },
                },
                "required": ["item", "kind", "ordered", "delivered"],
            },
        },
    },
    "required": ["note_number", "findings"],
}


def lookup(find: Find, key: str, what: str) -> Callable[[dict[str, Any]], Any]:
    def run(given: dict[str, Any]) -> Any:
        number = str(given[key])
        found = find(number)
        return dict(found) if found is not None else {"error": f"There is no {what} {number}."}

    return run


def earlier(find: FindEarlier) -> Callable[[dict[str, Any]], Any]:
    def run(given: dict[str, Any]) -> Any:
        number = str(given["order_number"])
        return {"order_number": number, "deliveries": [dict(note) for note in find(number)]}

    return run


def recorded(given: dict[str, Any]) -> dict[str, bool]:
    return {"recorded": True}


def responsibility(
    company: str, find_order: Find, find_note: Find, find_earlier: FindEarlier
) -> Responsibility:
    """The receiving check for one business, with its own way of finding its documents."""
    allowed = Level.ALLOWED
    return Responsibility(
        task=TASK.format(company=company),
        tools=(
            Tool(
                "get_delivery_note",
                "Fetch a delivery note that has come in, with its positions.",
                text_input(note_number=NOTE_NUMBER),
                allowed,
                lookup(find_note, "note_number", "delivery note"),
            ),
            Tool(
                "get_purchase_order",
                "Fetch one of our purchase orders, with its positions.",
                text_input(order_number="The number of the purchase order."),
                allowed,
                lookup(find_order, "order_number", "purchase order"),
            ),
            Tool(
                "get_earlier_deliveries",
                "Fetch the delivery notes that came in earlier for one of our purchase orders.",
                text_input(order_number="The number of the purchase order."),
                allowed,
                earlier(find_earlier),
            ),
            Tool(
                "report_discrepancies",
                "Report every discrepancy between the delivery note and its purchase order "
                "to a person.",
                FINDINGS,
                allowed,
                recorded,
                closes=True,
            ),
            Tool(
                "report_match",
                "Report to a person that the delivery note agrees with its purchase order.",
                text_input(note_number=NOTE_NUMBER),
                allowed,
                recorded,
                closes=True,
            ),
            Tool(
                "hand_off",
                "Hand the delivery note to a person, because it cannot be checked.",
                text_input(note_number=NOTE_NUMBER, reason="Why it cannot be checked."),
                allowed,
                recorded,
                closes=True,
            ),
        ),
        max_steps=MAX_STEPS,
    )
