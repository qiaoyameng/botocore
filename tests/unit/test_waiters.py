# Copyright 2012-2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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
import io
import json
import os

import botocore
import botocore.config
import botocore.session
from botocore.exceptions import (
    ClientError,
    WaiterConfigError,
    WaiterError,
    WaiterPaused,
    WaiterStateError,
)
from botocore.loaders import Loader
from botocore.model import ServiceModel
from botocore.stub import Stubber
from botocore.waiter import (
    NormalizedOperationMethod,
    SingleWaiterConfig,
    Waiter,
    WaiterModel,
    create_waiter_with_client,
)
from tests import BaseEnvVar, mock, unittest


class TestWaiterModel(unittest.TestCase):
    def setUp(self):
        self.boiler_plate_config = {
            'description': 'Waiter description',
            'operation': 'HeadBucket',
            'delay': 5,
            'maxAttempts': 20,
        }

    def create_acceptor_function(self, for_config):
        single_waiter = {'acceptors': [for_config]}
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        return config.acceptors[0].matcher_func

    def test_waiter_version(self):
        self.assertEqual(WaiterModel({'version': 2, 'waiters': {}}).version, 2)

    def test_wont_load_missing_version_in_config(self):
        # We only load waiter configs if we know for sure that we're
        # loading version 2 of the format.
        waiters = {
            # Missing the 'version' key.
            'waiters': {}
        }
        with self.assertRaises(WaiterConfigError):
            WaiterModel(waiters)

    def test_unsupported_waiter_version(self):
        waiters = {'version': 1, 'waiters': {}}
        with self.assertRaises(WaiterConfigError):
            WaiterModel(waiters)

    def test_waiter_names(self):
        waiters = {
            'version': 2,
            'waiters': {
                'BarWaiter': {},
                'FooWaiter': {},
            },
        }
        self.assertEqual(
            WaiterModel(waiters).waiter_names, ['BarWaiter', 'FooWaiter']
        )

    def test_get_single_waiter_config(self):
        single_waiter = {
            'description': 'Waiter description',
            'operation': 'HeadBucket',
            'delay': 5,
            'maxAttempts': 20,
            'acceptors': [
                {'state': 'success', 'matcher': 'status', 'expected': 200},
                {'state': 'retry', 'matcher': 'status', 'expected': 404},
            ],
        }
        waiters = {
            'version': 2,
            'waiters': {
                'BucketExists': single_waiter,
            },
        }
        model = WaiterModel(waiters)
        config = model.get_waiter('BucketExists')
        self.assertEqual(config.operation, 'HeadBucket')

    def test_get_waiter_does_not_exist(self):
        waiters = {'version': 2, 'waiters': {}}
        model = WaiterModel(waiters)
        with self.assertRaises(ValueError):
            model.get_waiter('UnknownWaiter')

    def test_single_waiter_config_attributes(self):
        single_waiter = {
            'description': 'Waiter description',
            'operation': 'HeadBucket',
            'delay': 5,
            'maxAttempts': 20,
            'acceptors': [],
        }
        config = SingleWaiterConfig(single_waiter)
        self.assertEqual(config.description, 'Waiter description')
        self.assertEqual(config.operation, 'HeadBucket')
        self.assertEqual(config.delay, 5)
        self.assertEqual(config.max_attempts, 20)

    def test_single_waiter_acceptors_built_with_matcher_func(self):
        # When the list of acceptors are requested, we actually will transform
        # them into values that are easier to use.
        single_waiter = {
            'acceptors': [
                {'state': 'success', 'matcher': 'status', 'expected': 200},
            ],
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        success_acceptor = config.acceptors[0]

        self.assertEqual(success_acceptor.state, 'success')
        self.assertEqual(success_acceptor.matcher, 'status')
        self.assertEqual(success_acceptor.expected, 200)
        self.assertTrue(callable(success_acceptor.matcher_func))

    def test_single_waiter_acceptor_matches_jmespath(self):
        single_waiter = {
            'acceptors': [
                {
                    'state': 'success',
                    'matcher': 'path',
                    'argument': 'Table.TableStatus',
                    'expected': 'ACCEPTED',
                },
            ],
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        success_acceptor = config.acceptors[0].matcher_func
        # success_acceptor is a callable that takes a response dict and returns
        # True or False.
        self.assertTrue(
            success_acceptor({'Table': {'TableStatus': 'ACCEPTED'}})
        )
        self.assertFalse(
            success_acceptor({'Table': {'TableStatus': 'CREATING'}})
        )

    def test_single_waiter_supports_status_code(self):
        single_waiter = {
            'acceptors': [
                {'state': 'success', 'matcher': 'status', 'expected': 200}
            ],
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        success_acceptor = config.acceptors[0].matcher_func
        self.assertTrue(
            success_acceptor({'ResponseMetadata': {'HTTPStatusCode': 200}})
        )
        self.assertFalse(
            success_acceptor({'ResponseMetadata': {'HTTPStatusCode': 404}})
        )

    def test_single_waiter_supports_error(self):
        single_waiter = {
            'acceptors': [
                {
                    'state': 'success',
                    'matcher': 'error',
                    'expected': 'DoesNotExistError',
                }
            ],
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        success_acceptor = config.acceptors[0].matcher_func
        self.assertTrue(
            success_acceptor({'Error': {'Code': 'DoesNotExistError'}})
        )
        self.assertFalse(
            success_acceptor({'Error': {'Code': 'DoesNotExistErorr'}})
        )

    def test_single_waiter_supports_no_error(self):
        single_waiter = {
            'acceptors': [
                {
                    'state': 'success',
                    'matcher': 'error',
                    'expected': False,
                }
            ],
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        success_acceptor = config.acceptors[0].matcher_func
        self.assertTrue(success_acceptor({}))
        self.assertFalse(success_acceptor({'Error': {'Code': 'ExampleError'}}))

    def test_single_waiter_supports_any_error(self):
        single_waiter = {
            'acceptors': [
                {
                    'state': 'success',
                    'matcher': 'error',
                    'expected': True,
                }
            ],
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        success_acceptor = config.acceptors[0].matcher_func
        self.assertTrue(success_acceptor({'Error': {'Code': 'ExampleError1'}}))
        self.assertTrue(success_acceptor({'Error': {'Code': 'ExampleError2'}}))

    def test_unknown_matcher(self):
        unknown_type = 'arbitrary_type'
        single_waiter = {
            'acceptors': [
                {
                    'state': 'success',
                    'matcher': unknown_type,
                    'expected': 'foo',
                }
            ]
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        with self.assertRaises(WaiterConfigError):
            config.acceptors

    def test_single_waiter_supports_path_all(self):
        matches = self.create_acceptor_function(
            for_config={
                'state': 'success',
                'matcher': 'pathAll',
                'argument': 'Tables[].State',
                'expected': 'GOOD',
            }
        )
        self.assertTrue(matches({'Tables': [{"State": "GOOD"}]}))
        self.assertTrue(
            matches({'Tables': [{"State": "GOOD"}, {"State": "GOOD"}]})
        )

    def test_single_waiter_supports_path_any(self):
        matches = self.create_acceptor_function(
            for_config={
                'state': 'failure',
                'matcher': 'pathAny',
                'argument': 'Tables[].State',
                'expected': 'FAIL',
            }
        )
        self.assertTrue(matches({'Tables': [{"State": "FAIL"}]}))
        self.assertTrue(
            matches({'Tables': [{"State": "GOOD"}, {"State": "FAIL"}]})
        )

    def test_waiter_handles_error_responses_with_path_matchers(self):
        path_any = self.create_acceptor_function(
            for_config={
                'state': 'success',
                'matcher': 'pathAny',
                'argument': 'length(Tables) > `0`',
                'expected': True,
            }
        )
        path_all = self.create_acceptor_function(
            for_config={
                'state': 'success',
                'matcher': 'pathAll',
                'argument': 'length(Tables) > `0`',
                'expected': True,
            }
        )
        path = self.create_acceptor_function(
            for_config={
                'state': 'success',
                'matcher': 'path',
                'argument': 'length(Tables) > `0`',
                'expected': True,
            }
        )
        self.assertFalse(path_any({'Error': {'Code': 'DoesNotExist'}}))
        self.assertFalse(path_all({'Error': {'Code': 'DoesNotExist'}}))
        self.assertFalse(path({'Error': {'Code': 'DoesNotExist'}}))

    def test_single_waiter_does_not_match_path_all(self):
        matches = self.create_acceptor_function(
            for_config={
                'state': 'success',
                'matcher': 'pathAll',
                'argument': 'Tables[].State',
                'expected': 'GOOD',
            }
        )
        self.assertFalse(
            matches({'Tables': [{"State": "GOOD"}, {"State": "BAD"}]})
        )
        self.assertFalse(
            matches({'Tables': [{"State": "BAD"}, {"State": "GOOD"}]})
        )
        self.assertFalse(
            matches({'Tables': [{"State": "BAD"}, {"State": "BAD"}]})
        )
        self.assertFalse(matches({'Tables': []}))
        self.assertFalse(
            matches(
                {
                    'Tables': [
                        {"State": "BAD"},
                        {"State": "BAD"},
                        {"State": "BAD"},
                        {"State": "BAD"},
                    ]
                }
            )
        )

    def test_path_all_missing_field(self):
        matches = self.create_acceptor_function(
            for_config={
                'state': 'success',
                'matcher': 'pathAll',
                'argument': 'Tables[].State',
                'expected': 'GOOD',
            }
        )
        self.assertFalse(
            matches({'Tables': [{"NotState": "GOOD"}, {"NotState": "BAD"}]})
        )

    def test_path_all_matcher_does_not_receive_list(self):
        matches = self.create_acceptor_function(
            for_config={
                'state': 'success',
                'matcher': 'pathAll',
                'argument': 'Tables[].State',
                'expected': 'GOOD',
            }
        )
        self.assertFalse(matches({"NotTables": []}))

    def test_single_waiter_supports_all_three_states(self):
        single_waiter = {
            'acceptors': [
                {
                    'state': 'success',
                    'matcher': 'error',
                    'expected': 'DoesNotExistError',
                },
                {'state': 'success', 'matcher': 'status', 'expected': 200},
                {
                    'state': 'success',
                    'matcher': 'path',
                    'argument': 'Foo.Bar',
                    'expected': 'baz',
                },
            ],
        }
        single_waiter.update(self.boiler_plate_config)
        config = SingleWaiterConfig(single_waiter)
        acceptors = config.acceptors
        # Each acceptors should be able to handle not matching
        # any type of response.
        matches_nothing = {}
        self.assertFalse(acceptors[0].matcher_func(matches_nothing))
        self.assertFalse(acceptors[1].matcher_func(matches_nothing))
        self.assertFalse(acceptors[2].matcher_func(matches_nothing))


class TestWaitersObjects(unittest.TestCase):
    def setUp(self):
        pass

    def client_responses_are(self, *responses, **kwargs):
        operation_method = kwargs['for_operation']
        operation_method.side_effect = responses
        return operation_method

    def create_waiter_config(
        self, operation='MyOperation', delay=0, max_attempts=3, acceptors=None
    ):
        if acceptors is None:
            # Create some arbitrary acceptor that will never match.
            acceptors = [
                {'state': 'success', 'matcher': 'status', 'expected': 1000}
            ]
        waiter_config = {
            'operation': operation,
            'delay': delay,
            'maxAttempts': max_attempts,
            'acceptors': acceptors,
        }
        config = SingleWaiterConfig(waiter_config)
        return config

    def test_waiter_waits_until_acceptor_matches(self):
        config = self.create_waiter_config(
            max_attempts=3,
            acceptors=[
                {
                    'state': 'success',
                    'matcher': 'path',
                    'argument': 'Foo',
                    'expected': 'SUCCESS',
                }
            ],
        )
        # Simulate the client having two calls that don't
        # match followed by a third call that matches the
        # acceptor.
        operation_method = mock.Mock()
        waiter = Waiter('MyWaiter', config, operation_method)
        self.client_responses_are(
            {'Foo': 'FAILURE'},
            {'Foo': 'FAILURE'},
            {'Foo': 'SUCCESS'},
            for_operation=operation_method,
        )
        waiter.wait()
        self.assertEqual(operation_method.call_count, 3)

    def test_waiter_matches_with_invalid_error_response(self):
        # Verify that the call will not raise WaiterError
        # because of 'Error' key in success response.
        config = self.create_waiter_config(
            max_attempts=3,
            acceptors=[
                {
                    'state': 'success',
                    'matcher': 'path',
                    'argument': 'Foo',
                    'expected': 'SUCCESS',
                }
            ],
        )
        operation_method = mock.Mock()
        waiter = Waiter('MyWaiter', config, operation_method)
        self.client_responses_are(
            {'Foo': 'SUCCESS', 'Error': 'foo'}, for_operation=operation_method
        )
        waiter.wait()
        self.assertEqual(operation_method.call_count, 1)

    def test_waiter_never_matches(self):
        # Verify that a matcher will fail after max_attempts
        # is exceeded.
        config = self.create_waiter_config(max_attempts=3)
        operation_method = mock.Mock()
        self.client_responses_are(
            {'Foo': 'FAILURE'},
            {'Foo': 'FAILURE'},
            {'Foo': 'FAILURE'},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        with self.assertRaises(WaiterError):
            waiter.wait()

    def test_unspecified_errors_stops_waiter(self):
        # If a waiter receives an error response, then the
        # waiter immediately stops.
        config = self.create_waiter_config()
        operation_method = mock.Mock()
        self.client_responses_are(
            # This is an unknown error that's not called out
            # in any of the waiter config, so when the
            # waiter encounters this response it will transition
            # to the failure state.
            {'Error': {'Code': 'UnknownError', 'Message': 'bad error'}},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        with self.assertRaises(WaiterError):
            waiter.wait()

    def test_last_response_available_on_waiter_error(self):
        last_response = {
            'Error': {'Code': 'UnknownError', 'Message': 'bad error'}
        }
        config = self.create_waiter_config()
        operation_method = mock.Mock()
        self.client_responses_are(
            last_response, for_operation=operation_method
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        with self.assertRaises(WaiterError) as e:
            waiter.wait()
        self.assertEqual(e.exception.last_response, last_response)

    def test_unspecified_errors_propagate_error_code(self):
        # If a waiter receives an error response, then the
        # waiter should pass along the error code
        config = self.create_waiter_config()
        operation_method = mock.Mock()
        error_code = 'error_message'
        error_message = 'error_message'
        self.client_responses_are(
            # This is an unknown error that's not called out
            # in any of the waiter config, so when the
            # waiter encounters this response it will transition
            # to the failure state.
            {'Error': {'Code': error_code, 'Message': error_message}},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)

        with self.assertRaisesRegex(WaiterError, error_message):
            waiter.wait()

    def _assert_failure_state_error_raised(
        self, acceptors, responses, expected_msg
    ):
        config = self.create_waiter_config(acceptors=acceptors)
        operation_method = mock.Mock()
        waiter = Waiter('MyWaiter', config, operation_method)
        self.client_responses_are(*responses, for_operation=operation_method)
        with self.assertRaisesRegex(WaiterError, expected_msg):
            waiter.wait()

    def test_waiter_failure_state_error(self):
        test_cases = [
            (
                [
                    {
                        'state': 'failure',
                        'matcher': 'path',
                        'argument': 'Foo',
                        'expected': 'FAILURE',
                    }
                ],
                [{'Foo': 'FAILURE'}],
                'FAILURE',
            ),
            (
                [
                    {
                        'state': 'failure',
                        'matcher': 'pathAll',
                        'argument': 'Tables[].State',
                        'expected': 'FAILURE',
                    }
                ],
                [{'Tables': [{"State": "FAILURE"}]}],
                'FAILURE',
            ),
            (
                [
                    {
                        'state': 'failure',
                        'matcher': 'pathAny',
                        'argument': 'Tables[].State',
                        'expected': 'FAILURE',
                    }
                ],
                [{'Tables': [{"State": "FAILURE"}]}],
                'FAILURE',
            ),
            (
                [{'state': 'failure', 'matcher': 'status', 'expected': 404}],
                [{'ResponseMetadata': {'HTTPStatusCode': 404}}],
                '404',
            ),
            (
                [
                    {
                        'state': 'failure',
                        'matcher': 'error',
                        'expected': 'FailError',
                    }
                ],
                [{'Error': {'Code': 'FailError', 'Message': 'foo'}}],
                'FailError',
            ),
            (
                [
                    {
                        'state': 'retry',
                        'matcher': 'error',
                        'expected': 'RetryMe',
                    }
                ],
                [{'Success': False}] * 4,
                'Max attempts exceeded',
            ),
            (
                [
                    {'state': 'success', 'matcher': 'status', 'expected': 200},
                    {
                        'state': 'retry',
                        'matcher': 'error',
                        'expected': 'RetryMe',
                    },
                ],
                [
                    {'Success': False},
                    {'Error': {'Code': 'RetryMe', 'Message': 'foo'}},
                    {'Success': False},
                    {'Success': False},
                ],
                'Previously accepted state',
            ),
        ]

        for acceptors, responses, expected_msg in test_cases:
            self._assert_failure_state_error_raised(
                acceptors, responses, expected_msg
            )

    def test_waiter_transitions_to_failure_state(self):
        acceptors = [
            # A success state that will never be hit.
            {'state': 'success', 'matcher': 'status', 'expected': 1000},
            {'state': 'failure', 'matcher': 'error', 'expected': 'FailError'},
        ]
        config = self.create_waiter_config(acceptors=acceptors)
        operation_method = mock.Mock()
        self.client_responses_are(
            {'Nothing': 'foo'},
            # And on the second attempt, a FailError is seen, which
            # causes the waiter to fail fast.
            {'Error': {'Code': 'FailError', 'Message': 'foo'}},
            {'WillNeverGetCalled': True},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        with self.assertRaises(WaiterError):
            waiter.wait()
        # Not only should we raise an exception, but we should have
        # only called the operation_method twice because the second
        # response triggered a fast fail.
        self.assertEqual(operation_method.call_count, 2)

    def test_waiter_handles_retry_state(self):
        acceptor_with_retry_state = [
            {'state': 'success', 'matcher': 'status', 'expected': 200},
            {'state': 'retry', 'matcher': 'error', 'expected': 'RetryMe'},
        ]
        config = self.create_waiter_config(acceptors=acceptor_with_retry_state)
        operation_method = mock.Mock()
        self.client_responses_are(
            {'Nothing': 'foo'},
            {'Error': {'Code': 'RetryMe', 'Message': 'foo'}},
            {'Success': True, 'ResponseMetadata': {'HTTPStatusCode': 200}},
            {'NeverCalled': True},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        waiter.wait()
        self.assertEqual(operation_method.call_count, 3)

    def test_kwargs_are_passed_through(self):
        acceptors = [
            {'state': 'success', 'matcher': 'error', 'expected': 'MyError'},
        ]
        config = self.create_waiter_config(acceptors=acceptors)
        operation_method = mock.Mock()
        self.client_responses_are(
            {'Error': {'Code': 'MyError'}}, for_operation=operation_method
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        waiter.wait(Foo='foo', Bar='bar', Baz='baz')

        operation_method.assert_called_with(Foo='foo', Bar='bar', Baz='baz')

    @mock.patch('time.sleep')
    def test_waiter_honors_delay_time_between_retries(self, sleep_mock):
        delay_time = 5
        config = self.create_waiter_config(delay=delay_time)
        operation_method = mock.Mock()
        self.client_responses_are(
            # This is an unknown error that's not called out
            # in any of the waiter config, so when the
            # waiter encounters this response it will transition
            # to the failure state.
            {'Success': False},
            {'Success': False},
            {'Success': False},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        with self.assertRaises(WaiterError):
            waiter.wait()

        # We attempt three times, which means we need to sleep
        # twice, once before each subsequent request.
        self.assertEqual(sleep_mock.call_count, 2)
        sleep_mock.assert_called_with(delay_time)

    @mock.patch('time.sleep')
    def test_waiter_invocation_config_honors_delay(self, sleep_mock):
        config = self.create_waiter_config()
        operation_method = mock.Mock()
        self.client_responses_are(
            {'Success': False},
            {'Success': False},
            {'Success': False},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        custom_delay = 3
        with self.assertRaises(WaiterError):
            waiter.wait(WaiterConfig={'Delay': custom_delay})

        # We attempt three times, which means we need to sleep
        # twice, once before each subsequent request.
        self.assertEqual(sleep_mock.call_count, 2)
        sleep_mock.assert_called_with(custom_delay)

    def test_waiter_invocation_config_honors_max_attempts(self):
        config = self.create_waiter_config()
        operation_method = mock.Mock()
        self.client_responses_are(
            {'Success': False},
            {'Success': False},
            for_operation=operation_method,
        )
        waiter = Waiter('MyWaiter', config, operation_method)
        custom_max = 2
        with self.assertRaises(WaiterError):
            waiter.wait(WaiterConfig={'MaxAttempts': custom_max})

        self.assertEqual(operation_method.call_count, 2)


class TestCreateWaiter(unittest.TestCase):
    def setUp(self):
        self.waiter_config = {
            'version': 2,
            'waiters': {
                'WaiterName': {
                    'operation': 'Foo',
                    'delay': 1,
                    'maxAttempts': 1,
                    'acceptors': [],
                },
            },
        }
        self.waiter_model = WaiterModel(self.waiter_config)
        self.service_json_model = {
            'metadata': {'serviceFullName': 'Amazon MyService'},
            'operations': {
                'Foo': {
                    'name': 'Foo',
                    'input': {'shape': 'FooInputOutput'},
                    'output': {'shape': 'FooInputOutput'},
                }
            },
            'shapes': {
                'FooInputOutput': {
                    'type': 'structure',
                    'members': {
                        'bar': {
                            'shape': 'String',
                            'documentation': 'Documents bar',
                        }
                    },
                },
                'String': {'type': 'string'},
            },
        }
        self.service_model = ServiceModel(self.service_json_model, 'myservice')
        self.client = mock.Mock()
        self.client.meta.service_model = self.service_model

    def test_can_create_waiter_from_client(self):
        waiter_name = 'WaiterName'
        waiter = create_waiter_with_client(
            waiter_name, self.waiter_model, self.client
        )
        self.assertIsInstance(waiter, Waiter)

    def test_waiter_class_name(self):
        waiter_name = 'WaiterName'
        waiter = create_waiter_with_client(
            waiter_name, self.waiter_model, self.client
        )
        self.assertEqual(
            waiter.__class__.__name__, 'MyService.Waiter.WaiterName'
        )

    def test_waiter_help_documentation(self):
        waiter_name = 'WaiterName'
        waiter = create_waiter_with_client(
            waiter_name, self.waiter_model, self.client
        )
        with mock.patch('sys.stdout', io.StringIO()) as mock_stdout:
            help(waiter.wait)
        content = mock_stdout.getvalue()
        lines = [
            (
                '    Polls :py:meth:`MyService.Client.foo` every 1 '
                'seconds until a successful state is reached. An error '
                'is raised after 1 failed checks.'
            ),
            '    **Request Syntax**',
            '    ::',
            '      waiter.wait(',
            "          bar='string'",
            '      )',
            '    :type bar: string',
            '    :param bar: Documents bar',
            '    :returns: None',
        ]
        for line in lines:
            self.assertIn(line, content)


class FakeClock:
    """A deterministic clock for testing resumable waiters."""

    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TestResumableWaiter(unittest.TestCase):
    def create_waiter_config(
        self, delay=5, max_attempts=5, acceptors=None, operation='MyOperation'
    ):
        if acceptors is None:
            acceptors = [
                {
                    'state': 'success',
                    'matcher': 'path',
                    'argument': 'Foo',
                    'expected': 'SUCCESS',
                },
                {
                    'state': 'retry',
                    'matcher': 'path',
                    'argument': 'Foo',
                    'expected': 'RETRY',
                },
            ]
        return SingleWaiterConfig(
            {
                'operation': operation,
                'delay': delay,
                'maxAttempts': max_attempts,
                'acceptors': acceptors,
            }
        )

    def create_paused_state(self, responses=('RETRY',), **wait_kwargs):
        config = wait_kwargs.pop('config', self.create_waiter_config())
        clock = wait_kwargs.pop('clock', FakeClock())
        operation_method = mock.Mock()
        operation_method.side_effect = [
            {'Foo': value} for value in responses
        ]
        waiter = Waiter('MyWaiter', config, operation_method, clock=clock)
        wait_kwargs.setdefault('MaxPolls', len(responses))
        with self.assertRaises(WaiterPaused) as e:
            waiter.wait(**wait_kwargs)
        return e.exception.state, operation_method, clock

    def test_pause_provides_json_state(self):
        clock = FakeClock()
        token, operation_method, _ = self.create_paused_state(
            responses=('RETRY', 'RETRY'), clock=clock
        )
        state = json.loads(token)
        self.assertEqual(
            set(state),
            {
                'version',
                'waiter',
                'state',
                'attempts',
                'next_poll_at',
                'fingerprint',
                'last_acceptor',
            },
        )
        self.assertEqual(state['waiter'], 'MyWaiter')
        self.assertEqual(state['state'], 'waiting')
        self.assertEqual(state['attempts'], 2)
        # One sleep happened after the first poll; the second poll's
        # delay is captured as the next allowed poll time instead.
        self.assertEqual(clock.sleeps, [5])
        self.assertEqual(state['next_poll_at'], 1010.0)
        self.assertEqual(operation_method.call_count, 2)

    def test_state_contains_no_params_or_response_content(self):
        config = self.create_waiter_config()
        clock = FakeClock()
        operation_method = mock.Mock()
        operation_method.side_effect = [
            {'Foo': 'RETRY', 'Sensitive': 'response-secret'}
        ]
        waiter = Waiter('MyWaiter', config, operation_method, clock=clock)
        with self.assertRaises(WaiterPaused) as e:
            waiter.wait(MaxPolls=1, Credentials='request-secret')
        token = e.exception.state
        self.assertNotIn('response-secret', token)
        self.assertNotIn('request-secret', token)
        self.assertNotIn('Credentials', token)

    def test_resume_continues_where_previous_call_left_off(self):
        token, _, first_clock = self.create_paused_state(
            responses=('RETRY', 'RETRY')
        )
        # A brand new waiter instance resumes from the saved state.
        second_clock = FakeClock(now=first_clock.now)
        operation_method = mock.Mock()
        operation_method.side_effect = [{'Foo': 'SUCCESS'}]
        waiter = Waiter(
            'MyWaiter',
            self.create_waiter_config(),
            operation_method,
            clock=second_clock,
        )
        waiter.wait(ResumeFrom=token)
        self.assertEqual(operation_method.call_count, 1)
        # The remaining delay from the first call is slept before polling.
        self.assertEqual(second_clock.sleeps, [5])

    def test_resume_refuses_changed_params(self):
        token, operation_method, _ = self.create_paused_state(Bucket='a')
        waiter = Waiter(
            'MyWaiter',
            self.create_waiter_config(),
            operation_method,
            clock=FakeClock(),
        )
        with self.assertRaises(WaiterStateError):
            waiter.wait(Bucket='b', ResumeFrom=token)
        # No additional requests are made.
        self.assertEqual(operation_method.call_count, 1)

    def test_resume_refuses_changed_acceptor_definitions(self):
        token, operation_method, _ = self.create_paused_state()
        changed_config = self.create_waiter_config(
            acceptors=[
                {
                    'state': 'success',
                    'matcher': 'path',
                    'argument': 'Foo',
                    'expected': 'CHANGED',
                }
            ]
        )
        waiter = Waiter(
            'MyWaiter', changed_config, operation_method, clock=FakeClock()
        )
        with self.assertRaises(WaiterStateError):
            waiter.wait(ResumeFrom=token)
        self.assertEqual(operation_method.call_count, 1)

    def test_resume_refuses_changed_waiter_config(self):
        token, operation_method, _ = self.create_paused_state()
        waiter = Waiter(
            'MyWaiter',
            self.create_waiter_config(),
            operation_method,
            clock=FakeClock(),
        )
        with self.assertRaises(WaiterStateError):
            waiter.wait(
                ResumeFrom=token, WaiterConfig={'Delay': 9, 'MaxAttempts': 5}
            )
        self.assertEqual(operation_method.call_count, 1)

    def test_resume_rejects_malformed_state(self):
        operation_method = mock.Mock()
        waiter = Waiter(
            'MyWaiter',
            self.create_waiter_config(),
            operation_method,
            clock=FakeClock(),
        )
        bad_tokens = [
            'not-json',
            json.dumps(['not', 'a', 'dict']),
            json.dumps({'version': 999}),
            json.dumps(
                {
                    'version': 1,
                    'state': 'waiting',
                    'attempts': 'not-an-int',
                    'next_poll_at': 0,
                }
            ),
        ]
        for token in bad_tokens:
            with self.assertRaises(WaiterStateError):
                waiter.wait(ResumeFrom=token)
        operation_method.assert_not_called()

    def test_resume_rejects_terminal_state(self):
        token, operation_method, _ = self.create_paused_state()
        state = json.loads(token)
        state['state'] = 'success'
        waiter = Waiter(
            'MyWaiter',
            self.create_waiter_config(),
            operation_method,
            clock=FakeClock(),
        )
        with self.assertRaises(WaiterStateError):
            waiter.wait(ResumeFrom=json.dumps(state))
        self.assertEqual(operation_method.call_count, 1)

    def test_resume_enforces_original_max_attempts(self):
        token, operation_method, _ = self.create_paused_state(
            config=self.create_waiter_config(max_attempts=2)
        )
        # A state that has already consumed the original MaxAttempts
        # budget must raise without issuing another request.
        state = json.loads(token)
        state['attempts'] = 2
        waiter = Waiter(
            'MyWaiter',
            self.create_waiter_config(max_attempts=2),
            operation_method,
            clock=FakeClock(),
        )
        with self.assertRaisesRegex(WaiterError, 'Max attempts exceeded'):
            waiter.wait(ResumeFrom=json.dumps(state))
        self.assertEqual(operation_method.call_count, 1)

    def test_max_attempts_spans_pause_and_resume(self):
        config = self.create_waiter_config(max_attempts=3)
        token, _, first_clock = self.create_paused_state(
            responses=('RETRY', 'RETRY'), config=config
        )
        operation_method = mock.Mock()
        operation_method.side_effect = [{'Foo': 'RETRY'}]
        waiter = Waiter(
            'MyWaiter',
            config,
            operation_method,
            clock=FakeClock(now=first_clock.now),
        )
        # The third (and final allowed) attempt is made by the resumed
        # waiter, which then exhausts the original MaxAttempts budget.
        with self.assertRaisesRegex(WaiterError, 'Max attempts exceeded'):
            waiter.wait(ResumeFrom=token)
        self.assertEqual(operation_method.call_count, 1)


class TestResumableWaiterEquivalence(unittest.TestCase):
    # Verify that a wait split across two waiter instances makes the same
    # decisions as a single continuous wait, using a real client driven
    # by a Stubber and an injected clock.

    def setUp(self):
        session = botocore.session.get_session()
        config = botocore.config.Config(signature_version=botocore.UNSIGNED)
        self.client = session.create_client(
            's3', region_name='us-east-1', config=config
        )
        self.stubber = Stubber(self.client)
        self.clock = FakeClock()

    def create_waiter(self, max_attempts=5, delay=5):
        waiter_model = WaiterModel(
            {
                'version': 2,
                'waiters': {
                    'ObjectReady': {
                        'operation': 'ListObjects',
                        'delay': delay,
                        'maxAttempts': max_attempts,
                        'acceptors': [
                            {
                                'state': 'success',
                                'matcher': 'path',
                                'argument': 'IsTruncated',
                                'expected': False,
                            },
                            {
                                'state': 'retry',
                                'matcher': 'path',
                                'argument': 'IsTruncated',
                                'expected': True,
                            },
                            {
                                'state': 'failure',
                                'matcher': 'error',
                                'expected': 'NoSuchBucket',
                            },
                        ],
                    }
                },
            }
        )
        return create_waiter_with_client(
            'ObjectReady', waiter_model, self.client, clock=self.clock
        )

    def add_pending_response(self):
        self.stubber.add_response(
            'list_objects',
            {'IsTruncated': True},
            expected_params={'Bucket': 'mybucket'},
        )

    def add_ready_response(self):
        self.stubber.add_response(
            'list_objects',
            {'IsTruncated': False},
            expected_params={'Bucket': 'mybucket'},
        )

    def test_success_matches_continuous_run(self):
        # Continuous run: two retries then a success.
        self.add_pending_response()
        self.add_pending_response()
        self.add_ready_response()
        with self.stubber:
            self.create_waiter().wait(Bucket='mybucket')
        self.stubber.assert_no_pending_responses()
        continuous_sleeps = list(self.clock.sleeps)

        # Segmented run: pause after the first poll, resume on a new
        # waiter instance, pause again, then resume to completion.
        self.setUp()
        self.add_pending_response()
        self.add_pending_response()
        self.add_ready_response()
        with self.stubber:
            with self.assertRaises(WaiterPaused) as first_pause:
                self.create_waiter().wait(Bucket='mybucket', MaxPolls=1)
            with self.assertRaises(WaiterPaused) as second_pause:
                self.create_waiter().wait(
                    Bucket='mybucket',
                    MaxPolls=1,
                    ResumeFrom=first_pause.exception.state,
                )
            self.create_waiter().wait(
                Bucket='mybucket', ResumeFrom=second_pause.exception.state
            )
        self.stubber.assert_no_pending_responses()
        self.assertEqual(self.clock.sleeps, continuous_sleeps)

    def test_failure_matches_continuous_run(self):
        # Continuous run: one retry then a terminal failure.
        self.add_pending_response()
        self.stubber.add_client_error(
            'list_objects',
            'NoSuchBucket',
            expected_params={'Bucket': 'mybucket'},
        )
        with self.stubber:
            with self.assertRaises(WaiterError) as continuous_error:
                self.create_waiter().wait(Bucket='mybucket')
        self.stubber.assert_no_pending_responses()
        continuous_sleeps = list(self.clock.sleeps)

        # Segmented run: pause after the first poll, then hit the same
        # terminal failure on the resumed waiter.
        self.setUp()
        self.add_pending_response()
        self.stubber.add_client_error(
            'list_objects',
            'NoSuchBucket',
            expected_params={'Bucket': 'mybucket'},
        )
        with self.stubber:
            with self.assertRaises(WaiterPaused) as pause:
                self.create_waiter().wait(Bucket='mybucket', MaxPolls=1)
            with self.assertRaises(WaiterError) as segmented_error:
                self.create_waiter().wait(
                    Bucket='mybucket', ResumeFrom=pause.exception.state
                )
        self.stubber.assert_no_pending_responses()
        self.assertEqual(self.clock.sleeps, continuous_sleeps)
        self.assertEqual(
            str(segmented_error.exception), str(continuous_error.exception)
        )

    def test_max_attempts_exceeded_matches_continuous_run(self):
        # Continuous run: three retries exhaust the max attempts.
        for _ in range(3):
            self.add_pending_response()
        with self.stubber:
            with self.assertRaises(WaiterError) as continuous_error:
                self.create_waiter(max_attempts=3).wait(Bucket='mybucket')
        self.stubber.assert_no_pending_responses()
        continuous_sleeps = list(self.clock.sleeps)

        # Segmented run: two polls in the first segment, the final
        # allowed poll happens after resuming.
        self.setUp()
        for _ in range(3):
            self.add_pending_response()
        with self.stubber:
            with self.assertRaises(WaiterPaused) as pause:
                self.create_waiter(max_attempts=3).wait(
                    Bucket='mybucket', MaxPolls=2
                )
            with self.assertRaises(WaiterError) as segmented_error:
                self.create_waiter(max_attempts=3).wait(
                    Bucket='mybucket', ResumeFrom=pause.exception.state
                )
        self.stubber.assert_no_pending_responses()
        self.assertEqual(self.clock.sleeps, continuous_sleeps)
        self.assertEqual(
            str(segmented_error.exception), str(continuous_error.exception)
        )


class TestOperationMethods(unittest.TestCase):
    def test_normalized_op_method_makes_call(self):
        client_method = mock.Mock()
        op = NormalizedOperationMethod(client_method)
        op(Foo='a', Bar='b')

        client_method.assert_called_with(Foo='a', Bar='b')

    def test_normalized_op_returns_error_response(self):
        # Client objects normally throw exceptions when an error
        # occurs, but we need to return the parsed error response.
        client_method = mock.Mock()
        op = NormalizedOperationMethod(client_method)
        parsed_response = {'Error': {'Code': 'Foo', 'Message': 'bar'}}
        exception = ClientError(parsed_response, 'OperationName')
        client_method.side_effect = exception
        actual_response = op(Foo='a', Bar='b')
        self.assertEqual(actual_response, parsed_response)


class ServiceWaiterFunctionalTest(BaseEnvVar):
    """
    This class is used as a base class if you want to functionally test the
    waiters for a specific service.
    """

    def setUp(self):
        super().setUp()
        self.data_path = os.path.join(
            os.path.dirname(botocore.__file__), 'data'
        )
        self.environ['AWS_DATA_PATH'] = self.data_path
        self.loader = Loader([self.data_path])

    def get_waiter_model(self, service, api_version=None):
        """Get the waiter model for the service."""
        with mock.patch(
            'botocore.loaders.Loader.list_available_services',
            return_value=[service],
        ):
            return WaiterModel(
                self.loader.load_service_model(
                    service, type_name='waiters-2', api_version=api_version
                )
            )

    def get_service_model(self, service, api_version=None):
        """Get the service model for the service."""
        with mock.patch(
            'botocore.loaders.Loader.list_available_services',
            return_value=[service],
        ):
            return ServiceModel(
                self.loader.load_service_model(
                    service, type_name='service-2', api_version=api_version
                ),
                service_name=service,
            )


class CloudFrontWaitersTest(ServiceWaiterFunctionalTest):
    def setUp(self):
        super().setUp()
        self.client = mock.Mock()
        self.service = 'cloudfront'
        self.old_api_versions = ['2014-05-31']

    def assert_distribution_deployed_call_count(self, api_version=None):
        waiter_name = 'DistributionDeployed'
        waiter_model = self.get_waiter_model(self.service, api_version)
        self.client.meta.service_model = self.get_service_model(
            self.service, api_version
        )
        self.client.get_distribution.side_effect = [
            {'Distribution': {'Status': 'Deployed'}}
        ]
        waiter = create_waiter_with_client(
            waiter_name, waiter_model, self.client
        )
        waiter.wait()
        self.assertEqual(self.client.get_distribution.call_count, 1)

    def assert_invalidation_completed_call_count(self, api_version=None):
        waiter_name = 'InvalidationCompleted'
        waiter_model = self.get_waiter_model(self.service, api_version)
        self.client.meta.service_model = self.get_service_model(
            self.service, api_version
        )
        self.client.get_invalidation.side_effect = [
            {'Invalidation': {'Status': 'Completed'}}
        ]
        waiter = create_waiter_with_client(
            waiter_name, waiter_model, self.client
        )
        waiter.wait()
        self.assertEqual(self.client.get_invalidation.call_count, 1)

    def assert_streaming_distribution_deployed_call_count(
        self, api_version=None
    ):
        waiter_name = 'StreamingDistributionDeployed'
        waiter_model = self.get_waiter_model(self.service, api_version)
        self.client.meta.service_model = self.get_service_model(
            self.service, api_version
        )
        self.client.get_streaming_distribution.side_effect = [
            {'StreamingDistribution': {'Status': 'Deployed'}}
        ]
        waiter = create_waiter_with_client(
            waiter_name, waiter_model, self.client
        )
        waiter.wait()
        self.assertEqual(self.client.get_streaming_distribution.call_count, 1)

    def test_distribution_deployed(self):
        # Test the latest version.
        self.assert_distribution_deployed_call_count()
        self.client.reset_mock()

        # Test previous api versions.
        for api_version in self.old_api_versions:
            self.assert_distribution_deployed_call_count(api_version)
            self.client.reset_mock()

    def test_invalidation_completed(self):
        # Test the latest version.
        self.assert_invalidation_completed_call_count()
        self.client.reset_mock()

        # Test previous api versions.
        for api_version in self.old_api_versions:
            self.assert_invalidation_completed_call_count(api_version)
            self.client.reset_mock()

    def test_streaming_distribution_deployed(self):
        # Test the latest version.
        self.assert_streaming_distribution_deployed_call_count()
        self.client.reset_mock()

        # Test previous api versions.
        for api_version in self.old_api_versions:
            self.assert_streaming_distribution_deployed_call_count(api_version)
            self.client.reset_mock()
