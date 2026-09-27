# Copyright 2012-2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

import json
import logging
import os
from unittest.mock import Mock, patch

import pytest

from botocore.endpoint_provider import (
    EndpointProvider,
    EndpointRule,
    ErrorRule,
    RuleCreator,
    RuleSet,
    RuleSetEndpoint,
    RuleSetStandardLibrary,
    TreeRule,
)
from botocore.exceptions import (
    EndpointResolutionError,
    MissingDependencyException,
    UnknownSignatureVersionError,
)
from botocore.loaders import Loader
from botocore.regions import EndpointRulesetResolver
from tests import requires_crt

REGION_TEMPLATE = "{Region}"
REGION_REF = {"ref": "Region"}
BUCKET_ARN_REF = {"ref": "bucketArn"}
PARSE_ARN_FUNC = {
    "fn": "aws.parseArn",
    "argv": [{"ref": "Bucket"}],
    "assign": "bucketArn",
}
STRING_EQUALS_FUNC = {
    "fn": "stringEquals",
    "argv": [
        {
            "fn": "getAttr",
            "argv": [BUCKET_ARN_REF, "region"],
            "assign": "bucketRegion",
        },
        "",
    ],
}
DNS_SUFFIX_TEMPLATE = "{PartitionResults#dnsSuffix}"
URL_TEMPLATE = (
    f"https://{REGION_TEMPLATE}.myGreatService.{DNS_SUFFIX_TEMPLATE}"
)
ENDPOINT_DICT = {
    "url": URL_TEMPLATE,
    "properties": {
        "authSchemes": [
            {
                "signingName": "s3",
                "signingScope": REGION_TEMPLATE,
                "name": "s3v4",
            }
        ],
    },
    "headers": {
        "x-amz-region-set": [
            REGION_REF,
            {
                "fn": "getAttr",
                "argv": [BUCKET_ARN_REF, "region"],
            },
            "us-east-2",
        ],
    },
}
ENDPOINT_AUTH_SCHEMES_DICT = {
    "url": URL_TEMPLATE,
    "properties": {
        "authSchemes": [
            {
                "disableDoubleEncoding": True,
                "name": "foo",
                "signingName": "s3-outposts",
                "signingRegionSet": ["*"],
            },
            {
                "disableDoubleEncoding": True,
                "name": "bar",
                "signingName": "s3-outposts",
                "signingRegion": REGION_TEMPLATE,
            },
        ],
    },
    "headers": {},
}


@pytest.fixture(scope="module")
def loader():
    return Loader()


@pytest.fixture(scope="module")
def partitions(loader):
    return loader.load_data("partitions")


@pytest.fixture(scope="module")
def rule_lib(partitions):
    return RuleSetStandardLibrary(partitions)


@pytest.fixture(scope="module")
def ruleset_dict():
    path = os.path.join(
        os.path.dirname(__file__),
        "data",
        "endpoints",
        "valid-rules",
        "deprecated-param.json",
    )
    with open(path) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def endpoint_provider(ruleset_dict, partitions):
    return EndpointProvider(ruleset_dict, partitions)


@pytest.fixture(scope="module")
def endpoint_rule():
    return EndpointRule(
        endpoint=ENDPOINT_DICT,
        conditions=[
            PARSE_ARN_FUNC,
            {
                "fn": "not",
                "argv": [STRING_EQUALS_FUNC],
            },
            {
                "fn": "aws.partition",
                "argv": [REGION_REF],
                "assign": "PartitionResults",
            },
        ],
    )


def ruleset_testcases():
    filenames = [
        "account-id",
        "array-index",
        "aws-region",
        "default-values",
        "eventbridge",
        "fns",
        "headers",
        "is-virtual-hostable-s3-bucket",
        "local-region-override",
        "parse-arn",
        "parse-url",
        "substring",
        "uri-encode",
        "valid-hostlabel",
    ]
    error_cases = []
    endpoint_cases = []
    base_path = os.path.join(os.path.dirname(__file__), "data", "endpoints")
    for name in filenames:
        with open(os.path.join(base_path, "valid-rules", f"{name}.json")) as f:
            ruleset = json.load(f)
        with open(os.path.join(base_path, "test-cases", f"{name}.json")) as f:
            tests = json.load(f)

        for test in tests["testCases"]:
            input_params = test["params"]
            expected_object = test["expect"]
            if "error" in expected_object:
                error_cases.append(
                    (ruleset, input_params, expected_object["error"])
                )
            elif "endpoint" in expected_object:
                endpoint_cases.append(
                    (ruleset, input_params, expected_object["endpoint"])
                )
            else:
                raise ValueError("Expected `error` or `endpoint` in test case")
    return error_cases, endpoint_cases


ERROR_TEST_CASES, ENDPOINT_TEST_CASES = ruleset_testcases()


@pytest.mark.parametrize(
    "ruleset,input_params,expected_error",
    ERROR_TEST_CASES,
)
def test_endpoint_resolution_raises(
    partitions, ruleset, input_params, expected_error
):
    endpoint_provider = EndpointProvider(ruleset, partitions)
    with pytest.raises(EndpointResolutionError) as exc_info:
        endpoint_provider.resolve_endpoint(**input_params)
    assert str(exc_info.value) == expected_error


@pytest.mark.parametrize(
    "ruleset,input_params,expected_endpoint",
    ENDPOINT_TEST_CASES,
)
def test_endpoint_resolution(
    partitions, ruleset, input_params, expected_endpoint
):
    endpoint_provider = EndpointProvider(ruleset, partitions)
    endpoint = endpoint_provider.resolve_endpoint(**input_params)
    assert endpoint.url == expected_endpoint["url"]
    assert endpoint.properties == expected_endpoint.get("properties", {})
    assert endpoint.headers == expected_endpoint.get("headers", {})


def test_none_returns_default_partition(rule_lib):
    partition_dict = rule_lib.aws_partition(None)
    assert partition_dict['name'] == "aws"


def test_no_match_region_returns_default_partition(rule_lib):
    partition_dict = rule_lib.aws_partition("invalid-region-42")
    assert partition_dict['name'] == "aws"


def test_invalid_arn_returns_none(rule_lib):
    assert rule_lib.aws_parse_arn("arn:aws:this-is-not-an-arn:foo") is None


@pytest.mark.parametrize(
    "arn, expected_resource_id",
    [
        (
            "arn:aws:s3:::myBucket/key",
            {
                "partition": "aws",
                "service": "s3",
                "region": "",
                "accountId": "",
                "resourceId": ["myBucket", "key"],
            },
        ),
        (
            "arn:aws:kinesis:us-east-1:1234567890:stream/mystream:foo",
            {
                "partition": "aws",
                "service": "kinesis",
                "region": "us-east-1",
                "accountId": "1234567890",
                "resourceId": ["stream", "mystream", "foo"],
            },
        ),
        (
            "arn:aws:s3:::myBucket",
            {
                "partition": "aws",
                "service": "s3",
                "region": "",
                "accountId": "",
                "resourceId": ["myBucket"],
            },
        ),
    ],
)
def test_parse_arn_delimiters(rule_lib, arn, expected_resource_id):
    parsed_arn = rule_lib.aws_parse_arn(arn)
    assert parsed_arn == expected_resource_id


def test_uri_encode_none_returns_none(rule_lib):
    assert rule_lib.uri_encode(None) is None


def test_parse_url_none_return_none(rule_lib):
    assert rule_lib.parse_url(None) is None


def test_string_equals_wrong_type_raises(rule_lib):
    with pytest.raises(EndpointResolutionError) as exc_info:
        rule_lib.string_equals(1, 2)
    assert "Both values must be strings" in str(exc_info.value)


def test_boolean_equals_wrong_type_raises(rule_lib):
    with pytest.raises(EndpointResolutionError) as exc_info:
        rule_lib.boolean_equals(1, 2)
    assert "Both arguments must be bools" in str(exc_info.value)


def test_substring_wrong_type_raises(rule_lib):
    with pytest.raises(EndpointResolutionError) as exc_info:
        rule_lib.substring(["h", "e", "l", "l", "o"], 0, 5, False)
    assert "Input must be a string" in str(exc_info.value)


def test_creator_unknown_type_raises():
    with pytest.raises(EndpointResolutionError) as exc_info:
        RuleCreator.create(type="foo")
    assert "Unknown rule type: foo." in str(exc_info.value)


def test_parameter_wrong_type_raises(endpoint_provider):
    param = endpoint_provider.ruleset.parameters["Region"]
    with pytest.raises(EndpointResolutionError) as exc_info:
        param.validate_input(1)
    assert "Value (Region) is the wrong type" in str(exc_info.value)


def test_deprecated_parameter_logs(endpoint_provider, caplog):
    caplog.set_level(logging.INFO)
    param = endpoint_provider.ruleset.parameters["Region"]
    param.validate_input("foo")
    assert "Region has been deprecated." in caplog.text


def test_no_endpoint_found_error(endpoint_provider):
    with pytest.raises(EndpointResolutionError) as exc_info:
        endpoint_provider.resolve_endpoint(
            **{"Endpoint": "mygreatendpoint.com", "Bucket": "mybucket"}
        )
    assert "No endpoint found for parameters" in str(exc_info.value)


@pytest.mark.parametrize(
    "rule_dict,expected_rule_type",
    [
        (
            {
                "type": "endpoint",
                "conditions": [],
                "endpoint": {
                    "url": (
                        "https://{Region}.myGreatService."
                        "{PartitionResult#dualStackDnsSuffix}"
                    ),
                    "properties": {},
                    "headers": {},
                },
            },
            EndpointRule,
        ),
        (
            {
                "type": "error",
                "conditions": [],
                "error": (
                    "Dualstack is enabled but this partition "
                    "does not support DualStack"
                ),
            },
            ErrorRule,
        ),
        ({"type": "tree", "conditions": [], "rules": []}, TreeRule),
    ],
)
def test_rule_creation(rule_dict, expected_rule_type):
    rule = RuleCreator.create(**rule_dict)
    assert isinstance(rule, expected_rule_type)


def test_assign_existing_scope_var_raises(rule_lib):
    rule = EndpointRule(
        conditions=[
            {
                'fn': 'aws.parseArn',
                'argv': ['{Bucket}'],
                'assign': 'bucketArn',
            },
            {
                'fn': 'aws.parseArn',
                'argv': ['{Bucket}'],
                'assign': 'bucketArn',
            },
        ],
        endpoint={'url': 'foo.bar'},
    )
    with pytest.raises(EndpointResolutionError) as exc_info:
        rule.evaluate_conditions(
            scope_vars={
                'Bucket': 'arn:aws:s3:us-east-1:123456789012:mybucket'
            },
            rule_lib=rule_lib,
        )
    assert str(exc_info.value) == (
        "Assignment bucketArn already exists in "
        "scoped variables and cannot be overwritten"
    )


def test_ruleset_unknown_parameter_type_raises(partitions):
    with pytest.raises(EndpointResolutionError) as exc_info:
        RuleSet(
            version='1.0',
            parameters={
                'Bucket': {"type": "list"},
            },
            rules=[],
            partitions=partitions,
        )
    assert "Unknown parameter type: list." in str(exc_info.value)


@pytest.fixture()
def empty_resolver():
    return EndpointRulesetResolver(
        endpoint_ruleset_data={
            'version': '1.0',
            'parameters': {},
            'rules': [],
        },
        partition_data={},
        service_model=Mock(service_name='test'),
        builtins={},
        client_context=None,
        event_emitter=None,
        use_ssl=True,
        requested_auth_scheme=None,
    )


def test_auth_schemes_conversion_sigv4(empty_resolver):
    auth_schemes = [
        {
            'name': 'sigv4',
            'signingName': 'dynamodb',
            'signingRegion': 'my-region-1',
            'disableDoubleEncoding': True,
            'otherParameter': 'otherValue',
        }
    ]
    at, sc = empty_resolver.auth_schemes_to_signing_ctx(auth_schemes)
    assert at == 'v4'
    assert sc == {
        'region': 'my-region-1',
        'signing_name': 'dynamodb',
        'disableDoubleEncoding': True,
    }


@requires_crt()
def test_auth_schemes_conversion_sigv4a_with_crt(monkeypatch, empty_resolver):
    monkeypatch.setattr('botocore.regions.HAS_CRT', True)
    auth_schemes = [
        {'name': 'sigv4a', 'signingName': 's3', 'signingRegionSet': ['*']}
    ]
    at, sc = empty_resolver.auth_schemes_to_signing_ctx(auth_schemes)
    assert at == 'v4a'
    assert sc == {'region': '*', 'signing_name': 's3'}


def test_auth_schemes_conversion_sigv4a_without_crt(
    monkeypatch, empty_resolver
):
    monkeypatch.setattr('botocore.regions.HAS_CRT', False)
    monkeypatch.setattr('botocore.regions.AUTH_TYPE_MAPS', {})
    auth_schemes = [
        {'name': 'sigv4a', 'signingName': 's3', 'signingRegionSet': ['*']}
    ]
    with pytest.raises(MissingDependencyException):
        empty_resolver.auth_schemes_to_signing_ctx(auth_schemes)


def test_auth_schemes_conversion_no_known_auth_types(empty_resolver):
    auth_schemes = [
        {'name': 'foo', 'signingName': 's3', 'signingRegion': 'ap-south-2'},
        {'name': 'bar', 'otherParamKey': 'otherParamVal'},
    ]
    with pytest.raises(UnknownSignatureVersionError):
        empty_resolver.auth_schemes_to_signing_ctx(auth_schemes)


def test_auth_schemes_conversion_first_authtype_unknown(
    monkeypatch, empty_resolver
):
    monkeypatch.setattr('botocore.regions.HAS_CRT', False)
    monkeypatch.setattr('botocore.regions.AUTH_TYPE_MAPS', {'bar': None})
    auth_schemes = [
        {'name': 'foo', 'signingName': 's3', 'signingRegion': 'ap-south-1'},
        {'name': 'bar', 'signingName': 's3', 'signingRegion': 'ap-south-2'},
    ]
    at, sc = empty_resolver.auth_schemes_to_signing_ctx(auth_schemes)
    assert at == 'bar'
    assert sc == {'region': 'ap-south-2', 'signing_name': 's3'}


def test_endpoint_resolution_caches(endpoint_provider, monkeypatch):
    mock_evaluate = Mock()
    monkeypatch.setattr(RuleSet, "evaluate", mock_evaluate)
    for _ in range(5):
        endpoint_provider.resolve_endpoint(Region="us-east-2")
    mock_evaluate.assert_called_once_with({"Region": "us-east-2"})


def test_endpoint_reevaluates_result(endpoint_provider, monkeypatch):
    regions = ["us-east-1", "us-west-2"]
    mock_evaluate = Mock()
    monkeypatch.setattr(RuleSet, "evaluate", mock_evaluate)
    for region in regions:
        endpoint_provider.resolve_endpoint(Region=region)
    assert mock_evaluate.call_count == 2


@pytest.mark.parametrize(
    "bucket, expected_value",
    [
        ("mybucket", True),
        ("ab", False),
        ("a.b", True),
        ("my.great.bucket.aws.com", True),
        ("mY.GREAT.bucket.aws.com", False),
        ("192.168.1.1", False),
    ],
)
def test_aws_is_virtual_hostable_s3_bucket_allow_subdomains(
    rule_lib, bucket, expected_value
):
    assert (
        rule_lib.aws_is_virtual_hostable_s3_bucket(bucket, True)
        == expected_value
    )


@pytest.mark.parametrize(
    "value, path, expected_value",
    [
        ({"foo": ['bar']}, 'baz[0]', None),  # Missing index
        ({"foo": ['bar']}, 'foo[1]', None),  # Out of range index
        ({"foo": ['bar']}, 'foo[0]', "bar"),  # Named index
        (("foo",), '[0]', "foo"),  # Bare index
        ({"foo": {}}, 'foo.bar[0]', None),  # Missing index from split path
        (
            {"foo": {'bar': []}},
            'foo.bar[0]',
            None,
        ),  # Out of range from split path
        (
            {"foo": {"bar": "baz"}},
            'foo.bar',
            "baz",
        ),  # Split path with named index
        (
            {"foo": {"bar": ["baz"]}},
            'foo.bar[0]',
            "baz",
        ),  # Split path with numeric index
    ],
)
def test_get_attr(rule_lib, value, path, expected_value):
    result = rule_lib.get_attr(value, path)
    assert result == expected_value


@pytest.mark.parametrize(
    "use_ssl, endpoint_url, provider_params, expected_url",
    [
        # use_ssl=True, endpoint_url="http://..." → HTTP
        (
            True,
            'http://custom.com',
            {'Endpoint': 'http://custom.com'},
            'http://custom.com',
        ),
        # use_ssl=True, endpoint_url="https://..." → HTTPS
        (
            True,
            'https://custom.com',
            {'Endpoint': 'https://custom.com'},
            'https://custom.com',
        ),
        # use_ssl=False, endpoint_url="http://..." → HTTP
        (
            False,
            'http://custom.com',
            {'Endpoint': 'http://custom.com'},
            'http://custom.com',
        ),
        # use_ssl=False, endpoint_url="https://..." → HTTPS
        (
            False,
            'https://custom.com',
            {'Endpoint': 'https://custom.com'},
            'https://custom.com',
        ),
        # use_ssl=True, no endpoint → HTTPS
        (
            True,
            'https://s3-test-only-domain.amazonaws.com',
            {},
            'https://s3-test-only-domain.amazonaws.com',
        ),
        # use_ssl=False, no endpoint → HTTP (downgrade)
        (
            False,
            'https://s3-test-only-domain.amazonaws.com',
            {},
            'http://s3-test-only-domain.amazonaws.com',
        ),
    ],
)
def test_construct_endpoint_parametrized(
    use_ssl, endpoint_url, provider_params, expected_url
):
    resolver = EndpointRulesetResolver(
        endpoint_ruleset_data={
            'version': '1.0',
            'parameters': {},
            'rules': [],
        },
        partition_data={},
        service_model=Mock(service_name='test'),
        builtins={},
        client_context=None,
        event_emitter=None,
        use_ssl=use_ssl,
        requested_auth_scheme=None,
    )

    with patch.object(resolver._provider, 'resolve_endpoint') as mock_resolve:
        mock_resolve.return_value = RuleSetEndpoint(
            url=endpoint_url, properties={}, headers={}
        )
        with patch.object(
            resolver, '_get_provider_params', return_value=provider_params
        ):
            result = resolver.construct_endpoint(None, None, None)
            assert result.url == expected_url


@pytest.mark.parametrize(
    "auth_scheme_preference,expected_auth_scheme_name",
    [
        (
            'foo,bar',
            'foo',
        ),
        (
            'bar,foo',
            'bar',
        ),
        (
            'xyz,foo,bar',
            'foo',
        ),
    ],
)
def test_auth_scheme_preference(
    auth_scheme_preference, expected_auth_scheme_name, monkeypatch
):
    conditions = (
        [
            PARSE_ARN_FUNC,
            {
                "fn": "not",
                "argv": [STRING_EQUALS_FUNC],
            },
            {
                "fn": "aws.partition",
                "argv": [REGION_REF],
                "assign": "PartitionResults",
            },
        ],
    )
    resolver = EndpointRulesetResolver(
        endpoint_ruleset_data={
            'version': '1.0',
            'parameters': {},
            'rules': [
                {
                    'conditions': conditions,
                    'type': 'endpoint',
                    'endpoint': ENDPOINT_AUTH_SCHEMES_DICT,
                }
            ],
        },
        partition_data={},
        service_model=Mock(service_name='test'),
        builtins={},
        client_context=None,
        event_emitter=None,
        use_ssl=True,
        requested_auth_scheme=None,
        auth_scheme_preference=auth_scheme_preference,
    )
    auth_schemes = [
        {'name': 'foo', 'signingName': 's3', 'signingRegion': 'ap-south-1'},
        {'name': 'bar', 'signingName': 's3', 'signingRegion': 'ap-south-2'},
    ]
    with (
        patch.dict(
            'botocore.auth.AUTH_TYPE_MAPS',
            {'bar': None, 'foo': None},
            clear=True,
        ),
        patch.dict(
            'botocore.auth.AUTH_PREF_TO_SIGNATURE_VERSION',
            {'bar': 'bar', 'foo': 'foo'},
            clear=True,
        ),
    ):
        name, scheme = resolver.auth_schemes_to_signing_ctx(auth_schemes)
    assert name == expected_auth_scheme_name


SPECIALIZABLE_RULESET_DICT = {
    "version": "1.0",
    "parameters": {
        "Region": {"type": "string", "required": True},
        "UseFIPS": {"type": "boolean", "default": False},
        "UseDualStack": {"type": "boolean", "default": False},
        "Endpoint": {"type": "string"},
        "Bucket": {"type": "string"},
    },
    "rules": [
        {
            "type": "tree",
            "conditions": [
                {"fn": "isSet", "argv": [{"ref": "Endpoint"}]},
            ],
            "rules": [
                {
                    "type": "endpoint",
                    "conditions": [
                        {
                            "fn": "parseUrl",
                            "argv": [{"ref": "Endpoint"}],
                            "assign": "parsedEndpoint",
                        },
                        {
                            "fn": "isValidHostLabel",
                            "argv": [
                                {
                                    "fn": "getAttr",
                                    "argv": [
                                        {"ref": "parsedEndpoint"},
                                        "authority",
                                    ],
                                },
                                False,
                            ],
                        },
                    ],
                    "endpoint": {
                        "url": "{Endpoint}",
                        "headers": {"x-endpoint": [{"ref": "Endpoint"}]},
                    },
                },
                {
                    "type": "error",
                    "conditions": [],
                    "error": "Invalid endpoint: {Endpoint}",
                },
            ],
        },
        {
            "type": "tree",
            "conditions": [
                {
                    "fn": "aws.partition",
                    "argv": [{"ref": "Region"}],
                    "assign": "Partition",
                },
                {"fn": "booleanEquals", "argv": [{"ref": "UseFIPS"}, True]},
            ],
            "rules": [
                {
                    "type": "error",
                    "conditions": [
                        {
                            "fn": "booleanEquals",
                            "argv": [{"ref": "UseDualStack"}, True],
                        },
                    ],
                    "error": "FIPS and dualstack are not supported",
                },
                {
                    "type": "endpoint",
                    "conditions": [],
                    "endpoint": {
                        "url": "https://{Region}.fips.{Partition#dnsSuffix}",
                        "properties": {
                            "authSchemes": [
                                {
                                    "name": "sigv4",
                                    "signingName": "myservice",
                                    "signingRegion": "{Region}",
                                }
                            ]
                        },
                        "headers": {"x-region": [{"ref": "Region"}]},
                    },
                },
            ],
        },
        {
            "type": "tree",
            "conditions": [
                {
                    "fn": "aws.partition",
                    "argv": [{"ref": "Region"}],
                    "assign": "Partition",
                },
            ],
            "rules": [
                {
                    "type": "endpoint",
                    "conditions": [
                        {
                            "fn": "booleanEquals",
                            "argv": [{"ref": "UseDualStack"}, True],
                        },
                    ],
                    "endpoint": {
                        "url": (
                            "https://{Region}.dualstack.{Partition#dnsSuffix}"
                        ),
                        "properties": {
                            "authSchemes": [
                                {
                                    "name": "sigv4",
                                    "signingName": "myservice",
                                    "signingRegion": "{Region}",
                                }
                            ]
                        },
                    },
                },
                {
                    "type": "endpoint",
                    "conditions": [
                        {"fn": "isSet", "argv": [{"ref": "Bucket"}]},
                    ],
                    "endpoint": {
                        "url": (
                            "https://{Bucket}.{Region}.{Partition#dnsSuffix}"
                        ),
                        "headers": {"x-bucket": [{"ref": "Bucket"}]},
                    },
                },
                {
                    "type": "error",
                    "conditions": [],
                    "error": "No endpoint for region {Region}",
                },
            ],
        },
    ],
}


@pytest.fixture()
def specializable_provider(partitions):
    return EndpointProvider(
        json.loads(json.dumps(SPECIALIZABLE_RULESET_DICT)), partitions
    )


def iter_rules(rules):
    for rule in rules:
        yield rule
        if isinstance(rule, TreeRule):
            yield from iter_rules(rule.rules)


def count_rule_conditions(rules):
    return sum(len(rule.conditions) for rule in iter_rules(rules))


def snapshot_ruleset_structure(rules):
    """Extract a comparable, deepcopy-safe view of a list of rules."""
    snapshot = []
    for rule in rules:
        entry = {
            "type": type(rule).__name__,
            "conditions": json.loads(json.dumps(rule.conditions)),
            "documentation": rule.documentation,
            "bound_vars": dict(rule.bound_vars),
        }
        if isinstance(rule, TreeRule):
            entry["rules"] = snapshot_ruleset_structure(rule.rules)
        if isinstance(rule, EndpointRule):
            entry["endpoint"] = json.loads(json.dumps(rule.endpoint))
        if isinstance(rule, ErrorRule):
            entry["error"] = rule.error
        snapshot.append(entry)
    return snapshot


def count_evaluated_conditions(ruleset, input_params):
    """Evaluate a ruleset, counting the rule conditions actually executed."""
    condition_ids = {
        id(condition)
        for rule in iter_rules(ruleset.rules)
        for condition in rule.conditions
    }
    evaluated = []
    rule_lib = ruleset.rule_lib
    original_call_function = rule_lib.call_function

    def counting_call_function(func_signature, scope_vars):
        if id(func_signature) in condition_ids:
            evaluated.append(func_signature)
        return original_call_function(func_signature, scope_vars)

    rule_lib.call_function = counting_call_function
    try:
        try:
            ruleset.evaluate(dict(input_params))
        except EndpointResolutionError:
            pass
    finally:
        del rule_lib.call_function
    return evaluated


SPECIALIZATION_FIXED_PARAMS = {
    "Region": "us-east-1",
    "UseFIPS": False,
    "UseDualStack": False,
}

SPECIALIZATION_REMAINING_PARAMS = [
    {},
    {"Bucket": "mybucket"},
    {"Endpoint": "https://custom.example.com"},
    {"Endpoint": "not a url"},
]


@pytest.mark.parametrize(
    "remaining_params", SPECIALIZATION_REMAINING_PARAMS
)
def test_specialized_provider_matches_original(
    specializable_provider, remaining_params
):
    full_params = {**SPECIALIZATION_FIXED_PARAMS, **remaining_params}
    residual_provider = specializable_provider.specialize(
        **SPECIALIZATION_FIXED_PARAMS
    )
    try:
        expected = specializable_provider.resolve_endpoint(**full_params)
    except EndpointResolutionError as ex:
        with pytest.raises(EndpointResolutionError) as exc_info:
            residual_provider.resolve_endpoint(**remaining_params)
        assert str(exc_info.value) == str(ex)
    else:
        endpoint = residual_provider.resolve_endpoint(**remaining_params)
        assert endpoint.url == expected.url
        assert endpoint.properties == expected.properties
        assert endpoint.headers == expected.headers


def test_specialized_provider_preserves_error_branch(
    specializable_provider,
):
    fixed = {"Region": "us-east-1", "UseFIPS": True, "UseDualStack": True}
    residual_provider = specializable_provider.specialize(**fixed)
    with pytest.raises(EndpointResolutionError) as exc_info:
        specializable_provider.resolve_endpoint(**fixed)
    expected_error = str(exc_info.value)
    with pytest.raises(EndpointResolutionError) as exc_info:
        residual_provider.resolve_endpoint()
    assert str(exc_info.value) == expected_error


def test_specialize_folds_conditions_and_rules(specializable_provider):
    residual = specializable_provider.specialize(**SPECIALIZATION_FIXED_PARAMS)
    original_ruleset = specializable_provider.ruleset
    residual_ruleset = residual.ruleset
    residual_count = count_rule_conditions(residual_ruleset.rules)
    original_count = count_rule_conditions(original_ruleset.rules)
    assert residual_count < original_count
    # the FIPS subtree can never match with UseFIPS=False and is removed
    residual_urls = [
        rule.endpoint["url"]
        for rule in iter_rules(residual_ruleset.rules)
        if isinstance(rule, EndpointRule)
    ]
    assert not any(".fips." in url for url in residual_urls)
    # fixed parameters are removed from the residual parameter spec
    assert not set(SPECIALIZATION_FIXED_PARAMS) & set(
        residual_ruleset.parameters
    )
    assert set(residual_ruleset.parameters) == {"Endpoint", "Bucket"}


def test_specialize_does_not_reevaluate_folded_conditions(
    specializable_provider,
):
    residual = specializable_provider.specialize(**SPECIALIZATION_FIXED_PARAMS)
    remaining_params = {"Bucket": "mybucket"}
    full_params = {**SPECIALIZATION_FIXED_PARAMS, **remaining_params}

    original_evaluated = count_evaluated_conditions(
        specializable_provider.ruleset, full_params
    )
    residual_evaluated = count_evaluated_conditions(
        residual.ruleset, remaining_params
    )

    original_fns = [cond["fn"] for cond in original_evaluated]
    residual_fns = [cond["fn"] for cond in residual_evaluated]
    # the residual ruleset executes strictly fewer conditions
    assert len(residual_evaluated) < len(original_evaluated)
    # conditions depending only on fixed parameters are never re-evaluated
    assert "aws.partition" in original_fns
    assert "aws.partition" not in residual_fns
    assert "booleanEquals" not in residual_fns
    # conditions depending on remaining parameters are still evaluated
    assert "isSet" in residual_fns
    # repeated evaluation of the residual ruleset stays folded
    reevaluated = count_evaluated_conditions(
        residual.ruleset, remaining_params
    )
    assert [cond["fn"] for cond in reevaluated] == residual_fns


def test_specialize_keeps_unknown_params_unfolded(specializable_provider):
    # only Region is fixed; UseFIPS/UseDualStack/Endpoint/Bucket are unknown
    residual = specializable_provider.specialize(Region="us-east-1")
    residual_fns = [
        condition["fn"]
        for rule in iter_rules(residual.ruleset.rules)
        for condition in rule.conditions
    ]
    # conditions on unknown values are kept: they must not be treated as
    # None or False during specialization
    assert "isSet" in residual_fns
    assert "booleanEquals" in residual_fns
    # aws.partition depends only on Region and is folded away everywhere
    assert "aws.partition" not in residual_fns

    # the unknown boolean parameters still drive branch selection
    endpoint = residual.resolve_endpoint(UseFIPS=True, UseDualStack=False)
    assert endpoint.url == "https://us-east-1.fips.amazonaws.com"
    with pytest.raises(EndpointResolutionError) as exc_info:
        residual.resolve_endpoint(UseFIPS=True, UseDualStack=True)
    assert "FIPS and dualstack are not supported" in str(exc_info.value)
    endpoint = residual.resolve_endpoint(
        UseFIPS=False, UseDualStack=False, Bucket="mybucket"
    )
    assert endpoint.url == "https://mybucket.us-east-1.amazonaws.com"


def test_specialize_does_not_modify_original_ruleset(
    specializable_provider,
):
    ruleset = specializable_provider.ruleset
    before = snapshot_ruleset_structure(ruleset.rules)
    specializable_provider.specialize(**SPECIALIZATION_FIXED_PARAMS)
    after = snapshot_ruleset_structure(ruleset.rules)
    assert before == after
    assert all(rule.bound_vars == {} for rule in iter_rules(ruleset.rules))
    # the original provider still resolves other parameter values
    endpoint = specializable_provider.resolve_endpoint(
        Region="eu-west-1", UseFIPS=False, UseDualStack=False, Bucket="bucket"
    )
    assert endpoint.url == "https://bucket.eu-west-1.amazonaws.com"


def test_specialize_ruleset_returns_reusable_residual(
    specializable_provider,
):
    ruleset = specializable_provider.ruleset
    residual_ruleset = ruleset.specialize({"Region": "us-east-1"})
    assert isinstance(residual_ruleset, RuleSet)
    assert residual_ruleset is not ruleset
    assert residual_ruleset.bound_vars == {"Region": "us-east-1"}
    assert "Region" not in residual_ruleset.parameters
    assert "Region" in ruleset.parameters
    # the residual ruleset can be evaluated repeatedly
    for bucket in ("bucket-one", "bucket-two"):
        endpoint = residual_ruleset.evaluate(
            {"UseFIPS": False, "UseDualStack": False, "Bucket": bucket}
        )
        assert endpoint.url == f"https://{bucket}.us-east-1.amazonaws.com"


def test_specialize_applies_default_for_none_value(specializable_provider):
    residual = specializable_provider.specialize(
        Region="us-east-1", UseFIPS=None
    )
    # UseFIPS=None resolves to its default (False), so the FIPS subtree
    # is folded away
    residual_urls = [
        rule.endpoint["url"]
        for rule in iter_rules(residual.ruleset.rules)
        if isinstance(rule, EndpointRule)
    ]
    assert not any(".fips." in url for url in residual_urls)


def test_specialize_missing_required_param_raises(specializable_provider):
    with pytest.raises(EndpointResolutionError):
        specializable_provider.specialize(Region=None)


def test_specialize_wrong_param_type_raises(specializable_provider):
    with pytest.raises(EndpointResolutionError):
        specializable_provider.specialize(
            Region="us-east-1", UseFIPS="not-a-bool"
        )


def test_specialize_preserves_excluded_params(
    specializable_provider, partitions
):
    provider = EndpointProvider(
        json.loads(json.dumps(SPECIALIZABLE_RULESET_DICT)),
        partitions,
        excluded_params=frozenset({"Bucket"}),
    )
    residual = provider.specialize(Region="us-east-1")
    assert residual._excluded_params == provider._excluded_params
