"""The function's settings: read from its environment and checked, with no network call."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import errors
import page

MAX_RESULTS = 30

MAX_QUESTION_LIMIT = 10_000
MAX_WINDOW_HOURS = 24 * 365
MAX_KEEP_DAYS = 3650

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR

MAX_PRICE_PER_THOUSAND = Decimal(10)


@dataclass(frozen=True)
class Prices:
    """USD per 1,000 tokens of the answering model, or per 1,000 guardrail text units."""

    input_tokens: Decimal
    output_tokens: Decimal
    topic_units: Decimal
    sensitive_information_units: Decimal
    grounding_units: Decimal
    #: The topic model's tokens, where a model checks the topics; 0 where a guardrail does.
    topic_model_input_tokens: Decimal = Decimal(0)
    topic_model_output_tokens: Decimal = Decimal(0)


@dataclass(frozen=True)
class Settings:
    """What the stack tells the function through its environment."""

    knowledge_base_id: str
    model_id: str
    region: str
    number_of_results: int
    key_parameter_path: str
    budget_table: str
    question_limit: int
    window_hours: int
    keep_days: int
    guardrail_id: str
    guardrail_version: str
    excerpt_guardrail_id: str
    excerpt_guardrail_version: str
    #: Either the topic guardrail and its version, or the topic model; never both.
    topic_guardrail_id: str | None
    topic_guardrail_version: str | None
    prices: Prices
    price_date: str
    topic_model_id: str | None = None
    #: Where the function serves the page itself and is the address, without CloudFront in
    #: front of it: the folder of the page's files (page.py). None behind CloudFront.
    page_folder: str | None = None


#: Environment variable for each setting. The stack sets all of them.
ENVIRONMENT_VARIABLES = {
    "knowledge_base_id": "KNOWLEDGE_BASE_ID",
    "model_id": "MODEL_ID",
    "region": "REGION",
    "number_of_results": "NUMBER_OF_RESULTS",
    "key_parameter_path": "API_KEY_PARAMETER_PATH",
    "budget_table": "BUDGET_TABLE_NAME",
    "question_limit": "QUESTION_LIMIT",
    "window_hours": "WINDOW_HOURS",
    "keep_days": "KEEP_DAYS",
    "guardrail_id": "GUARDRAIL_ID",
    "guardrail_version": "GUARDRAIL_VERSION",
    "excerpt_guardrail_id": "EXCERPT_GUARDRAIL_ID",
    "excerpt_guardrail_version": "EXCERPT_GUARDRAIL_VERSION",
    "price_input_tokens": "PRICE_INPUT_PER_1K_TOKENS",
    "price_output_tokens": "PRICE_OUTPUT_PER_1K_TOKENS",
    "price_topic_units": "PRICE_TOPIC_PER_1K_UNITS",
    "price_sensitive_information_units": "PRICE_PII_PER_1K_UNITS",
    "price_grounding_units": "PRICE_GROUNDING_PER_1K_UNITS",
    "price_date": "PRICE_DATE",
}

#: The settings of each way to check the topics: the stack sets the one or the other. The topic
#: model is set where no guardrail that reads German stays in the region (infra/answer_models.py).
TOPIC_GUARDRAIL_VARIABLES = {
    "topic_guardrail_id": "TOPIC_GUARDRAIL_ID",
    "topic_guardrail_version": "TOPIC_GUARDRAIL_VERSION",
}
TOPIC_MODEL_VARIABLES = {
    "topic_model_id": "TOPIC_MODEL_ID",
    "price_topic_model_input_tokens": "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS",
    "price_topic_model_output_tokens": "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS",
}

#: Set only where the function is the address itself (infra/answer_models.py): then it serves
#: the page from this folder, and no CloudFront waits in front of it.
PAGE_VARIABLES = {"page_folder": page.PAGE_FOLDER_VARIABLE}


def whole_number(variable: str, text: str, lowest: int, highest: int) -> int:
    """A setting that has to be a whole number in a range; the error names the variable."""
    if not (text.isascii() and text.isdigit()) or not lowest <= int(text) <= highest:
        raise errors.ConfigurationError(
            f"{variable} must be a whole number from {lowest} to {highest}, not {text!r}."
        )
    return int(text)


def price(variable: str, text: str) -> Decimal:
    """A price per thousand: a plain decimal from 0 to `MAX_PRICE_PER_THOUSAND`."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or not Decimal(0) <= value <= MAX_PRICE_PER_THOUSAND:
        raise errors.ConfigurationError(
            f"{variable} must be a price in USD from 0 to {MAX_PRICE_PER_THOUSAND}, not {text!r}."
        )
    return value


def price_day(text: str) -> str:
    """The day the prices were read, as ``YYYY-MM-DD`` and a real date."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is None or day.isoformat() != text:
        raise errors.ConfigurationError(f"PRICE_DATE must be a date as YYYY-MM-DD, not {text!r}.")
    return text


def topic_check_variables(environ: Mapping[str, str]) -> dict[str, str]:
    """The variables of the one way to check the topics the environment sets: the topic model
    once ``TOPIC_MODEL_ID`` is set, else the topic guardrail; both at once is refused."""
    if not environ.get("TOPIC_MODEL_ID", "").strip():
        return TOPIC_GUARDRAIL_VARIABLES
    both = [name for name in TOPIC_GUARDRAIL_VARIABLES.values() if environ.get(name, "").strip()]
    if both:
        raise errors.ConfigurationError(
            f"TOPIC_MODEL_ID and {', '.join(both)} are both set: the topics are checked by the "
            "topic model or by the topic guardrail, not by both."
        )
    return TOPIC_MODEL_VARIABLES


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Read and check the settings; no network call."""
    values: dict[str, str] = {}
    for name, variable in {**ENVIRONMENT_VARIABLES, **topic_check_variables(environ)}.items():
        value = environ.get(variable, "").strip()
        if not value:
            raise errors.ConfigurationError(f"The environment variable {variable} is not set.")
        values[name] = value

    number_of_results = whole_number(
        "NUMBER_OF_RESULTS", values["number_of_results"], 1, MAX_RESULTS
    )
    question_limit = whole_number("QUESTION_LIMIT", values["question_limit"], 1, MAX_QUESTION_LIMIT)
    window_hours = whole_number("WINDOW_HOURS", values["window_hours"], 1, MAX_WINDOW_HOURS)
    keep_days = whole_number("KEEP_DAYS", values["keep_days"], 1, MAX_KEEP_DAYS)
    path = values["key_parameter_path"]
    if not path.startswith("/") or path.endswith("/"):
        raise errors.ConfigurationError(
            f"API_KEY_PARAMETER_PATH must start with a slash and not end with one, not {path!r}."
        )
    if keep_days * SECONDS_PER_DAY <= window_hours * SECONDS_PER_HOUR:
        raise errors.ConfigurationError(
            f"KEEP_DAYS ({keep_days}) must outlast WINDOW_HOURS ({window_hours}): a budget "
            "whose item disappears before its window closes is no budget."
        )
    variables = {**ENVIRONMENT_VARIABLES, **TOPIC_GUARDRAIL_VARIABLES}
    for name in ("guardrail_version", "excerpt_guardrail_version", "topic_guardrail_version"):
        if name in values and not (values[name].isascii() and values[name].isdigit()):
            raise errors.ConfigurationError(
                f"{variables[name]} must be a version number, not {values[name]!r}."
            )
    topic_model_prices = {
        field_name: price(variable, values[name])
        for field_name, name, variable in (
            (
                "topic_model_input_tokens",
                "price_topic_model_input_tokens",
                "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS",
            ),
            (
                "topic_model_output_tokens",
                "price_topic_model_output_tokens",
                "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS",
            ),
        )
        if name in values
    }
    prices = Prices(
        input_tokens=price("PRICE_INPUT_PER_1K_TOKENS", values["price_input_tokens"]),
        output_tokens=price("PRICE_OUTPUT_PER_1K_TOKENS", values["price_output_tokens"]),
        topic_units=price("PRICE_TOPIC_PER_1K_UNITS", values["price_topic_units"]),
        sensitive_information_units=price(
            "PRICE_PII_PER_1K_UNITS", values["price_sensitive_information_units"]
        ),
        grounding_units=price("PRICE_GROUNDING_PER_1K_UNITS", values["price_grounding_units"]),
        **topic_model_prices,
    )

    return Settings(
        knowledge_base_id=values["knowledge_base_id"],
        model_id=values["model_id"],
        region=values["region"],
        number_of_results=number_of_results,
        key_parameter_path=values["key_parameter_path"],
        budget_table=values["budget_table"],
        question_limit=question_limit,
        window_hours=window_hours,
        keep_days=keep_days,
        guardrail_id=values["guardrail_id"],
        guardrail_version=values["guardrail_version"],
        excerpt_guardrail_id=values["excerpt_guardrail_id"],
        excerpt_guardrail_version=values["excerpt_guardrail_version"],
        topic_guardrail_id=values.get("topic_guardrail_id"),
        topic_guardrail_version=values.get("topic_guardrail_version"),
        prices=prices,
        price_date=price_day(values["price_date"]),
        topic_model_id=values.get("topic_model_id"),
        page_folder=environ.get(page.PAGE_FOLDER_VARIABLE, "").strip() or None,
    )
