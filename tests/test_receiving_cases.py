"""The receiving cases: the file agrees with itself, and every expected finding follows from the
documents of its case."""

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts import receiving_probe as probe
from vard import receiving

#: The cases of the first build; they carry no group and no recipient, and stay as they were.
FIRST = {f"{firm}{n}" for firm in "pk" for n in range(1, 7)}

GROUPS = ("long", "rest", "supplier_number", "other_company")


@pytest.fixture(scope="module")
def loaded() -> tuple[dict[str, str], list[dict[str, Any]]]:
    return probe.load_cases(probe.CASES_FILE)


@pytest.fixture(scope="module")
def cases(loaded: tuple[dict[str, str], list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in loaded[1]}


def test_24_cases_split_between_two_businesses(cases: dict[str, dict[str, Any]]) -> None:
    companies = Counter(case["company"] for case in cases.values())
    assert (len(cases), companies["pellwick"], companies["kalvenstett"]) == (24, 12, 12)


def test_the_new_cases_are_three_of_each_group(cases: dict[str, dict[str, Any]]) -> None:
    new = [case for case in cases.values() if case["id"] not in FIRST]
    assert {case["id"] for case in cases.values() if "group" not in case} == FIRST
    assert Counter(case["group"] for case in new) == dict.fromkeys(GROUPS, 3)
    assert Counter(case["company"] for case in new) == {"pellwick": 6, "kalvenstett": 6}


def test_every_kind_and_every_close_is_asked_for(cases: dict[str, dict[str, Any]]) -> None:
    kinds = {kind for case in cases.values() for f in case["findings"] for kind in f["kinds"]}
    assert kinds == set(receiving.KINDS)
    assert {case["expect"] for case in cases.values()} == set(receiving.CLOSING)


def german(text: str) -> Decimal:
    """A quantity as written on the paper: a point groups thousands, a comma marks decimals."""
    return Decimal(text.replace(".", "").replace(",", "."))


def amount(position: dict[str, Any]) -> tuple[Decimal, str]:
    """The quantity in the unit it is counted in; a pack like "Karton à 200 Stück" opened."""
    quantity, unit = german(position["quantity"]), position["unit"]
    pack = re.fullmatch(r".+ à ([\d.,]+) (.+)", unit)
    return (quantity * german(pack[1]), pack[2]) if pack else (quantity, unit)


def derived(case: dict[str, Any]) -> set[tuple[str, str]]:
    """The discrepancies the documents show, worked out without a model: what is still open of
    the order after the earlier deliveries, against the note, by our number or the supplier's."""
    (order,) = case["purchase_orders"]
    ours = {
        probe.number(p[key]): p["item"]
        for p in order["positions"]
        for key in ("item", "supplier_item")
        if p.get(key)
    }
    still_open = {p["item"]: amount(p) for p in order["positions"]}
    for note in case.get("earlier_deliveries", []):
        for p in note["positions"]:
            item = ours[probe.number(p["item"])]
            (left, unit), (came, came_unit) = still_open[item], amount(p)
            assert came_unit == unit, case["id"]
            still_open[item] = (left - came, unit)
    found, delivered = set(), {}
    for p in case["delivery_note"]["positions"]:
        item = ours.get(probe.number(p["item"]))
        if item is None:
            found.add((probe.number(p["item"]), "extra_item"))
            continue
        assert item not in delivered, case["id"]
        delivered[item] = amount(p)
    for item, (left, unit) in still_open.items():
        if item not in delivered:
            if left:
                found.add((probe.number(item), "missing_item"))
        elif delivered[item][1] != unit:
            found.add((probe.number(item), "wrong_unit"))
        elif delivered[item][0] != left:
            found.add((probe.number(item), "wrong_quantity"))
    return found


def test_every_expected_finding_follows_from_the_documents(
    cases: dict[str, dict[str, Any]],
) -> None:
    """Each expected finding accounts for exactly what the documents show, and nothing is left:
    a wrong article for its ordered half missing and its delivered half extra, any other for
    one discrepancy of one of its kinds. A case to hand off has no order to work it out from."""
    for name, case in cases.items():
        if case["expect"] == "hand_off":
            continue
        shown = derived(case)
        covered = set()
        for f in case["findings"]:
            mine = {probe.number(item) for item in f["items"]}
            if f["kinds"] == ["wrong_item"]:
                halves = {d for d in shown if d[0] in mine}
                assert sorted(kind for _, kind in halves) == ["extra_item", "missing_item"], name
            else:
                halves = {d for d in shown if d[0] in mine and d[1] in f["kinds"]}
                assert len(halves) == 1, (name, f)
            covered |= halves
        assert covered == shown, (name, shown - covered)


def test_the_new_notes_name_their_recipient(
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    """A note is addressed to one of the two businesses; the ones of the other business lie at
    the wrong one, which has no such order, and go to a person."""
    companies, cases = loaded
    names = {key: text.split(",")[0] for key, text in companies.items()}
    for case in cases:
        notes = [case["delivery_note"], *case.get("earlier_deliveries", [])]
        if case["id"] in FIRST:
            assert all("recipient" not in note for note in notes), case["id"]
            continue
        own = names[case["company"]]
        other = case["group"] == "other_company"
        for note in notes:
            assert note["recipient"] in names.values(), case["id"]
            assert (note["recipient"] != own) == other, case["id"]
        if other:
            assert (case["purchase_orders"], case["expect"]) == ([], "hand_off"), case["id"]


def test_the_note_to_hand_off_names_no_order_or_one_of_another_business(
    cases: dict[str, dict[str, Any]],
) -> None:
    for case in cases.values():
        named = case["delivery_note"]["order_number"]
        foreign = case.get("group") == "other_company"
        assert (case["expect"] == "hand_off") == (named is None or foreign), case["id"]
        if named is not None and not foreign:
            assert [o["order_number"] for o in case["purchase_orders"]] == [named], case["id"]


def test_a_rest_delivery_names_the_note_before_it(cases: dict[str, dict[str, Any]]) -> None:
    for case in cases.values():
        earlier = case.get("earlier_deliveries", [])
        assert bool(earlier) == (case.get("group") == "rest"), case["id"]
        if earlier:
            last = earlier[-1]["note_number"]
            remark = case["delivery_note"]["remark"]
            assert remark == f"Restlieferung zu Lieferschein {last}", case["id"]
            assert all(note["date"] < case["delivery_note"]["date"] for note in earlier)


def test_the_supplier_numbers_stand_beside_ours_only_in_their_group(
    cases: dict[str, dict[str, Any]],
) -> None:
    for case in cases.values():
        given = any(
            "supplier_item" in p for order in case["purchase_orders"] for p in order["positions"]
        )
        assert given == (case.get("group") == "supplier_number"), case["id"]


def test_the_earlier_notes_are_fetched_by_order_and_by_number(
    loaded: tuple[dict[str, str], list[dict[str, Any]]], cases: dict[str, dict[str, Any]]
) -> None:
    companies = loaded[0]
    case = cases["p8"]
    tools = {t.name: t for t in probe.case_responsibility(companies, case).tools}
    fetched = tools["get_earlier_deliveries"].run({"order_number": " pw-2026-0297"})
    assert [n["note_number"] for n in fetched["deliveries"]] == ["LS-HT-58016", "LS-HT-58133"]
    assert tools["get_earlier_deliveries"].run({"order_number": "KH-26-0558"}) == {
        "order_number": "KH-26-0558",
        "deliveries": [],
    }
    assert tools["get_delivery_note"].run({"note_number": "LS-HT-58133"})["remark"]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"id": "p1"}, "share an id"),
        ({"company": "quillmoor"}, "unknown company"),
        ({"message": "Ein Lieferschein ist eingegangen."}, "name the delivery note"),
        ({"expect": "book"}, "expect is one of"),
        ({"findings": [{"items": ["X"], "kinds": ["late"]}]}, "lists its article numbers"),
        ({"findings": [{"items": ["X"], "kinds": ["wrong_item"]}]}, "only there"),
    ],
)
def test_a_broken_case_is_refused(
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
    change: dict[str, Any],
    message: str,
) -> None:
    companies, cases = loaded
    broken = [cases[0], {**cases[1], **change}] if "id" in change else [{**cases[0], **change}]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"companies": companies, "cases": broken}), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        probe.load_cases(path)


def rest(data: dict[str, Any]) -> dict[str, Any]:
    return next(case for case in data["cases"] if case["id"] == "p8")


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda data: data.update(cases=[]), "no cases"),
        (lambda data: data["cases"][0]["purchase_orders"][0].pop("order_number"), "its number"),
        (lambda data: data["cases"][0]["delivery_note"].pop("order_number"), "its number"),
        (lambda data: data["cases"][4]["delivery_note"]["positions"][0].pop("item"), "its item"),
        (lambda data: data["cases"][4]["purchase_orders"][0]["positions"].append("x"), "its item"),
        (
            lambda data: rest(data)["earlier_deliveries"][0].update(order_number="PW-2026-0001"),
            "same purchase order",
        ),
        (
            lambda data: rest(data)["earlier_deliveries"][1].update(note_number="LS-HT-58263"),
            "share a number",
        ),
        (lambda data: rest(data)["earlier_deliveries"][0].pop("note_number"), "its number"),
        (
            lambda data: rest(data)["earlier_deliveries"][0]["positions"][0].pop("item"),
            "its item",
        ),
        (
            lambda data: data["cases"][18]["purchase_orders"][0]["positions"][0].update(
                supplier_item=7
            ),
            "its item",
        ),
    ],
    ids=[
        "empty",
        "order-without-number",
        "note-without-order-field",
        "position-without-item",
        "position-no-object",
        "earlier-of-another-order",
        "earlier-with-the-note's-number",
        "earlier-without-number",
        "earlier-position-without-item",
        "supplier-number-no-text",
    ],
)
def test_a_file_that_would_fail_after_paying_is_refused_before(
    tmp_path: Path, change: Any, message: str
) -> None:
    data = json.loads(probe.CASES_FILE.read_text(encoding="utf-8"))
    change(data)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        probe.load_cases(path)
