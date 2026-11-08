"""Svar's question handler: one Lambda function behind a Function URL."""

import json
import os
import time
import traceback
from collections.abc import Mapping
from typing import Any

import answering
import budget
import clocks
import config
import errors
import intake
import keys
import log
import page
import refunds
import sentences


def http_response(
    status: int, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """A Function URL response with a JSON body."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        "body": json.dumps(body, ensure_ascii=False),
    }


def handle(event: Mapping[str, Any], record: log.RequestRecord) -> dict[str, Any]:
    """Run one request through every check and return the response body."""
    intake.require_post(event)
    body = intake.read_body(event)
    record.language = intake.requested_language(body)
    workspace = intake.requested_workspace(body)
    record.workspace = workspace

    settings = config.load_settings(os.environ)
    key = intake.request_key(event.get("headers"))
    keys.authorize(key, workspace, settings)

    question = intake.requested_question(body)
    charge = budget.charge_question(settings, workspace, key)
    try:
        return answering.answer_question(settings, workspace, question, record)
    except refunds.REFUNDABLE_ERRORS as error:
        elapsed = clocks.clock() - record.started
        error.refunded = refunds.refund_question(
            settings, workspace, key, charge, error, elapsed, record.model_answered
        )
        raise


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point: one HTTP request in, one HTTP response out.

    Behind CloudFront only questions reach the function. Where it is the address itself
    (`PAGE_VARIABLES`), it serves the page as well, and every response carries the headers
    CloudFront would have set (page.py)."""
    page_folder = os.environ.get(page.PAGE_FOLDER_VARIABLE, "").strip()
    if not page_folder:
        return answer_request(event)
    try:
        served = page.route(event, page_folder)
    except Exception:
        log.log_event("crashed", status=500, traceback=traceback.format_exc())
        served = http_response(
            500, {"error": sentences.in_language(sentences.UNEXPECTED_FAILURE_MESSAGES, "en")}
        )
    return page.with_security_headers(served if served is not None else answer_request(event))


def answer_request(event: Mapping[str, Any]) -> dict[str, Any]:
    """One question in, one HTTP response out, one log line."""
    started = time.perf_counter()
    record = log.RequestRecord()
    try:
        body = handle(event, record)
    except errors.HandlerError as error:
        log.log_event(
            "refused" if error.status < 500 else "failed",
            status=error.status,
            workspace=record.workspace,
            error=str(error),
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
        body = {"error": error.public_message(record.language)}
        if error.refunded:
            body["refunded"] = True
        return http_response(error.status, body, error.headers)
    except Exception:
        log.log_event(
            "crashed", status=500, workspace=record.workspace, traceback=traceback.format_exc()
        )
        return http_response(
            500,
            {
                "error": sentences.in_language(
                    sentences.UNEXPECTED_FAILURE_MESSAGES, record.language
                )
            },
        )

    body["latency_ms"] = round((time.perf_counter() - started) * 1000)
    log.log_event(
        "answered",
        status=200,
        workspace=record.workspace,
        model=body["model"],
        input_tokens=body["usage"]["input_tokens"],
        output_tokens=body["usage"]["output_tokens"],
        hits=record.hits,
        dropped=record.dropped,
        cited=sum(1 for source in body["sources"] if source["cited"]),
        guardrail=record.guardrail.get("action"),
        guardrail_policies=record.guardrail.get("policies"),
        excerpt_policies=record.excerpt_policies,
        grounding_score=record.guardrail.get("grounding_score"),
        relevance_score=record.guardrail.get("relevance_score"),
        guardrail_units=record.guardrail.get("units"),
        topic_tokens=record.topic_tokens,
        cost_usd=body["cost_usd"],
        price_date=body["price_date"],
        latency_ms=body["latency_ms"],
    )
    return http_response(200, body)
