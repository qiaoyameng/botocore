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


class _UnorderedGroup:
    """
    A set of expected responses that can be matched in any order.

    Responses are added to the group while it is open (see
    ``Stubber.unordered_group``). An incoming API call is matched against
    the remaining responses in the group by operation name and expected
    parameters rather than by queue position. The responses queued after
    the group are only matched once every response in the group has been
    consumed.
    """

    def __init__(self):
        self.responses = []


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


    If your code makes API calls concurrently and therefore cannot
    guarantee the order in which they are made, you can group the
    corresponding stubs with ``unordered_group``. Responses inside the
    group are matched by operation name and expected parameters instead
    of by queue position. Responses queued outside of the group keep
    their first in, first out ordering, and are only matched once every
    response inside the group has been consumed. A call that matches no
    response in the group, or that matches more than one, raises an
    error and consumes nothing.

    **Example:**
    ::
        import botocore.session
        from botocore.stub import Stubber


        s3 = botocore.session.get_session().create_client('s3')

        with Stubber(s3) as stubber:
            with stubber.unordered_group():
                stubber.add_response(
                    'list_objects', {}, {'Bucket': 'bucket-one'}
                )
                stubber.add_response(
                    'list_objects', {}, {'Bucket': 'bucket-two'}
                )
            # These calls can be made in either order.
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
        self._current_group = None
        self._lock = threading.Lock()
        self._group_match_context_key = 'boto_stubber_group_match'

    def __enter__(self):
        self.activate()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.deactivate()

    @contextmanager
    def unordered_group(self):
        """
        Groups stubbed responses so they can be matched in any order.

        Responses added inside the ``with`` block (via ``add_response``
        or ``add_client_error``) are matched against incoming API calls
        by operation name and expected parameters rather than by their
        position in the queue. This is useful when the code under test
        makes API calls concurrently and their order is not deterministic.
        Responses queued before or after the group keep their first in,
        first out ordering, and calls are never matched against responses
        queued after the group while the group still has remaining
        responses. Groups cannot be nested.
        """
        if self._current_group is not None:
            raise ValueError('Unordered groups cannot be nested.')
        group = _UnorderedGroup()
        self._current_group = group
        try:
            yield
        finally:
            self._current_group = None
            self._queue.append(group)

    def _queue_response(self, response):
        if self._current_group is not None:
            self._current_group.responses.append(response)
        else:
            self._queue.append(response)

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
        self._queue_response(response)

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
        self._queue_response(response)

    def assert_no_pending_responses(self):
        """
        Asserts that all expected calls were made.
        """
        remaining = []
        count = 0
        for item in self._queue:
            if isinstance(item, _UnorderedGroup):
                if item.responses:
                    count += len(item.responses)
                    names = [
                        response['operation_name']
                        for response in item.responses
                    ]
                    remaining.append(f"unordered group ({', '.join(names)})")
            else:
                count += 1
                remaining.append(item['operation_name'])
        if count != 0:
            raise AssertionError(
                f"{count} responses remaining in queue: "
                f"{', '.join(remaining)}."
            )

    def _peek_head(self):
        # Discard any unordered groups that have been fully consumed.
        while self._queue and self._is_empty_group(self._queue[0]):
            self._queue.popleft()
        if self._queue:
            return self._queue[0]
        return None

    @staticmethod
    def _is_empty_group(item):
        return isinstance(item, _UnorderedGroup) and not item.responses

    @staticmethod
    def _params_match(expected_params, params):
        if expected_params is None:
            return True
        for param, value in expected_params.items():
            if param not in params or value != params[param]:
                return False
        return sorted(expected_params.keys()) == sorted(params.keys())

    def _find_group_matches(self, group, operation_name, params):
        return [
            response
            for response in group.responses
            if response['operation_name'] == operation_name
            and self._params_match(response['expected_params'], params)
        ]

    def _assert_group_match(self, group, model, params):
        matches = self._find_group_matches(group, model.name, params)
        if not matches:
            remaining = [
                response['operation_name'] for response in group.responses
            ]
            if model.name not in remaining:
                raise StubResponseError(
                    operation_name=model.name,
                    reason=(
                        f'Operation mismatch: no response for {model.name} '
                        'remains in the current unordered group. Expected '
                        f'one of: {remaining}.'
                    ),
                )
            expected = [
                response['expected_params']
                for response in group.responses
                if response['operation_name'] == model.name
            ]
            raise StubAssertionError(
                operation_name=model.name,
                reason=(
                    f'Expected one of the parameters:\n{pformat(expected)},\n'
                    f'but received:\n{pformat(params)}'
                ),
            )
        if len(matches) > 1:
            raise StubResponseError(
                operation_name=model.name,
                reason=(
                    f'Ambiguous API Call: the call to {model.name} matches '
                    f'{len(matches)} responses in the current unordered '
                    'group. Make the expected parameters of the stubbed '
                    'responses in the group mutually exclusive.'
                ),
            )
        return matches

    def _assert_expected_call_order(self, model, params):
        head = self._peek_head()
        if head is None:
            raise UnStubbedResponseError(
                operation_name=model.name,
                reason=(
                    'Unexpected API Call: A call was made but no additional '
                    'calls expected. Either the API Call was not stubbed or '
                    'it was called multiple times.'
                ),
            )
        if isinstance(head, _UnorderedGroup):
            self._assert_group_match(head, model, params)
            return
        name = head['operation_name']
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
            head = self._peek_head()
            if isinstance(head, _UnorderedGroup):
                return self._get_group_response(head, model, context)
            self._assert_expected_call_order(model, params)
            # Pop off the entire response once everything has been validated
            return self._queue.popleft()['response']

    def _get_group_response(self, group, model, context):
        # The call was matched against the group when its parameters were
        # validated. Consume exactly the response that was matched so
        # that concurrent calls can never consume the same response twice.
        match = None
        if isinstance(context, dict):
            match = context.get(self._group_match_context_key)
        if match is not None and any(
            response is match for response in group.responses
        ):
            group.responses.remove(match)
            if not group.responses:
                self._queue.popleft()
            return match['response']
        raise UnStubbedResponseError(
            operation_name=model.name,
            reason=(
                'Unexpected API Call: A call was made but the response it '
                'matched in the current unordered group was already '
                'consumed. Either the API Call was not stubbed or it was '
                'called multiple times.'
            ),
        )

    def _assert_expected_params(self, model, params, context, **kwargs):
        if self._should_not_stub(context):
            return
        with self._lock:
            head = self._peek_head()
            if isinstance(head, _UnorderedGroup):
                # Match the call against the operation name and expected
                # parameters of the remaining responses in the group and
                # remember the match so that _get_response_handler can
                # consume exactly this response. The request context is
                # unique to each API call, making this safe to use from
                # concurrent threads.
                match = self._assert_group_match(head, model, params)[0]
                if isinstance(context, dict):
                    context[self._group_match_context_key] = match
                return
            self._assert_expected_call_order(model, params)
            expected_params = head['expected_params']
            if expected_params is None:
                return

            # Validate the parameters are equal
            for param, value in expected_params.items():
                if (
                    param not in params
                    or expected_params[param] != params[param]
                ):
                    raise StubAssertionError(
                        operation_name=model.name,
                        reason=(
                            f'Expected parameters:\n'
                            f'{pformat(expected_params)},\n'
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
