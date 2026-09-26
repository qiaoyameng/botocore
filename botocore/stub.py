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
import copy
import threading
from collections import deque
from contextlib import contextmanager
from pprint import pformat

from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ParamValidationError,
    StubAssertionError,
    StubResponseError,
    UnStubbedResponseError,
)
from botocore.validate import validate_parameters


class _ANY:
    """
    A helper object that compares equal to everything. Copied from
    unittest.mock
    """

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __repr__(self):
        return '<ANY>'


ANY = _ANY()


def _expected_params_match(expected_params, params):
    # An expected_params of None means the parameters are not validated,
    # so anything matches.  Otherwise the parameter names must be exactly
    # the same and every expected value must compare equal to the actual
    # value.  Comparisons are done with the expected value on the left so
    # that ANY (including ANY nested inside dicts or lists) keeps its
    # "matches everything" meaning.
    if expected_params is None:
        return True
    if sorted(expected_params.keys()) != sorted(params.keys()):
        return False
    for param, value in expected_params.items():
        if value != params[param]:
            return False
    return True


def _strict_equal(first, second):
    # Equality used to detect identical expectations.  Unlike normal
    # comparison, ANY only compares equal to itself so that an expectation
    # using ANY is never considered identical to a concrete one.
    if first is ANY or second is ANY:
        return first is ANY and second is ANY
    if isinstance(first, dict) and isinstance(second, dict):
        if first.keys() != second.keys():
            return False
        return all(_strict_equal(first[key], second[key]) for key in first)
    if isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
        return len(first) == len(second) and all(
            _strict_equal(first_item, second_item)
            for first_item, second_item in zip(first, second)
    )
    return first == second


def _expectations_equivalent(first, second):
    return first['operation_name'] == second['operation_name'] and (
        _strict_equal(first['expected_params'], second['expected_params'])
    )


class _UnorderedGroup:
    """A group of stubbed responses matched by operation and parameters.

    Responses within the group can be consumed in any order, but the
    whole group must be consumed before the next queued response or
    group can be matched.
    """

    def __init__(self):
        self.responses = []

    def __len__(self):
        return len(self.responses)

    def find_match(self, operation_name, params):
        operation_matches = [
            response
            for response in self.responses
            if response['operation_name'] == operation_name
        ]
        if not operation_matches:
            remaining = sorted(
                {response['operation_name'] for response in self.responses}
            )
            raise StubResponseError(
                operation_name=operation_name,
                reason=(
                    'Operation mismatch: no remaining response for '
                    f'{operation_name} in the current unordered group. '
                    f'Remaining operations in group: {remaining}.'
                ),
            )
        param_matches = [
            response
            for response in operation_matches
            if _expected_params_match(response['expected_params'], params)
        ]
        if not param_matches:
            expected = [r['expected_params'] for r in operation_matches]
            raise StubAssertionError(
                operation_name=operation_name,
                reason=(
                    'Expected parameters:\n'
                    f'{pformat(expected)},\n'
                    f'but received:\n{pformat(params)}'
                ),
            )
        distinct = []
        for response in param_matches:
            if not any(
                _expectations_equivalent(response, other) for other in distinct
            ):
                distinct.append(response)
        if len(distinct) > 1:
            expected = [r['expected_params'] for r in distinct]
            raise StubResponseError(
                operation_name=operation_name,
                reason=(
                    'Ambiguous API call: the call matches multiple '
                    'distinct stubbed responses in the current unordered '
                    f'group:\n{pformat(expected)}'
                ),
            )
        return distinct[0]

    def consume_equivalent(self, matched):
        # Remove and return the response of a remaining expectation that
        # is equivalent to the one matched earlier.  Returns None if no
        # equivalent expectation remains (e.g. another call consumed it).
        for index, response in enumerate(self.responses):
            if _expectations_equivalent(response, matched):
                del self.responses[index]
                return response['response']
        return None


class Stubber:
    """
    This class will allow you to stub out requests so you don't have to hit
    an endpoint to write tests. Responses are returned first in, first out.
    If operations are called out of order, or are called with no remaining
    queued responses, an error will be raised.

    **Example:**
    ::
        import datetime
        import botocore.session
        from botocore.stub import Stubber


        s3 = botocore.session.get_session().create_client('s3')
        stubber = Stubber(s3)

        response = {
            'IsTruncated': False,
            'Name': 'test-bucket',
            'MaxKeys': 1000, 'Prefix': '',
            'Contents': [{
                'Key': 'test.txt',
                'ETag': '"abc123"',
                'StorageClass': 'STANDARD',
                'LastModified': datetime.datetime(2016, 1, 20, 22, 9),
                'Owner': {'ID': 'abc123', 'DisplayName': 'myname'},
                'Size': 14814
            }],
            'EncodingType': 'url',
            'ResponseMetadata': {
                'RequestId': 'abc123',
                'HTTPStatusCode': 200,
                'HostId': 'abc123'
            },
            'Marker': ''
        }

        expected_params = {'Bucket': 'test-bucket'}

        stubber.add_response('list_objects', response, expected_params)
        stubber.activate()

        service_response = s3.list_objects(Bucket='test-bucket')
        assert service_response == response


    This class can also be called as a context manager, which will handle
    activation / deactivation for you.

    **Example:**
    ::
        import datetime
        import botocore.session
        from botocore.stub import Stubber


        s3 = botocore.session.get_session().create_client('s3')

        response = {
            "Owner": {
                "ID": "foo",
                "DisplayName": "bar"
            },
            "Buckets": [{
                "CreationDate": datetime.datetime(2016, 1, 20, 22, 9),
                "Name": "baz"
            }]
        }


        with Stubber(s3) as stubber:
            stubber.add_response('list_buckets', response, {})
            service_response = s3.list_buckets()

        assert service_response == response


    If you have an input parameter that is a randomly generated value, or you
    otherwise don't care about its value, you can use ``stub.ANY`` to ignore
    it in validation.

    **Example:**
    ::
        import datetime
        import botocore.session
        from botocore.stub import Stubber, ANY


        s3 = botocore.session.get_session().create_client('s3')
        stubber = Stubber(s3)

        response = {
            'IsTruncated': False,
            'Name': 'test-bucket',
            'MaxKeys': 1000, 'Prefix': '',
            'Contents': [{
                'Key': 'test.txt',
                'ETag': '"abc123"',
                'StorageClass': 'STANDARD',
                'LastModified': datetime.datetime(2016, 1, 20, 22, 9),
                'Owner': {'ID': 'abc123', 'DisplayName': 'myname'},
                'Size': 14814
            }],
            'EncodingType': 'url',
            'ResponseMetadata': {
                'RequestId': 'abc123',
                'HTTPStatusCode': 200,
                'HostId': 'abc123'
            },
            'Marker': ''
        }

        expected_params = {'Bucket': ANY}
        stubber.add_response('list_objects', response, expected_params)

        with stubber:
            service_response = s3.list_objects(Bucket='test-bucket')

        assert service_response == response


    If your code makes concurrent calls whose order is not deterministic,
    you can group the stubbed responses with ``unordered_group`` so that
    they are matched by operation name and expected parameters instead of
    strictly first in, first out. Responses outside the group keep the
    regular FIFO behavior, and the group must be fully consumed before
    any response queued after it can be matched.

    **Example:**
    ::
        import botocore.session
        from botocore.stub import Stubber


        s3 = botocore.session.get_session().create_client('s3')
        stubber = Stubber(s3)

        with stubber.unordered_group():
            stubber.add_response(
                'list_objects', {}, {'Bucket': 'bucket-one'}
            )
            stubber.add_response(
                'list_objects', {}, {'Bucket': 'bucket-two'}
            )

        with stubber:
            # Either call may happen first.
            s3.list_objects(Bucket='bucket-two')
            s3.list_objects(Bucket='bucket-one')
    """

    def __init__(self, client):
        """
        :param client: The client to add your stubs to.
        """
        self.client = client
        self._event_id = 'boto_stubber'
        self._expected_params_event_id = 'boto_stubber_expected_params'
        self._stub_account_id_event_id = 'boto_stubber_stub_account_id'
        self._queue = deque()
        self._active_group = None
        self._lock = threading.Lock()
        self._thread_local = threading.local()

    def __enter__(self):
        self.activate()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.deactivate()

    def activate(self):
        """
        Activates the stubber on the client
        """
        self.client.meta.events.register_first(
            'before-parameter-build.*.*',
            self._assert_expected_params,
            unique_id=self._expected_params_event_id,
        )
        self.client.meta.events.register(
            'before-call.*.*',
            self._get_response_handler,
            unique_id=self._event_id,
        )
        self.client.meta.events.register(
            'before-endpoint-resolution.*',
            self._set_account_id_for_endpoint_resolution,
            unique_id=self._stub_account_id_event_id,
        )

    def deactivate(self):
        """
        Deactivates the stubber on the client
        """
        self.client.meta.events.unregister(
            'before-parameter-build.*.*',
            self._assert_expected_params,
            unique_id=self._expected_params_event_id,
        )
        self.client.meta.events.unregister(
            'before-call.*.*',
            self._get_response_handler,
            unique_id=self._event_id,
        )
        self.client.meta.events.unregister(
            'before-endpoint-resolution.*',
            self._stub_account_id_event_id,
            unique_id=self._stub_account_id_event_id,
        )

    def add_response(self, method, service_response, expected_params=None):
        """
        Adds a service response to the response queue. This will be validated
        against the service model to ensure correctness. It should be noted,
        however, that while missing attributes are often considered correct,
        your code may not function properly if you leave them out. Therefore
        you should always fill in every value you see in a typical response for
        your particular request.

        :param method: The name of the client method to stub.
        :type method: str

        :param service_response: A dict response stub. Provided parameters will
            be validated against the service model.
        :type service_response: dict

        :param expected_params: A dictionary of the expected parameters to
            be called for the provided service response. The parameters match
            the names of keyword arguments passed to that client call. If
            any of the parameters differ a ``StubResponseError`` is thrown.
            You can use stub.ANY to indicate a particular parameter to ignore
            in validation. stub.ANY is only valid for top level params.
        """
        self._add_response(method, service_response, expected_params)

    def _add_response(self, method, service_response, expected_params):
        if not hasattr(self.client, method):
            raise ValueError(
                f"Client {self.client.meta.service_model.service_name} "
                f"does not have method: {method}"
            )

        # Create a successful http response
        http_response = AWSResponse(None, 200, {}, None)

        operation_name = self.client.meta.method_to_api_mapping.get(method)
        self._validate_operation_response(operation_name, service_response)

        # Add the service_response to the queue for returning responses
        response = {
            'operation_name': operation_name,
            'response': (http_response, service_response),
            'expected_params': expected_params,
        }
        self._enqueue_response(response)

    @contextmanager
    def unordered_group(self):
        """Groups stubbed responses so they can be called in any order.

        Responses and client errors added within the context are matched
        by operation name and ``expected_params`` (``stub.ANY`` keeps its
        usual meaning) rather than by the order they were added in.  A
        call that matches multiple distinct expectations in the group
        raises a ``StubResponseError``, and a call that matches none of
        them raises an error without consuming any expectation.  The
        group must be fully consumed before responses queued after it
        can be matched.  Groups cannot be nested.
        """
        if self._active_group is not None:
            raise ValueError('Cannot nest unordered groups.')
        group = _UnorderedGroup()
        self._active_group = group
        try:
            yield
        finally:
            self._active_group = None
            if len(group) > 0:
                self._queue.append(group)

    def _enqueue_response(self, response):
        if self._active_group is not None:
            self._active_group.responses.append(response)
        else:
            self._queue.append(response)

    def add_client_error(
        self,
        method,
        service_error_code='',
        service_message='',
        http_status_code=400,
        service_error_meta=None,
        expected_params=None,
        response_meta=None,
        modeled_fields=None,
    ):
        """
        Adds a ``ClientError`` to the response queue.

        :param method: The name of the service method to return the error on.
        :type method: str

        :param service_error_code: The service error code to return,
                                   e.g. ``NoSuchBucket``
        :type service_error_code: str

        :param service_message: The service message to return, e.g.
                        'The specified bucket does not exist.'
        :type service_message: str

        :param http_status_code: The HTTP status code to return, e.g. 404, etc
        :type http_status_code: int

        :param service_error_meta: Additional keys to be added to the
            service Error
        :type service_error_meta: dict

        :param expected_params: A dictionary of the expected parameters to
            be called for the provided service response. The parameters match
            the names of keyword arguments passed to that client call. If
            any of the parameters differ a ``StubResponseError`` is thrown.
            You can use stub.ANY to indicate a particular parameter to ignore
            in validation.

        :param response_meta: Additional keys to be added to the
            response's ResponseMetadata
        :type response_meta: dict

        :param modeled_fields: Additional keys to be added to the response
            based on fields that are modeled for the particular error code.
            These keys will be validated against the particular error shape
            designated by the error code.
        :type modeled_fields: dict

        """
        http_response = AWSResponse(None, http_status_code, {}, None)

        # We don't look to the model to build this because the caller would
        # need to know the details of what the HTTP body would need to
        # look like.
        parsed_response = {
            'ResponseMetadata': {'HTTPStatusCode': http_status_code},
            'Error': {'Message': service_message, 'Code': service_error_code},
        }

        if service_error_meta is not None:
            parsed_response['Error'].update(service_error_meta)

        if response_meta is not None:
            parsed_response['ResponseMetadata'].update(response_meta)

        if modeled_fields is not None:
            service_model = self.client.meta.service_model
            shape = service_model.shape_for_error_code(service_error_code)
            self._validate_response(shape, modeled_fields)
            parsed_response.update(modeled_fields)

        operation_name = self.client.meta.method_to_api_mapping.get(method)
        # Note that we do not allow for expected_params while
        # adding errors into the queue yet.
        response = {
            'operation_name': operation_name,
            'response': (http_response, parsed_response),
            'expected_params': expected_params,
        }
        self._enqueue_response(response)

    def assert_no_pending_responses(self):
        """
        Asserts that all expected calls were made.
        """
        remaining = []
        for entry in self._queue:
            if isinstance(entry, _UnorderedGroup):
                remaining.extend(entry.responses)
            else:
                remaining.append(entry)
        if remaining:
            descriptions = '\n'.join(
                f"  {response['operation_name']} with expected params: "
                f"{pformat(response['expected_params'])}"
                for response in remaining
            )
            raise AssertionError(
                f"{len(remaining)} responses remaining in queue:\n"
                f"{descriptions}"
            )

    def _assert_expected_call_order(self, model, params):
        if not self._queue:
            raise UnStubbedResponseError(
                operation_name=model.name,
                reason=(
                    'Unexpected API Call: A call was made but no additional '
                    'calls expected. Either the API Call was not stubbed or '
                    'it was called multiple times.'
                ),
            )

        if isinstance(self._queue[0], _UnorderedGroup):
            raise UnStubbedResponseError(
                operation_name=model.name,
                reason=(
                    'Unexpected API Call: the call did not match any '
                    'remaining response in the current unordered group.'
                ),
            )

        name = self._queue[0]['operation_name']
        if name != model.name:
            raise StubResponseError(
                operation_name=model.name,
                reason=f'Operation mismatch: found response for {name}.',
            )

    def _set_account_id_for_endpoint_resolution(self, builtins, **kwargs):
        # Account ID comes from credentials and will try to resolve on endpoint resolution
        # when it's a builtin.  This breaks any stubber in environments where credentials
        # are not available.  We mock it to be a None value so that we don't attempt to
        # resolve credentials.
        if 'AWS::Auth::AccountId' in builtins:
            builtins['AWS::Auth::AccountId'] = None

    def _get_response_handler(self, model, params, context, **kwargs):
        with self._lock:
            matched = getattr(self._thread_local, 'matched_response', None)
            if matched is not None:
                self._thread_local.matched_response = None
                return self._consume_group_match(model, matched)
            self._assert_expected_call_order(model, params)
            # Pop off the entire response once everything has been validated
            return self._queue.popleft()['response']

    def _consume_group_match(self, model, matched):
        if self._queue and isinstance(self._queue[0], _UnorderedGroup):
            group = self._queue[0]
            response = group.consume_equivalent(matched)
            if response is not None:
                if len(group) == 0:
                    self._queue.popleft()
                return response
        raise UnStubbedResponseError(
            operation_name=model.name,
            reason=(
                'Unexpected API Call: the matching stubbed response in '
                'the current unordered group was already consumed by '
                'another call.'
            ),
        )

    def _assert_expected_params(self, model, params, context, **kwargs):
        if self._should_not_stub(context):
            return
        with self._lock:
            if self._queue and isinstance(self._queue[0], _UnorderedGroup):
                matched = self._queue[0].find_match(model.name, params)
                # The actual parameters are only available in this event,
                # so remember the match for this thread and consume it
                # when the corresponding before-call event fires.
                self._thread_local.matched_response = matched
                return
            self._assert_expected_call_order(model, params)
            expected_params = self._queue[0]['expected_params']
            if expected_params is None:
                return

            # Validate the parameters are equal
            for param, value in expected_params.items():
                if param not in params or expected_params[param] != params[param]:
                    raise StubAssertionError(
                        operation_name=model.name,
                        reason=(
                            f'Expected parameters:\n{pformat(expected_params)},\n'
                            f'but received:\n{pformat(params)}'
                        ),
                    )

            # Ensure there are no extra params hanging around
            if sorted(expected_params.keys()) != sorted(params.keys()):
                raise StubAssertionError(
                    operation_name=model.name,
                    reason=(
                        f'Expected parameters:\n{pformat(expected_params)},\n'
                        f'but received:\n{pformat(params)}'
                    ),
                )

    def _should_not_stub(self, context):
        # Do not include presign requests when processing stubbed client calls
        # as a presign request will never have an HTTP request sent over the
        # wire for it and therefore not receive a response back.
        if context and context.get('is_presign_request'):
            return True

    def _validate_operation_response(self, operation_name, service_response):
        service_model = self.client.meta.service_model
        operation_model = service_model.operation_model(operation_name)
        output_shape = operation_model.output_shape

        # Remove ResponseMetadata so that the validator doesn't attempt to
        # perform validation on it.
        response = service_response
        if 'ResponseMetadata' in response:
            response = copy.copy(service_response)
            del response['ResponseMetadata']

        self._validate_response(output_shape, response)

    def _validate_response(self, shape, response):
        if shape is not None:
            validate_parameters(response, shape)
        elif response:
            # If the output shape is None, that means the response should be
            # empty apart from ResponseMetadata
            raise ParamValidationError(
                report=(
                    "Service response should only contain ResponseMetadata."
                )
            )
