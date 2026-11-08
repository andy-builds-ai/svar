"""The plans a key can carry, and what each allows.

This table is the one place a plan is defined. The answer function reads it to
count a question (app/budget.py), `python -m scripts.workspace` to give a key
its plan. Changing a number here and deploying changes it for every key on that
plan, from its next question. A new plan is deployed before a key is put on
it, and a plan keys carry is never renamed or removed: the function refuses
a key whose plan it does not know, with a 500, until the two agree again.

A key whose budget item names no plan is a demo key: ``QUESTION_LIMIT``
questions within ``WINDOW_HOURS`` of the first, set by the stack.
"""

from typing import NamedTuple


class Plan(NamedTuple):
    name: str
    #: Questions per calendar month, German time; the count starts over on the 1st.
    questions_per_month: int
    #: Questions per calendar day, German time; the count starts over at midnight.
    questions_per_day: int


PLANS = (
    Plan("betrieb", questions_per_month=6_000, questions_per_day=600),
    Plan("firma", questions_per_month=15_000, questions_per_day=1_500),
)

#: What `python -m scripts.workspace` calls a key without a plan.
DEMO = "demo"


def plan_named(name: object) -> Plan | None:
    """The plan of this name, or None when there is none."""
    return next((plan for plan in PLANS if plan.name == name), None)
