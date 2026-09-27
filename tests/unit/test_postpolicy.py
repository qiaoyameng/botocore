# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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
from botocore.exceptions import (
    ContradictoryConditionsError,
    MissingFieldConditionError,
    UnknownConditionTypeError,
)
from botocore.postpolicy import validate_post_policy
from tests import unittest


class TestValidatePostPolicy(unittest.TestCase):
    def test_valid_policy_passes(self):
        validate_post_policy(
            fields={'key': 'mykey', 'acl': 'public-read'},
            conditions=[
                {'bucket': 'mybucket'},
                {'key': 'mykey'},
                {'acl': 'public-read'},
                ['starts-with', '$Content-Type', 'image/'],
                ['content-length-range', 2, 5],
            ],
        )

    def test_empty_fields_and_conditions_pass(self):
        validate_post_policy(fields={}, conditions=[])

    def test_eq_condition_matching_field_passes(self):
        validate_post_policy(
            fields={'acl': 'private'},
            conditions=[['eq', '$acl', 'private']],
        )

    def test_dict_conditions_with_same_value_pass(self):
        validate_post_policy(
            fields={},
            conditions=[{'bucket': 'mybucket'}, {'bucket': 'mybucket'}],
        )

    def test_conflicting_dict_conditions(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={},
                conditions=[{'bucket': 'one'}, {'bucket': 'two'}],
            )

    def test_conflicting_eq_and_dict_conditions(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={},
                conditions=[{'bucket': 'one'}, ['eq', '$bucket', 'two']],
            )

    def test_field_value_conflicts_with_condition(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={'acl': 'private'},
                conditions=[{'acl': 'public-read'}],
            )

    def test_eq_value_must_satisfy_starts_with(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={},
                conditions=[
                    {'key': 'mykey'},
                    ['starts-with', '$key', 'other'],
                ],
            )

    def test_eq_value_satisfying_starts_with_passes(self):
        validate_post_policy(
            fields={},
            conditions=[
                {'key': 'mykey'},
                ['starts-with', '$key', 'my'],
            ],
        )

    def test_compatible_prefixes_pass(self):
        validate_post_policy(
            fields={},
            conditions=[
                ['starts-with', '$key', 'my'],
                ['starts-with', '$key', 'mykey'],
            ],
        )

    def test_compatible_prefixes_reversed_pass(self):
        validate_post_policy(
            fields={},
            conditions=[
                ['starts-with', '$key', 'mykey'],
                ['starts-with', '$key', 'my'],
            ],
        )

    def test_incompatible_prefixes(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={},
                conditions=[
                    ['starts-with', '$key', 'foo'],
                    ['starts-with', '$key', 'bar'],
                ],
            )

    def test_overlapping_content_length_ranges_pass(self):
        validate_post_policy(
            fields={},
            conditions=[
                ['content-length-range', 1, 10],
                ['content-length-range', 5, 20],
            ],
        )

    def test_disjoint_content_length_ranges(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={},
                conditions=[
                    ['content-length-range', 1, 5],
                    ['content-length-range', 10, 20],
                ],
            )

    def test_inverted_content_length_range(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={},
                conditions=[['content-length-range', 10, 5]],
            )

    def test_unknown_condition_operator(self):
        with self.assertRaises(UnknownConditionTypeError):
            validate_post_policy(
                fields={},
                conditions=[['ends-with', '$key', 'mykey']],
            )

    def test_malformed_condition_type(self):
        with self.assertRaises(UnknownConditionTypeError):
            validate_post_policy(fields={}, conditions=['key'])

    def test_empty_condition_list(self):
        with self.assertRaises(UnknownConditionTypeError):
            validate_post_policy(fields={}, conditions=[[]])

    def test_eq_condition_without_field_reference(self):
        with self.assertRaises(UnknownConditionTypeError):
            validate_post_policy(
                fields={},
                conditions=[['eq', 'key', 'mykey']],
            )

    def test_eq_condition_wrong_arity(self):
        with self.assertRaises(UnknownConditionTypeError):
            validate_post_policy(
                fields={},
                conditions=[['eq', '$key']],
            )

    def test_content_length_range_wrong_arity(self):
        with self.assertRaises(UnknownConditionTypeError):
            validate_post_policy(
                fields={},
                conditions=[['content-length-range', 1]],
            )

    def test_content_length_range_non_integer_bounds(self):
        with self.assertRaises(UnknownConditionTypeError):
            validate_post_policy(
                fields={},
                conditions=[['content-length-range', 'a', 'b']],
            )

    def test_field_missing_condition(self):
        with self.assertRaises(MissingFieldConditionError):
            validate_post_policy(
                fields={'acl': 'public-read'},
                conditions=[{'bucket': 'mybucket'}],
            )

    def test_exempt_fields_do_not_require_conditions(self):
        validate_post_policy(
            fields={
                'file': 'contents',
                'policy': 'somepolicy',
                'x-amz-signature': 'somesig',
                'x-ignore-anything': 'whatever',
            },
            conditions=[],
        )

    def test_filename_placeholder_treated_as_prefix(self):
        # ${filename} is a placeholder, so any key under the prefix
        # satisfies the policy.
        validate_post_policy(
            fields={'key': 'myprefix/${filename}'},
            conditions=[
                ['starts-with', '$key', 'myprefix/'],
                {'key': 'myprefix/myfile.txt'},
            ],
        )

    def test_filename_placeholder_conflicting_user_condition(self):
        with self.assertRaises(ContradictoryConditionsError):
            validate_post_policy(
                fields={'key': 'myprefix/${filename}'},
                conditions=[
                    ['starts-with', '$key', 'myprefix/'],
                    {'key': 'other/file.txt'},
                ],
            )

    def test_sensitive_field_values_not_in_error_message(self):
        secret = 'AKIAIOSFODNN7EXAMPLE/20140310/us-east-1/s3/aws4_request'
        with self.assertRaises(ContradictoryConditionsError) as ctx:
            validate_post_policy(
                fields={'x-amz-credential': secret},
                conditions=[{'x-amz-credential': 'tampered'}],
            )
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn('tampered', str(ctx.exception))

    def test_security_token_not_in_error_message(self):
        token = 'AQoDYXdzEPT//////////wEXAMPLE'
        with self.assertRaises(ContradictoryConditionsError) as ctx:
            validate_post_policy(
                fields={'x-amz-security-token': token},
                conditions=[{'x-amz-security-token': 'othertoken'}],
            )
        self.assertNotIn(token, str(ctx.exception))
        self.assertNotIn('othertoken', str(ctx.exception))
