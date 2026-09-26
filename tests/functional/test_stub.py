# Copyright 2016 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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
import threading
from unittest import mock

import botocore
import botocore.client
import botocore.config
import botocore.retryhandler
import botocore.session
import botocore.stub as stub
import botocore.translate
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ParamValidationError,
    StubAssertionError,
    StubResponseError,
    UnStubbedResponseError,
)
from botocore.stub import Stubber
from tests import unittest


class TestStubber(unittest.TestCase):
    def setUp(self):
        session = botocore.session.get_session()
        config = botocore.config.Config(
            signature_version=botocore.UNSIGNED,
            s3={'addressing_style': 'path'},
        )
        self.client = session.create_client(
            's3', region_name='us-east-1', config=config
        )
        self.stubber = Stubber(self.client)

    def test_stubber_returns_response(self):
        service_response = {'ResponseMetadata': {'foo': 'bar'}}
        self.stubber.add_response('list_objects', service_response)
        self.stubber.activate()
        response = self.client.list_objects(Bucket='foo')
        self.assertEqual(response, service_response)

    def test_context_manager_returns_response(self):
        service_response = {'ResponseMetadata': {'foo': 'bar'}}
        self.stubber.add_response('list_objects', service_response)

        with self.stubber:
            response = self.client.list_objects(Bucket='foo')
        self.assertEqual(response, service_response)

    def test_activated_stubber_errors_with_no_registered_stubs(self):
        self.stubber.activate()
        # Params one per line for readability.
        with self.assertRaisesRegex(
            UnStubbedResponseError, "Unexpected API Call"
        ):
            self.client.list_objects(
                Bucket='asdfasdfasdfasdf',
                Delimiter='asdfasdfasdfasdf',
                Prefix='asdfasdfasdfasdf',
                EncodingType='url',
            )

    def test_stubber_errors_when_stubs_are_used_up(self):
        self.stubber.add_response('list_objects', {})
        self.stubber.activate()
        self.client.list_objects(Bucket='foo')

        with self.assertRaises(UnStubbedResponseError):
            self.client.list_objects(Bucket='foo')

    def test_client_error_response(self):
        error_code = "AccessDenied"
        error_message = "Access Denied"
        self.stubber.add_client_error(
            'list_objects', error_code, error_message
        )
        self.stubber.activate()

        with self.assertRaises(ClientError):
            self.client.list_objects(Bucket='foo')

    def test_modeled_client_error_response(self):
        error_code = "InvalidObjectState"
        error_message = "Object is in invalid state"
        modeled_fields = {
            'StorageClass': 'foo',
            'AccessTier': 'bar',
        }
        self.stubber.add_client_error(
            'get_object',
            error_code,
            error_message,
            modeled_fields=modeled_fields,
        )
        self.stubber.activate()

        actual_exception = None
        try:
            self.client.get_object(Bucket='foo', Key='bar')
        except self.client.exceptions.InvalidObjectState as e:
            actual_exception = e
        self.assertIsNotNone(actual_exception)
        response = actual_exception.response
        self.assertEqual(response['StorageClass'], 'foo')
        self.assertEqual(response['AccessTier'], 'bar')

    def test_modeled_client_error_response_validation_error(self):
        error_code = "InvalidObjectState"
        error_message = "Object is in invalid state"
        modeled_fields = {
            'BadField': 'fail please',
        }
        with self.assertRaises(ParamValidationError):
            self.stubber.add_client_error(
                'get_object',
                error_code,
                error_message,
                modeled_fields=modeled_fields,
            )

    def test_modeled_client_unknown_code_validation_error(self):
        error_code = "NotARealError"
        error_message = "Message"
        modeled_fields = {
            'BadField': 'fail please',
        }
        with self.assertRaises(ParamValidationError):
            self.stubber.add_client_error(
                'get_object',
                error_code,
                error_message,
                modeled_fields=modeled_fields,
            )

    def test_can_add_expected_params_to_client_error(self):
        self.stubber.add_client_error(
            'list_objects', 'Error', 'error', expected_params={'Bucket': 'foo'}
        )
        self.stubber.activate()
        with self.assertRaises(ClientError):
            self.client.list_objects(Bucket='foo')

    def test_can_expected_param_fails_in_client_error(self):
        self.stubber.add_client_error(
            'list_objects', 'Error', 'error', expected_params={'Bucket': 'foo'}
        )
        self.stubber.activate()
        # We expect an AssertionError instead of a ClientError
        # because we're calling the operation with the wrong
        # param value.
        with self.assertRaises(AssertionError):
            self.client.list_objects(Bucket='wrong-argument-value')

    def test_expected_params_success(self):
        service_response = {}
        expected_params = {'Bucket': 'foo'}
        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )
        self.stubber.activate()
        # This should be called successfully with no errors being thrown
        # for mismatching expected params.
        response = self.client.list_objects(Bucket='foo')
        self.assertEqual(response, service_response)

    def test_expected_params_fail(self):
        service_response = {}
        expected_params = {'Bucket': 'bar'}
        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )
        self.stubber.activate()
        # This should call should raise an for mismatching expected params.
        with self.assertRaisesRegex(StubResponseError, "{'Bucket': 'bar'},\n"):
            self.client.list_objects(Bucket='foo')

    def test_expected_params_mixed_with_errors_responses(self):
        # Add an error response
        error_code = "AccessDenied"
        error_message = "Access Denied"
        self.stubber.add_client_error(
            'list_objects', error_code, error_message
        )

        # Add a response with incorrect expected params
        service_response = {}
        expected_params = {'Bucket': 'bar'}
        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )

        self.stubber.activate()

        # The first call should throw and error as expected.
        with self.assertRaises(ClientError):
            self.client.list_objects(Bucket='foo')

        # The second call should throw an error for unexpected parameters
        with self.assertRaisesRegex(StubResponseError, 'Expected parameters'):
            self.client.list_objects(Bucket='foo')

    def test_can_continue_to_call_after_expected_params_fail(self):
        service_response = {}
        expected_params = {'Bucket': 'bar'}

        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )

        self.stubber.activate()
        # Throw an error for unexpected parameters
        with self.assertRaises(StubResponseError):
            self.client.list_objects(Bucket='foo')

        # The stubber should still have the responses queued up
        # even though the original parameters did not match the expected ones.
        self.client.list_objects(Bucket='bar')
        self.stubber.assert_no_pending_responses()

    def test_still_relies_on_param_validation_with_expected_params(self):
        service_response = {}
        expected_params = {'Buck': 'bar'}

        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )

        self.stubber.activate()
        # Throw an error for invalid parameters
        with self.assertRaises(ParamValidationError):
            self.client.list_objects(Buck='bar')

    def test_any_ignores_param_for_validation(self):
        service_response = {}
        expected_params = {'Bucket': stub.ANY}

        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )
        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )

        try:
            with self.stubber:
                self.client.list_objects(Bucket='foo')
                self.client.list_objects(Bucket='bar')
        except StubAssertionError:
            self.fail("stub.ANY failed to ignore parameter for validation.")

    def test_mixed_any_and_concrete_params(self):
        service_response = {}
        expected_params = {'Bucket': stub.ANY, 'Key': 'foo.txt'}

        self.stubber.add_response(
            'head_object', service_response, expected_params
        )
        self.stubber.add_response(
            'head_object', service_response, expected_params
        )

        try:
            with self.stubber:
                self.client.head_object(Bucket='foo', Key='foo.txt')
                self.client.head_object(Bucket='bar', Key='foo.txt')
        except StubAssertionError:
            self.fail("stub.ANY failed to ignore parameter for validation.")

    def test_nested_any_param(self):
        service_response = {}
        expected_params = {
            'Bucket': 'foo',
            'Key': 'bar.txt',
            'Metadata': {
                'MyMeta': stub.ANY,
            },
        }

        self.stubber.add_response(
            'put_object', service_response, expected_params
        )
        self.stubber.add_response(
            'put_object', service_response, expected_params
        )

        try:
            with self.stubber:
                self.client.put_object(
                    Bucket='foo',
                    Key='bar.txt',
                    Metadata={
                        'MyMeta': 'Foo',
                    },
                )
                self.client.put_object(
                    Bucket='foo',
                    Key='bar.txt',
                    Metadata={
                        'MyMeta': 'Bar',
                    },
                )
        except StubAssertionError:
            self.fail(
                "stub.ANY failed to ignore nested parameter for validation."
            )

    def test_ANY_repr(self):
        self.assertEqual(repr(stub.ANY), '<ANY>')

    def test_none_param(self):
        service_response = {}
        expected_params = {'Buck': None}

        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )

        self.stubber.activate()
        # Throw an error for invalid parameters
        with self.assertRaises(StubAssertionError):
            self.client.list_objects(Buck='bar')

    def test_many_expected_params(self):
        service_response = {}
        expected_params = {
            'Bucket': 'mybucket',
            'Prefix': 'myprefix',
            'Delimiter': '/',
            'EncodingType': 'url',
        }
        self.stubber.add_response(
            'list_objects', service_response, expected_params
        )
        try:
            with self.stubber:
                self.client.list_objects(**expected_params)
        except StubAssertionError:
            self.fail(
                "Stubber inappropriately raised error for same parameters."
            )

    def test_no_stub_for_presign_url(self):
        try:
            with self.stubber:
                url = self.client.generate_presigned_url(
                    ClientMethod='get_object',
                    Params={'Bucket': 'mybucket', 'Key': 'mykey'},
                )
                self.assertEqual(
                    url, 'https://s3.amazonaws.com/mybucket/mykey'
                )
        except StubResponseError:
            self.fail(
                'Stubbed responses should not be required for generating '
                'presigned requests'
            )

    def test_can_stub_with_presign_url_mixed_in(self):
        desired_response = {}
        expected_params = {
            'Bucket': 'mybucket',
            'Prefix': 'myprefix',
        }
        self.stubber.add_response(
            'list_objects', desired_response, expected_params
        )
        with self.stubber:
            url = self.client.generate_presigned_url(
                ClientMethod='get_object',
                Params={'Bucket': 'myotherbucket', 'Key': 'myotherkey'},
            )
            self.assertEqual(
                url, 'https://s3.amazonaws.com/myotherbucket/myotherkey'
            )
            actual_response = self.client.list_objects(**expected_params)
            self.assertEqual(desired_response, actual_response)
        self.stubber.assert_no_pending_responses()

    def test_parse_get_bucket_location(self):
        error_code = "NoSuchBucket"
        error_message = "The specified bucket does not exist"
        self.stubber.add_client_error(
            'get_bucket_location', error_code, error_message
        )
        self.stubber.activate()

        with self.assertRaises(ClientError):
            self.client.get_bucket_location(Bucket='foo')

    def test_parse_get_bucket_location_returns_response(self):
        service_response = {"LocationConstraint": "us-west-2"}
        self.stubber.add_response('get_bucket_location', service_response)
        self.stubber.activate()
        response = self.client.get_bucket_location(Bucket='foo')
        self.assertEqual(response, service_response)

    def test_dynamodb_can_stub_without_credentials(self):
        assume_role_config = {
            'role_arn': 'arn:aws:iam::123456789012:role/demo',
            'source_profile': 'default',
            'external_id': 'fake-external-id',
        }
        with mock.patch(
            "botocore.credentials.RefreshableCredentials.account_id",
            side_effect=NoCredentialsError(),
        ):
            with mock.patch.object(
                botocore.session.Session,
                "get_scoped_config",
                return_value=assume_role_config,
            ):
                session = botocore.session.get_session()
                client = session.create_client(
                    "dynamodb",
                    region_name="us-east-1",
                )
                stubber = Stubber(client)
                service_response = {}
                stubber.add_response("list_tables", service_response)
                with stubber:
                    response = client.list_tables()
                self.assertEqual(response, service_response)


class TestStubberUnorderedGroup(unittest.TestCase):
    def setUp(self):
        session = botocore.session.get_session()
        config = botocore.config.Config(
            signature_version=botocore.UNSIGNED,
            s3={'addressing_style': 'path'},
        )
        self.client = session.create_client(
            's3', region_name='us-east-1', config=config
        )
        self.stubber = Stubber(self.client)

    def test_same_operation_different_params_any_order(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects',
                {'Name': 'bucket-one'},
                {'Bucket': 'bucket-one'},
            )
            self.stubber.add_response(
                'list_objects',
                {'Name': 'bucket-two'},
                {'Bucket': 'bucket-two'},
            )
        with self.stubber:
            # Calls are made in the opposite order they were queued and
            # each gets the response matching its parameters.
            response = self.client.list_objects(Bucket='bucket-two')
            self.assertEqual(response['Name'], 'bucket-two')
            response = self.client.list_objects(Bucket='bucket-one')
            self.assertEqual(response['Name'], 'bucket-one')
        self.stubber.assert_no_pending_responses()

    def test_identical_expectations_are_each_consumed_once(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'mybucket'}
            )
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'mybucket'}
            )
        with self.stubber:
            self.client.list_objects(Bucket='mybucket')
            self.client.list_objects(Bucket='mybucket')
        self.stubber.assert_no_pending_responses()

    def test_nested_any_matches_within_group(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'put_object',
                {},
                {
                    'Bucket': 'foo',
                    'Key': 'bar.txt',
                    'Metadata': {'MyMeta': stub.ANY},
                },
            )
            self.stubber.add_response(
                'head_object', {}, {'Bucket': 'foo', 'Key': 'bar.txt'}
            )
        with self.stubber:
            self.client.head_object(Bucket='foo', Key='bar.txt')
            self.client.put_object(
                Bucket='foo', Key='bar.txt', Metadata={'MyMeta': 'anything'}
            )
        self.stubber.assert_no_pending_responses()

    def test_ambiguous_match_raises_error(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': stub.ANY}
            )
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'mybucket'}
            )
        with self.stubber:
            with self.assertRaisesRegex(StubResponseError, 'Ambiguous'):
                self.client.list_objects(Bucket='mybucket')

    def test_unmatched_call_does_not_consume_expectation(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'mybucket'}
            )
        with self.stubber:
            with self.assertRaises(StubAssertionError):
                self.client.list_objects(Bucket='wrongbucket')
            # The expectation is still available for a matching call.
            self.client.list_objects(Bucket='mybucket')
        self.stubber.assert_no_pending_responses()

    def test_group_blocks_responses_queued_after_it(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'mybucket'}
            )
        self.stubber.add_response(
            'head_object', {}, {'Bucket': 'mybucket', 'Key': 'mykey'}
        )
        with self.stubber:
            # The group is not complete, so the queued head_object
            # response cannot be matched yet.
            with self.assertRaisesRegex(
                StubResponseError, 'Operation mismatch'
            ):
                self.client.head_object(Bucket='mybucket', Key='mykey')
            self.client.list_objects(Bucket='mybucket')
            self.client.head_object(Bucket='mybucket', Key='mykey')
        self.stubber.assert_no_pending_responses()

    def test_group_blocks_following_group(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'mybucket'}
            )
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'head_object', {}, {'Bucket': 'mybucket', 'Key': 'mykey'}
            )
        with self.stubber:
            with self.assertRaisesRegex(
                StubResponseError, 'Operation mismatch'
            ):
                self.client.head_object(Bucket='mybucket', Key='mykey')
            self.client.list_objects(Bucket='mybucket')
            self.client.head_object(Bucket='mybucket', Key='mykey')
        self.stubber.assert_no_pending_responses()

    def test_client_error_in_group_consumes_match(self):
        with self.stubber.unordered_group():
            self.stubber.add_client_error(
                'list_objects',
                'NoSuchBucket',
                'The specified bucket does not exist',
                expected_params={'Bucket': 'error-bucket'},
            )
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'ok-bucket'}
            )
        with self.stubber:
            self.client.list_objects(Bucket='ok-bucket')
            with self.assertRaises(ClientError):
                self.client.list_objects(Bucket='error-bucket')
        self.stubber.assert_no_pending_responses()

    def test_assert_no_pending_responses_lists_remaining(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'bucket-one'}
            )
            self.stubber.add_response(
                'head_object', {}, {'Bucket': 'bucket-two', 'Key': 'mykey'}
            )
        with self.assertRaises(AssertionError) as context:
            self.stubber.assert_no_pending_responses()
        message = str(context.exception)
        self.assertIn('2 responses remaining in queue', message)
        self.assertIn('ListObjects', message)
        self.assertIn('HeadObject', message)

    def test_cannot_nest_unordered_groups(self):
        with self.stubber.unordered_group():
            with self.assertRaises(ValueError):
                with self.stubber.unordered_group():
                    pass

    def test_threaded_calls_match_own_responses(self):
        buckets = [f'bucket-{i}' for i in range(5)]
        with self.stubber.unordered_group():
            for bucket in buckets:
                self.stubber.add_response(
                    'list_objects', {'Name': bucket}, {'Bucket': bucket}
                )
        barrier = threading.Barrier(len(buckets))
        results = {}
        errors = []

        def make_call(bucket):
            try:
                barrier.wait(timeout=10)
                response = self.client.list_objects(Bucket=bucket)
                results[bucket] = response['Name']
            except Exception as e:
                errors.append(e)

        with self.stubber:
            threads = [
                threading.Thread(target=make_call, args=(bucket,))
                for bucket in buckets
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        for bucket in buckets:
            self.assertEqual(results[bucket], bucket)
        self.stubber.assert_no_pending_responses()

    def test_threaded_calls_cannot_consume_same_response_twice(self):
        with self.stubber.unordered_group():
            self.stubber.add_response(
                'list_objects', {}, {'Bucket': 'mybucket'}
            )
        barrier = threading.Barrier(2)
        outcomes = []
        outcomes_lock = threading.Lock()

        def make_call():
            barrier.wait(timeout=10)
            try:
                self.client.list_objects(Bucket='mybucket')
                outcome = 'success'
            except StubResponseError:
                outcome = 'error'
            with outcomes_lock:
                outcomes.append(outcome)

        with self.stubber:
            threads = [threading.Thread(target=make_call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        # Exactly one call gets the response; the other is rejected and
        # the single expectation is consumed exactly once.
        self.assertEqual(sorted(outcomes), ['error', 'success'])
        self.stubber.assert_no_pending_responses()
