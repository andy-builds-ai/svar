"""The split of Andur's one stack into Taug's and Svar's, held in place.

`cdk refactor` moves a resource from one stack to the other only when its logical id, type and
properties are unchanged, and refuses the whole move when anything else changed
(docs/decisions.md). These tests pin which logical ids each stack holds in each region, so that
a change between the deploy of the new stack and the move shows up here first, and they check
the conditions of the move itself.
"""

import json
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from taug.infra.answer_models import region_models
from tests.test_stack import andur_stacks, deploy_context

#: What moves from AndurStack to TaugStack, by logical id: the documents, the vectors and the
#: knowledge base over them.
MOVED = {
    "DocumentsBucket9EC9DEB9": "AWS::S3::Bucket",
    "DocumentsBucketPolicy1F991BD4": "AWS::S3::BucketPolicy",
    "VectorBucket": "AWS::S3Vectors::VectorBucket",
    "VectorIndex": "AWS::S3Vectors::Index",
    "KnowledgeBaseRoleA2B317B9": "AWS::IAM::Role",
    "KnowledgeBase": "AWS::Bedrock::KnowledgeBase",
    "DocumentsDataSource": "AWS::Bedrock::DataSource",
}

#: Built anew in TaugStack under the ids it had in AndurStack: its provider's policy
#: (AWS::IAM::Policy) cannot be moved.
KEY_RETIRER = {
    "ApiKeyFunction7C99575B",
    "ApiKeyFunctionLogsB0447CCF",
    "ApiKeyFunctionRoleC17A4D17",
    "ApiKeyProviderLogsF4655EA2",
    "ApiKeyProviderframeworkonEvent9849A943",
    "ApiKeyProviderframeworkonEventServiceRole12F1E908",
    "ApiKeyProviderframeworkonEventServiceRoleDefaultPolicy3A0B3B27",
}

#: Svar's resources, which stay in AndurStack. Ireland has CloudFront and the page in a
#: bucket; Frankfurt serves the page from the function's own URL and a layer.
SVAR_BOTH = {
    "AnswerFunctionC9E3DEFB",
    "AnswerFunctionFunctionUrl222F3513",
    "AnswerFunctionLogsFF553602",
    "AnswerFunctionRoleC3203451",
    "AnswerGuardrail",
    "AnswerGuardrailVersion",
    "DeployDrain",
    "DeployDrainFunction4E65C1B7",
    "DeployDrainFunctionLogsCFEC8188",
    "DeployDrainFunctionRoleCA5EA332",
    "DeployDrainProviderLogsF36D4E3F",
    "DeployDrainProviderframeworkonEvent7784E926",
    "DeployDrainProviderframeworkonEventServiceRoleC3B2541E",
    "DeployDrainProviderframeworkonEventServiceRoleDefaultPolicy1921DC00",
    "ExcerptGuardrail",
    "ExcerptGuardrailVersion",
    "KeyBudgetTable34ED4D7A",
}
SVAR = {
    "eu-west-1": SVAR_BOTH
    | {
        "AnswerOriginAccessControl4A7F44F8",
        "CustomCDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C81C01536",
        "CustomCDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756CServiceRole89A01265",
        "CustomCDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756CServiceRoleDefaultPolicy88902FDF",
        "InvokeFunctionFromCloudFront",
        "SecurityHeadersE66B69D3",
        "SiteBucket397A1860",
        "SiteBucketPolicy3AC1D0F8",
        "SiteDistribution3FF9535D",
        "SiteDistributionOrigin2InvokeFromApiForAndurStackSiteDistributionOrigin250E2A2DA52E31D8C",
        "SiteDistributionOrigin2InvokeFunctionFromCloudFrontForAndurStackSiteDistributionOrigin250E2A2DA5E080678",
        "SiteFilesAwsCliLayer5D1C20DE",
        "SiteFilesCustomResource8693D8DF",
        "SiteFilesLogsE08292DF",
        "SiteOriginAccessControl4DD454FF",
        "TopicGuardrail",
        "TopicGuardrailVersion",
    },
    "eu-central-1": SVAR_BOTH
    | {
        "AnswerFunctioninvokefunction8CE8DDED",
        "AnswerFunctioninvokefunctionurlF5B59E2A",
        "PageLayer6DEEDD1A",
    },
}

#: Types CloudFormation cannot move or cannot provision on its own (describe-type, run by the
#: operator in both regions on 09.11.2026): they must stay where they are.
CANNOT_MOVE = {
    "AWS::Bedrock::GuardrailVersion",
    "AWS::Lambda::Permission",
    "AWS::Lambda::LayerVersion",
    "AWS::DynamoDB::GlobalTable",
}


@pytest.fixture(scope="module", params=["eu-west-1", "eu-central-1"])
def region(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(scope="module")
def templates(region: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Taug's and Svar's templates for ``region``, as infra/app.py builds them: Svar's stack
    under the name AndurStack, the logical ids of its CloudFront permissions carry it."""
    models = region_models(region)
    taug, svar = andur_stacks(
        cdk.App(context=deploy_context()),
        "AndurStack",
        env=cdk.Environment(region=region),
        answer_model_id=models.answer_model_id,
        topic_model_id=models.topic_model_id,
        answer_guardrail_profile=models.answer_guardrail_profile,
        behind_cloudfront=models.behind_cloudfront,
    )
    return Template.from_stack(taug).to_json(), Template.from_stack(svar).to_json()


def resources(template: dict[str, Any]) -> dict[str, Any]:
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] != "AWS::CDK::Metadata"
    }


def test_each_stack_holds_exactly_its_logical_ids(
    region: str, templates: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    taug, svar = templates
    assert set(resources(taug)) == set(MOVED) | KEY_RETIRER
    assert set(resources(svar)) == SVAR[region]


def test_what_moves_keeps_its_logical_id_and_type(
    templates: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    taug, svar = templates
    for logical_id, resource_type in MOVED.items():
        assert resources(taug)[logical_id]["Type"] == resource_type
        assert logical_id not in resources(svar)


def test_nothing_that_cannot_move_is_in_taugs_stack(
    templates: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    taug, svar = templates
    assert not {resource["Type"] for resource in resources(taug).values()} & CANNOT_MOVE
    assert {MOVED[logical_id] for logical_id in MOVED}.isdisjoint(CANNOT_MOVE)
    assert {"AWS::Bedrock::GuardrailVersion"} <= {r["Type"] for r in resources(svar).values()}


def test_no_moved_resource_names_its_own_stack(
    templates: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """A pseudo parameter that differs between the two stacks would change the resource on
    the move, and AWS refuses that."""
    taug, _ = templates
    for logical_id in MOVED:
        rendered = json.dumps(resources(taug)[logical_id])
        assert "AWS::StackName" not in rendered and "AWS::StackId" not in rendered, logical_id


def test_svar_reads_only_the_knowledge_base_from_taug(
    templates: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """Two imports, both of the knowledge base; Taug reads nothing from Svar."""
    taug, svar = templates
    exports = {
        output["Export"]["Name"]: output["Value"]
        for output in taug["Outputs"].values()
        if "Export" in output
    }
    assert sorted(exports.values(), key=json.dumps) == [
        {"Fn::GetAtt": ["KnowledgeBase", "KnowledgeBaseArn"]},
        {"Fn::GetAtt": ["KnowledgeBase", "KnowledgeBaseId"]},
    ]
    rendered = json.dumps(svar)
    assert sorted(name for name in exports if f'"Fn::ImportValue": "{name}"' in rendered) == sorted(
        exports
    )
    assert "Fn::ImportValue" not in json.dumps(taug)
