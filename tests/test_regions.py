"""The stack in each region of infra/answer_models.py: Ireland as it runs, Frankfurt with its own
answer model and gpt-oss-120b checking the topics, nothing through the EU profile; and the
fallback in Frankfurt, the topic guardrail through the EU profile, as the code still allows."""

import json

import aws_cdk as cdk
import config
import page
import prompts
import pytest
from aws_cdk.assertions import Template

from infra.answer_models import (
    ANSWER_MODELS,
    DEFAULT_PROMPT,
    DEFAULT_REGION,
    FRANKFURT_MODELS,
    FRANKFURT_PRICE_DATE,
    PROMPT_EACH_PART,
    PROMPT_SENTENCE_FIRST,
    REGIONS,
    answer_guardrail_uses_profile,
    answer_model,
    region_answer_guardrail_uses_profile,
    region_models,
)
from infra.stacks.andur_stack import AndurStack
from infra.stacks.answer_function import (
    ANSWER_MAX_CONCURRENCY,
    PAGE_LAYER_FOLDER,
    PAGE_LOAD_CONCURRENCY,
)
from infra.stacks.guardrails import GUARDRAIL_PROFILE_REGIONS
from tests.test_stack import (
    deploy_context,
    get_att,
    guarded_by,
    resource_of,
    role_policies,
)

TOPIC_MODEL_ID = "openai.gpt-oss-120b-1:0"
ACCOUNT = "111111111111"


def profile_arn(region: str) -> dict[str, object]:
    """The EU guardrail profile in ``region``, as a stack with a set account renders it."""
    return {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":bedrock:{region}:{ACCOUNT}:guardrail-profile/eu.guardrail.v1:0",
            ],
        ]
    }


def stack_in(region: str, **overrides: object) -> AndurStack:
    """The stack as infra/app.py builds it for ``region``, with ``overrides`` on top."""
    models = region_models(region)
    settings = {
        "answer_model_id": models.answer_model_id,
        "topic_model_id": models.topic_model_id,
        "answer_guardrail_profile": models.answer_guardrail_profile,
        "behind_cloudfront": models.behind_cloudfront,
        **overrides,
    }
    return AndurStack(
        cdk.App(context=deploy_context()),
        f"Stack{region.replace('-', '')}{len(overrides)}",
        env=cdk.Environment(account=ACCOUNT, region=region),
        **settings,
    )


@pytest.fixture(scope="module")
def frankfurt() -> tuple[AndurStack, Template]:
    stack = stack_in("eu-central-1")
    return stack, Template.from_stack(stack)


@pytest.fixture(scope="module")
def fallback() -> tuple[AndurStack, Template]:
    """Frankfurt with the topic guardrail through the EU profile instead of a topic model."""
    stack = stack_in("eu-central-1", topic_model_id=None)
    return stack, Template.from_stack(stack)


# --- The table -------------------------------------------------------------


def test_ireland_stays_the_default_with_its_model_and_profile() -> None:
    assert DEFAULT_REGION == "eu-west-1"
    ireland = REGIONS["eu-west-1"]
    assert ireland.answer_model_id == "eu.amazon.nova-pro-v1:0"
    assert (ireland.topic_model_id, ireland.answer_guardrail_profile) == (None, True)


def test_frankfurt_answers_with_qwen_and_checks_everything_at_home() -> None:
    frankfurt = REGIONS["eu-central-1"]
    assert frankfurt.answer_model_id == "qwen.qwen3-235b-a22b-2507-v1:0"
    assert frankfurt.topic_model_id == TOPIC_MODEL_ID
    assert frankfurt.answer_guardrail_profile is False
    assert frankfurt.price_date == FRANKFURT_PRICE_DATE


@pytest.mark.parametrize("region", sorted(REGIONS))
def test_every_model_of_a_region_has_a_price_there_and_runs_there(region: str) -> None:
    """A topic model runs in the region itself; only an answer model may be an EU profile."""
    models = REGIONS[region]
    answer_model(models.answer_model_id, region)
    if models.topic_model_id is not None:
        answer_model(models.topic_model_id, region)
        assert not models.topic_model_id.startswith("eu.")


def test_where_a_model_checks_the_topics_no_guardrail_uses_the_profile() -> None:
    assert answer_guardrail_uses_profile(True, None)
    assert not answer_guardrail_uses_profile(False, None)
    assert not answer_guardrail_uses_profile(True, TOPIC_MODEL_ID)


def test_the_grounding_probe_follows_the_stacks_rule_on_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A region with a topic model and `answer_guardrail_profile` set: the stack keeps its
    answer guardrail in the region, so the probe does as well."""
    from scripts import grounding_probe

    frankfurt = REGIONS["eu-central-1"]
    monkeypatch.setitem(REGIONS, "eu-central-1", frankfurt._replace(answer_guardrail_profile=True))
    assert not region_answer_guardrail_uses_profile("eu-central-1")
    assert "crossRegionConfig" not in grounding_probe.probe_request("eu-central-1")
    stack = stack_in("eu-central-1")
    assert stack.answer_guardrail_profile is False


def test_only_frankfurt_goes_without_cloudfront() -> None:
    assert REGIONS["eu-west-1"].behind_cloudfront is True
    assert REGIONS["eu-central-1"].behind_cloudfront is False


def test_a_region_without_a_table_is_refused() -> None:
    with pytest.raises(KeyError, match="has no models"):
        region_models("us-east-1")
    with pytest.raises(KeyError, match="has no price for eu-central-1"):
        answer_model("eu.amazon.nova-2-lite-v1:0", "eu-central-1")


# --- The fallback in Frankfurt: the topic guardrail through the profile ------


def test_the_fallback_sends_only_the_topic_check_through_the_profile(
    fallback: tuple[AndurStack, Template],
) -> None:
    """Masking and grounding stay in eu-central-1; the Standard topic tier, which reads German,
    goes only with the EU profile."""
    stack, template = fallback
    answer = resource_of(stack, template, stack.guardrail)["Properties"]
    excerpt = resource_of(stack, template, stack.excerpt_guardrail)["Properties"]
    topic = resource_of(stack, template, stack.topic_guardrail)["Properties"]
    assert "CrossRegionConfig" not in answer and "CrossRegionConfig" not in excerpt
    assert "TopicPolicyConfig" not in answer
    assert topic["CrossRegionConfig"] == {"GuardrailProfileArn": profile_arn("eu-central-1")}
    assert topic["TopicPolicyConfig"]["TopicsTierConfig"] == {"TierName": "STANDARD"}


def test_the_fallback_invokes_qwen_in_the_region_and_applies_its_three_guardrails(
    fallback: tuple[AndurStack, Template],
) -> None:
    stack, template = fallback
    statements = role_policies(stack, template, stack.answer_role)["InvokeAnswerModel"]["Statement"]
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert set(by_sid) == {"InvokeAnswerModelStatement", "ApplyAnswerGuardrail"}
    answer = by_sid["InvokeAnswerModelStatement"]
    assert answer["Resource"]["Fn::Join"][1][-1].endswith(
        "::foundation-model/qwen.qwen3-235b-a22b-2507-v1:0"
    )
    assert answer["Condition"] == guarded_by(stack)
    # The order of the resources is IAM's to choose (minimizePolicies sorts them).
    assert sorted(map(json.dumps, by_sid["ApplyAnswerGuardrail"]["Resource"])) == sorted(
        map(
            json.dumps,
            [
                get_att(stack, stack.guardrail, "GuardrailArn"),
                get_att(stack, stack.excerpt_guardrail, "GuardrailArn"),
                get_att(stack, stack.topic_guardrail, "GuardrailArn"),
                *(profile_arn(region) for region in GUARDRAIL_PROFILE_REGIONS),
            ],
        )
    )


def test_the_fallback_tells_the_function_its_model_and_frankfurt_prices(
    fallback: tuple[AndurStack, Template],
) -> None:
    stack, template = fallback
    variables = resource_of(stack, template, stack.answer_function)["Properties"]["Environment"][
        "Variables"
    ]
    assert variables["MODEL_ID"] == "qwen.qwen3-235b-a22b-2507-v1:0"
    assert (variables["PRICE_INPUT_PER_1K_TOKENS"], variables["PRICE_OUTPUT_PER_1K_TOKENS"]) == (
        "0.00029",
        "0.00116",
    )
    assert variables["PRICE_DATE"] == FRANKFURT_PRICE_DATE
    assert "TOPIC_MODEL_ID" not in variables
    assert variables[config.ANSWER_PROMPT_VARIABLE] == "each-part"
    assert set(variables) == {
        *config.ENVIRONMENT_VARIABLES.values(),
        *config.TOPIC_GUARDRAIL_VARIABLES.values(),
        *config.PAGE_VARIABLES.values(),
        config.ANSWER_PROMPT_VARIABLE,
    }


def test_a_stack_without_reserved_concurrency_leaves_it_to_the_account() -> None:
    """The sub-account allowed 10 concurrent executions and Lambda keeps 10 unreserved."""
    stack = stack_in("eu-central-1", answer_max_concurrency=None)
    template = Template.from_stack(stack)
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert "ReservedConcurrentExecutions" not in properties


def test_ireland_keeps_its_three_guardrails_through_the_profile() -> None:
    stack = stack_in("eu-west-1")
    template = Template.from_stack(stack)
    answer = resource_of(stack, template, stack.guardrail)["Properties"]
    assert answer["CrossRegionConfig"] == {"GuardrailProfileArn": profile_arn("eu-west-1")}
    assert len(template.find_resources("AWS::Bedrock::Guardrail")) == 3
    statements = role_policies(stack, template, stack.answer_role)["InvokeAnswerModel"]["Statement"]
    assert "InvokeTopicModel" not in {statement["Sid"] for statement in statements}
    outputs = template.to_json()["Outputs"]
    assert "TopicGuardrailRef" in outputs and "TopicModelId" not in outputs


def test_the_grounding_probe_copies_the_answer_guardrail_of_each_region() -> None:
    from scripts import grounding_probe

    frankfurt = grounding_probe.probe_request("eu-central-1")
    assert "crossRegionConfig" not in frankfurt and "topicPolicyConfig" not in frankfurt
    assert {f["type"] for f in frankfurt["contextualGroundingPolicyConfig"]["filtersConfig"]} == {
        "GROUNDING",
        "RELEVANCE",
    }
    ireland = grounding_probe.probe_request("eu-west-1")
    assert ireland["crossRegionConfig"] == {"guardrailProfileIdentifier": "eu.guardrail.v1:0"}


# --- The stack in Frankfurt: gpt-oss-120b checks the topics -----------------


def test_frankfurt_uses_no_topic_guardrail_and_no_profile(
    frankfurt: tuple[AndurStack, Template],
) -> None:
    stack, template = frankfurt
    guardrails = template.find_resources("AWS::Bedrock::Guardrail")
    assert stack.topic_guardrail is None
    assert len(guardrails) == 2
    assert all("CrossRegionConfig" not in g["Properties"] for g in guardrails.values())
    assert all("TopicPolicyConfig" not in g["Properties"] for g in guardrails.values())
    assert "guardrail-profile" not in json.dumps(template.to_json())


def test_frankfurt_may_invoke_its_topic_model_there_and_nothing_more(
    frankfurt: tuple[AndurStack, Template],
) -> None:
    stack, template = frankfurt
    statements = role_policies(stack, template, stack.answer_role)["InvokeAnswerModel"]["Statement"]
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert set(by_sid) == {"InvokeAnswerModelStatement", "ApplyAnswerGuardrail", "InvokeTopicModel"}
    assert by_sid["InvokeTopicModel"]["Action"] == "bedrock:InvokeModel"
    assert by_sid["InvokeTopicModel"]["Resource"] == {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":bedrock:eu-central-1::foundation-model/{TOPIC_MODEL_ID}",
            ],
        ]
    }
    assert by_sid["ApplyAnswerGuardrail"]["Resource"] == [
        get_att(stack, stack.guardrail, "GuardrailArn"),
        get_att(stack, stack.excerpt_guardrail, "GuardrailArn"),
    ]


def test_frankfurt_tells_the_function_both_models_and_frankfurt_prices(
    frankfurt: tuple[AndurStack, Template],
) -> None:
    stack, template = frankfurt
    variables = resource_of(stack, template, stack.answer_function)["Properties"]["Environment"][
        "Variables"
    ]
    assert variables["MODEL_ID"] == "qwen.qwen3-235b-a22b-2507-v1:0"
    assert variables["PRICE_DATE"] == FRANKFURT_PRICE_DATE
    assert variables["TOPIC_MODEL_ID"] == TOPIC_MODEL_ID
    assert variables["PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS"] == "0.0002"
    assert variables["PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS"] == "0.00079"
    assert variables[config.ANSWER_PROMPT_VARIABLE] == "each-part"
    assert set(variables) == {
        *config.ENVIRONMENT_VARIABLES.values(),
        *config.TOPIC_MODEL_VARIABLES.values(),
        *config.PAGE_VARIABLES.values(),
        config.ANSWER_PROMPT_VARIABLE,
    }
    (drain,) = template.find_resources("Custom::AndurDeployDrain").values()
    assert drain["Properties"]["GuardrailVersions"] == [
        {"Ref": stack.get_logical_id(stack.guardrail_version)},
        {"Ref": stack.get_logical_id(stack.excerpt_guardrail_version)},
    ]
    outputs = template.to_json()["Outputs"]
    assert outputs["TopicModelId"]["Value"] == TOPIC_MODEL_ID
    assert "TopicGuardrailRef" not in outputs


# --- Frankfurt without CloudFront: the function's URL is the address ----------

CLOUDFRONT_TYPES = (
    "AWS::CloudFront::Distribution",
    "AWS::CloudFront::OriginAccessControl",
    "AWS::CloudFront::ResponseHeadersPolicy",
)


def test_frankfurt_has_no_cloudfront_and_no_site_bucket(
    frankfurt: tuple[AndurStack, Template],
) -> None:
    """Nothing of a request passes an edge location outside the region, and no bucket holds
    the page: the function hands it out."""
    stack, template = frankfurt
    for resource_type in CLOUDFRONT_TYPES:
        assert template.find_resources(resource_type) == {}
    assert template.find_resources("Custom::CDKBucketDeployment") == {}
    assert stack.site_bucket is None and stack.distribution is None
    assert len(template.find_resources("AWS::S3::Bucket")) == 1  # the documents
    assert "cloudfront" not in json.dumps(template.to_json()["Resources"]).lower()


def test_frankfurt_opens_the_url_and_lets_it_invoke_the_function_and_nothing_else(
    frankfurt: tuple[AndurStack, Template],
) -> None:
    """AuthType NONE needs both actions since October 2025: the URL itself, and the function
    only through the URL (Lambda docs, "Control access to Lambda function URLs")."""
    stack, template = frankfurt
    (url,) = template.find_resources("AWS::Lambda::Url").values()
    assert url["Properties"]["AuthType"] == "NONE"
    function = get_att(stack, stack.answer_function.node.default_child, "Arn")
    assert url["Properties"]["TargetFunctionArn"] == function
    permissions = {
        p["Properties"]["Action"]: p["Properties"]
        for p in template.find_resources("AWS::Lambda::Permission").values()
        if p["Properties"]["FunctionName"] == function
    }
    assert permissions == {
        "lambda:InvokeFunctionUrl": {
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionName": function,
            "FunctionUrlAuthType": "NONE",
            "Principal": "*",
        },
        "lambda:InvokeFunction": {
            "Action": "lambda:InvokeFunction",
            "FunctionName": function,
            "InvokedViaFunctionUrl": True,
            "Principal": "*",
        },
    }


def test_frankfurt_serves_the_page_from_a_layer(frankfurt: tuple[AndurStack, Template]) -> None:
    stack, template = frankfurt
    assert stack.page_layer is not None
    (layer,) = template.find_resources("AWS::Lambda::LayerVersion")
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert properties["Layers"] == [{"Ref": layer}]
    assert properties["Environment"]["Variables"]["PAGE_FOLDER"] == PAGE_LAYER_FOLDER == "/opt"


def test_frankfurt_keeps_room_for_a_page_load_beside_its_questions(
    frankfurt: tuple[AndurStack, Template],
) -> None:
    """Each file of the page is a call of the function: a page load must not wait for, or be
    refused beside, the five questions the reservation allows."""
    stack, template = frankfurt
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert properties["ReservedConcurrentExecutions"] == ANSWER_MAX_CONCURRENCY + 5 == 10
    assert PAGE_LOAD_CONCURRENCY == 1 + len({"style.css", "app.js", "language.js", "markdown.js"})
    unreserved = stack_in("eu-central-1", answer_max_concurrency=None)
    unreserved_template = Template.from_stack(unreserved)
    properties = resource_of(unreserved, unreserved_template, unreserved.answer_function)
    assert "ReservedConcurrentExecutions" not in properties["Properties"]


def test_frankfurt_names_the_url_as_its_address(frankfurt: tuple[AndurStack, Template]) -> None:
    """scripts/ask.py posts to <WebUrl>/ask, in either region."""
    stack, template = frankfurt
    outputs = template.to_json()["Outputs"]
    url = stack.function_url.node.default_child
    assert outputs["WebUrl"]["Value"] == get_att(stack, url, "FunctionUrl")
    assert "FunctionUrl" not in outputs


def test_ireland_keeps_cloudfront_the_signed_url_and_no_page_in_the_function() -> None:
    stack = stack_in("eu-west-1")
    template = Template.from_stack(stack)
    for resource_type in CLOUDFRONT_TYPES:
        assert len(template.find_resources(resource_type)) >= 1
    (url,) = template.find_resources("AWS::Lambda::Url").values()
    assert url["Properties"]["AuthType"] == "AWS_IAM"
    # The one layer here is CDK's own, for uploading the page to its bucket.
    assert stack.page_layer is None
    assert not [
        layer
        for layer in template.find_resources("AWS::Lambda::LayerVersion").values()
        if "the page's files" in layer["Properties"].get("Description", "")
    ]
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert "Layers" not in properties
    assert "PAGE_FOLDER" not in properties["Environment"]["Variables"]


def test_the_function_sets_the_headers_cloudfront_sets_in_ireland() -> None:
    """Read back from Ireland's response headers policy: the same values, so a page in
    Frankfurt is held to the same rules as in Ireland."""
    from tests.test_stack import security_headers

    policy = security_headers(Template.from_stack(stack_in("eu-west-1")))
    hsts = policy["StrictTransportSecurity"]
    assert (hsts["IncludeSubdomains"], hsts["Preload"]) == (True, False)
    assert policy["ContentTypeOptions"]["Override"] is True
    from_cloudfront = {
        "Content-Security-Policy": policy["ContentSecurityPolicy"]["ContentSecurityPolicy"],
        "Strict-Transport-Security": (
            f"max-age={hsts['AccessControlMaxAgeSec']}; includeSubDomains"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": policy["ReferrerPolicy"]["ReferrerPolicy"],
        "X-Frame-Options": policy["FrameOptions"]["FrameOption"],
    }
    assert page.SECURITY_HEADERS == from_cloudfront


# --- The grounding threshold, measured for each region's answer model --------


def grounding_filters(request_filters: list[dict[str, object]]) -> dict[str, object]:
    """Threshold by type, from a CreateGuardrail request (``type``) or a template (``Type``)."""
    return {
        f.get("type", f.get("Type")): f.get("threshold", f.get("Threshold"))
        for f in request_filters
    }


def test_frankfurt_checks_grounding_at_its_own_threshold_ireland_at_the_default(
    frankfurt: tuple[AndurStack, Template],
) -> None:
    """0.8 withheld the most made-up answers and none of the right ones to German documents,
    Qwen3 235B's own in three runs (07.10.2026); Ireland keeps 0.7."""
    assert REGIONS["eu-central-1"].grounding_threshold == 0.8
    assert REGIONS["eu-west-1"].grounding_threshold is None
    ireland = stack_in("eu-west-1")
    for region, stack, template in (
        ("eu-central-1", *frankfurt),
        ("eu-west-1", ireland, Template.from_stack(ireland)),
    ):
        answer = resource_of(stack, template, stack.guardrail)["Properties"]
        filters = answer["ContextualGroundingPolicyConfig"]["FiltersConfig"]
        expected = 0.8 if region == "eu-central-1" else 0.7
        assert grounding_filters(filters) == {"GROUNDING": expected, "RELEVANCE": 0.7}


def test_the_table_and_the_function_name_the_same_prompt_wordings() -> None:
    """infra/ and app/ share no code, so each lists the names itself."""
    assert set(prompts.PROMPTS) == {PROMPT_EACH_PART, PROMPT_SENTENCE_FIRST}
    assert prompts.DEFAULT_PROMPT == DEFAULT_PROMPT == PROMPT_EACH_PART
    for model in (*ANSWER_MODELS, *FRANKFURT_MODELS):
        assert model.prompt in prompts.PROMPTS, model.name


def test_the_prompt_belongs_to_the_model_nova_pro_in_both_tables_qwen_in_frankfurt() -> None:
    """Nova Pro: q02 and q19 right 10 of 10 times with `sentence-first`, 2 and 0 of 10 with
    `each-part` (14.10.2026). Qwen3 235B: c32 right 20 of 20 with `each-part` (07.10.2026).
    The other models keep the wording they have run with since 07.10.2026."""
    for table in (ANSWER_MODELS, FRANKFURT_MODELS):
        by_name = {model.name: model.prompt for model in table}
        assert by_name["Nova Pro"] == PROMPT_SENTENCE_FIRST
        assert {p for name, p in by_name.items() if name != "Nova Pro"} == {PROMPT_EACH_PART}
    frankfurt = REGIONS["eu-central-1"]
    assert answer_model(frankfurt.answer_model_id, "eu-central-1").prompt == PROMPT_EACH_PART
    ireland = REGIONS["eu-west-1"]
    assert answer_model(ireland.answer_model_id, "eu-west-1").prompt == PROMPT_SENTENCE_FIRST


@pytest.mark.parametrize(("region", "expected"), [("eu-central-1", 0.8), ("eu-west-1", 0.7)])
def test_the_probes_copy_the_grounding_threshold_of_their_region(
    region: str, expected: float
) -> None:
    from scripts import control_eval, grounding_probe

    probe = grounding_probe.probe_request(region)["contextualGroundingPolicyConfig"]
    assert grounding_filters(probe["filtersConfig"]) == {"GROUNDING": expected, "RELEVANCE": 0.7}
    answer = control_eval.probe_requests("model", False, region)["answer"]
    filters = answer["contextualGroundingPolicyConfig"]["filtersConfig"]
    assert grounding_filters(filters) == {"GROUNDING": expected, "RELEVANCE": 0.7}
