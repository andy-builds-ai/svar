"""The page behind CloudFront: the site bucket, the distribution and its security headers."""

from typing import TYPE_CHECKING

from aws_cdk import (
    Aws,
    Duration,
    RemovalPolicy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
)

from app.page import (
    ASK_PATH,
    CONTENT_SECURITY_POLICY,
    HSTS_MAX_AGE_DAYS,
    ROOT_DOCUMENT,
)
from infra.stacks.shared import ASSET_EXCLUDES, WEB_FOLDER, create_log_group

if TYPE_CHECKING:
    from infra.stacks.andur_stack import AndurStack


#: The file CloudFront, or the function, serves for the bare address (app/page.py).
SITE_ROOT_DOCUMENT = ROOT_DOCUMENT

ORIGIN_RESPONSE_TIMEOUT_SECONDS = 120


def create_site_bucket(stack: "AndurStack") -> s3.Bucket:
    """The bucket the page is served from — private, and reachable only through CloudFront."""
    return s3.Bucket(
        stack,
        "SiteBucket",
        bucket_name=f"andur-site-{Aws.ACCOUNT_ID}-{Aws.REGION}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        removal_policy=RemovalPolicy.DESTROY,
    )


def create_security_headers(stack: "AndurStack") -> cloudfront.ResponseHeadersPolicy:
    """The headers every response carries, page and answer alike."""
    return cloudfront.ResponseHeadersPolicy(
        stack,
        "SecurityHeaders",
        # CloudFront names are account-wide: the region keeps two stacks apart.
        response_headers_policy_name=f"andur-security-headers-{Aws.REGION}",
        comment="Andur: what the page may load, and how it must be reached.",
        security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
            content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                content_security_policy=CONTENT_SECURITY_POLICY, override=True
            ),
            strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                access_control_max_age=Duration.days(HSTS_MAX_AGE_DAYS),
                include_subdomains=True,
                preload=False,
                override=True,
            ),
            content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
            referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
            ),
            frame_options=cloudfront.ResponseHeadersFrameOptions(
                frame_option=cloudfront.HeadersFrameOption.DENY, override=True
            ),
        ),
    )


def create_distribution(
    stack: "AndurStack", site_bucket: s3.Bucket, function_url: lambda_.FunctionUrl
) -> cloudfront.Distribution:
    """One address for everything: the page from the bucket, `ASK_PATH` from the function."""
    headers = create_security_headers(stack)
    return cloudfront.Distribution(
        stack,
        "SiteDistribution",
        comment="Andur: the page, and /ask on its way to the answer function.",
        default_root_object=SITE_ROOT_DOCUMENT,
        default_behavior=cloudfront.BehaviorOptions(
            origin=origins.S3BucketOrigin.with_origin_access_control(
                site_bucket,
                origin_access_control=cloudfront.S3OriginAccessControl(
                    stack,
                    "SiteOriginAccessControl",
                    origin_access_control_name=f"andur-site-{Aws.REGION}",
                ),
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            response_headers_policy=headers,
        ),
        additional_behaviors={
            ASK_PATH: cloudfront.BehaviorOptions(
                origin=origins.FunctionUrlOrigin.with_origin_access_control(
                    function_url,
                    origin_access_control=cloudfront.FunctionUrlOriginAccessControl(
                        stack,
                        "AnswerOriginAccessControl",
                        origin_access_control_name=f"andur-answer-{Aws.REGION}",
                    ),
                    read_timeout=Duration.seconds(ORIGIN_RESPONSE_TIMEOUT_SECONDS),
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                response_headers_policy=headers,
            )
        },
    )


def allow_cloudfront_to_invoke(
    stack: "AndurStack", function: lambda_.Function, distribution: cloudfront.Distribution
) -> lambda_.CfnPermission:
    """The second of the two permissions the CloudFront documentation asks for."""
    return lambda_.CfnPermission(
        stack,
        "InvokeFunctionFromCloudFront",
        action="lambda:InvokeFunction",
        function_name=function.function_arn,
        principal="cloudfront.amazonaws.com",
        source_arn=(
            f"arn:{Aws.PARTITION}:cloudfront::{Aws.ACCOUNT_ID}:"
            f"distribution/{distribution.distribution_id}"
        ),
    )


def upload_the_page(
    stack: "AndurStack", site_bucket: s3.Bucket, distribution: cloudfront.Distribution
) -> s3_deployment.BucketDeployment:
    """Put web/ into the site bucket as part of the deploy, and clear the caches."""
    return s3_deployment.BucketDeployment(
        stack,
        "SiteFiles",
        sources=[s3_deployment.Source.asset(str(WEB_FOLDER), exclude=ASSET_EXCLUDES)],
        destination_bucket=site_bucket,
        distribution=distribution,
        distribution_paths=["/*"],
        prune=True,
        retain_on_delete=False,
        log_group=create_log_group(stack, "SiteFilesLogs"),
    )
