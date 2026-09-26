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
import hashlib
import json
import logging
import time
from functools import partial

import jmespath

from botocore.context import with_current_context
from botocore.docs.docstring import WaiterDocstring
from botocore.useragent import register_feature_id
from botocore.utils import get_service_module_name

from . import xform_name
from .exceptions import ClientError, WaiterConfigError, WaiterError

logger = logging.getLogger(__name__)


def create_waiter_with_client(waiter_name, waiter_model, client):
    """

    :type waiter_name: str
    :param waiter_name: The name of the waiter.  The name should match
        the name (including the casing) of the key name in the waiter
        model file (typically this is CamelCasing).

    :type waiter_model: botocore.waiter.WaiterModel
    :param waiter_model: The model for the waiter configuration.

    :type client: botocore.client.BaseClient
    :param client: The botocore client associated with the service.

    :rtype: botocore.waiter.Waiter
    :return: The waiter object.

    """
    single_waiter_config = waiter_model.get_waiter(waiter_name)
    operation_name = xform_name(single_waiter_config.operation)
    operation_method = NormalizedOperationMethod(
        getattr(client, operation_name)
    )

    # Create a new wait method that will serve as a proxy to the underlying
    # Waiter.wait method. This is needed to attach a docstring to the
    # method.
    def wait(self, **kwargs):
        Waiter.wait(self, **kwargs)

    wait.__doc__ = WaiterDocstring(
        waiter_name=waiter_name,
        event_emitter=client.meta.events,
        service_model=client.meta.service_model,
        service_waiter_model=waiter_model,
        include_signature=False,
    )

    # Rename the waiter class based on the type of waiter.
    waiter_class_name = str(
        f'{get_service_module_name(client.meta.service_model)}.Waiter.{waiter_name}'
    )

    # Create the new waiter class
    documented_waiter_cls = type(waiter_class_name, (Waiter,), {'wait': wait})

    # Return an instance of the new waiter class.
    return documented_waiter_cls(
        waiter_name, single_waiter_config, operation_method
    )


def is_valid_waiter_error(response):
    error = response.get('Error')
    if isinstance(error, dict) and 'Code' in error:
        return True
    return False


class NormalizedOperationMethod:
    def __init__(self, client_method):
        self._client_method = client_method

    def __call__(self, **kwargs):
        try:
            return self._client_method(**kwargs)
        except ClientError as e:
            return e.response


class WaiterModel:
    SUPPORTED_VERSION = 2

    def __init__(self, waiter_config):
        """

        Note that the WaiterModel takes ownership of the waiter_config.
        It may or may not mutate the waiter_config.  If this is a concern,
        it is best to make a copy of the waiter config before passing it to
        the WaiterModel.

        :type waiter_config: dict
        :param waiter_config: The loaded waiter config
            from the <service>*.waiters.json file.  This can be
            obtained from a botocore Loader object as well.

        """
        self._waiter_config = waiter_config['waiters']

        # These are part of the public API.  Changing these
        # will result in having to update the consuming code,
        # so don't change unless you really need to.
        version = waiter_config.get('version', 'unknown')
        self._verify_supported_version(version)
        self.version = version
        self.waiter_names = list(sorted(waiter_config['waiters'].keys()))

    def _verify_supported_version(self, version):
        if version != self.SUPPORTED_VERSION:
            raise WaiterConfigError(
                error_msg=(
                    "Unsupported waiter version, supported version "
                    f"must be: {self.SUPPORTED_VERSION}, but version "
                    f"of waiter config is: {version}"
                )
            )

    def get_waiter(self, waiter_name):
        try:
            single_waiter_config = self._waiter_config[waiter_name]
        except KeyError:
            raise ValueError(f"Waiter does not exist: {waiter_name}")
        return SingleWaiterConfig(single_waiter_config)


class SingleWaiterConfig:
    """Represents the waiter configuration for a single waiter.

    A single waiter is considered the configuration for a single
    value associated with a named waiter (i.e TableExists).

    """

    def __init__(self, single_waiter_config):
        self._config = single_waiter_config

        # These attributes are part of the public API.
        self.description = single_waiter_config.get('description', '')
        # Per the spec, these three fields are required.
        self.operation = single_waiter_config['operation']
        self.delay = single_waiter_config['delay']
        self.max_attempts = single_waiter_config['maxAttempts']

    @property
    def acceptors(self):
        acceptors = []
        for acceptor_config in self._config['acceptors']:
            acceptor = AcceptorConfig(acceptor_config)
            acceptors.append(acceptor)
        return acceptors


class AcceptorConfig:
    def __init__(self, config):
        self.state = config['state']
        self.matcher = config['matcher']
        self.expected = config['expected']
        self.argument = config.get('argument')
        self.matcher_func = self._create_matcher_func()

    @property
    def explanation(self):
        if self.matcher == 'path':
            return f'For expression "{self.argument}" we matched expected path: "{self.expected}"'
        elif self.matcher == 'pathAll':
            return (
                f'For expression "{self.argument}" all members matched '
                f'expected path: "{self.expected}"'
            )
        elif self.matcher == 'pathAny':
            return (
                f'For expression "{self.argument}" we matched expected '
                f'path: "{self.expected}" at least once'
            )
        elif self.matcher == 'status':
            return f'Matched expected HTTP status code: {self.expected}'
        elif self.matcher == 'error':
            return f'Matched expected service error code: {self.expected}'
        else:
            return f'No explanation for unknown waiter type: "{self.matcher}"'

    def _create_matcher_func(self):
        # An acceptor function is a callable that takes a single value.  The
        # parsed AWS response.  Note that the parsed error response is also
        # provided in the case of errors, so it's entirely possible to
        # handle all the available matcher capabilities in the future.
        # There's only three supported matchers, so for now, this is all
        # contained to a single method.  If this grows, we can expand this
        # out to separate methods or even objects.

        if self.matcher == 'path':
            return self._create_path_matcher()
        elif self.matcher == 'pathAll':
            return self._create_path_all_matcher()
        elif self.matcher == 'pathAny':
            return self._create_path_any_matcher()
        elif self.matcher == 'status':
            return self._create_status_matcher()
        elif self.matcher == 'error':
            return self._create_error_matcher()
        else:
            raise WaiterConfigError(
                error_msg=f"Unknown acceptor: {self.matcher}"
            )

    def _create_path_matcher(self):
        expression = jmespath.compile(self.argument)
        expected = self.expected

        def acceptor_matches(response):
            if is_valid_waiter_error(response):
                return
            return expression.search(response) == expected

        return acceptor_matches

    def _create_path_all_matcher(self):
        expression = jmespath.compile(self.argument)
        expected = self.expected

        def acceptor_matches(response):
            if is_valid_waiter_error(response):
                return
            result = expression.search(response)
            if not isinstance(result, list) or not result:
                # pathAll matcher must result in a list.
                # Also we require at least one element in the list,
                # that is, an empty list should not result in this
                # acceptor match.
                return False
            for element in result:
                if element != expected:
                    return False
            return True

        return acceptor_matches

    def _create_path_any_matcher(self):
        expression = jmespath.compile(self.argument)
        expected = self.expected

        def acceptor_matches(response):
            if is_valid_waiter_error(response):
                return
            result = expression.search(response)
            if not isinstance(result, list) or not result:
                # pathAny matcher must result in a list.
                # Also we require at least one element in the list,
                # that is, an empty list should not result in this
                # acceptor match.
                return False
            for element in result:
                if element == expected:
                    return True
            return False

        return acceptor_matches

    def _create_status_matcher(self):
        expected = self.expected

        def acceptor_matches(response):
            # We don't have any requirements on the expected incoming data
            # other than it is a dict, so we don't assume there's
            # a ResponseMetadata.HTTPStatusCode.
            status_code = response.get('ResponseMetadata', {}).get(
                'HTTPStatusCode'
            )
            return status_code == expected

        return acceptor_matches

    def _create_error_matcher(self):
        expected = self.expected

        def acceptor_matches(response):
            # When the client encounters an error, it will normally raise
            # an exception.  However, the waiter implementation will catch
            # this exception, and instead send us the parsed error
            # response.  So response is still a dictionary, and in the case
            # of an error response will contain the "Error" and
            # "ResponseMetadata" key.
            # When expected is True, accept any error code.
            # When expected is False, check if any errors were encountered.
            # Otherwise, check for a specific AWS error code.
            if expected is True:
                return "Error" in response and "Code" in response["Error"]
            elif expected is False:
                return "Error" not in response
            else:
                return response.get("Error", {}).get("Code", "") == expected

        return acceptor_matches


class _SystemClock:
    """Default clock used by waiters, backed by the ``time`` module."""

    def time(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)


class WaiterState:
    """A serializable checkpoint of a waiter's progress.

    The state can be serialized to a JSON token via ``to_json()`` and
    later restored via ``from_json()``, allowing a wait to be resumed
    by a new ``Waiter`` instance (potentially in a different process).

    The token intentionally only contains bookkeeping information: the
    number of attempts consumed so far, the next time a poll is allowed,
    the total attempt limit, the current status, and a fingerprint of
    the waiter model and request parameters.  It never contains
    credentials, request parameters, or response payloads.
    """

    VERSION = 1
    STATUS_WAITING = 'waiting'
    STATUS_SUCCESS = 'success'
    STATUS_FAILURE = 'failure'
    _ALL_STATUSES = (STATUS_WAITING, STATUS_SUCCESS, STATUS_FAILURE)
    _TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILURE)

    def __init__(
        self,
        fingerprint,
        max_attempts,
        attempts=0,
        next_poll_at=None,
        status=STATUS_WAITING,
        reason=None,
    ):
        self.fingerprint = fingerprint
        self.max_attempts = max_attempts
        self.attempts = attempts
        self.next_poll_at = next_poll_at
        self.status = status
        self.reason = reason
        # The last response is only kept for the lifetime of this object
        # so it can be attached to raised WaiterErrors.  It is never
        # serialized into the JSON token.
        self.last_response = None

    @property
    def is_terminal(self):
        return self.status in self._TERMINAL_STATUSES

    def to_json(self):
        """Serialize the state to a JSON token."""
        return json.dumps(
            {
                'version': self.VERSION,
                'fingerprint': self.fingerprint,
                'attempts': self.attempts,
                'max_attempts': self.max_attempts,
                'next_poll_at': self.next_poll_at,
                'status': self.status,
                'reason': self.reason,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, payload):
        """Restore a state from a JSON token.

        :raises ValueError: If the token is malformed or uses an
            unsupported version.
        """
        try:
            data = json.loads(payload)
        except (TypeError, ValueError) as e:
            raise ValueError(f'Invalid waiter state token: {e}')
        if not isinstance(data, dict):
            raise ValueError(
                'Invalid waiter state token: expected a JSON object'
            )
        if data.get('version') != cls.VERSION:
            raise ValueError(
                f'Unsupported waiter state version: {data.get("version")}'
            )
        try:
            state = cls(
                fingerprint=data['fingerprint'],
                max_attempts=data['max_attempts'],
                attempts=data['attempts'],
                next_poll_at=data['next_poll_at'],
                status=data['status'],
                reason=data['reason'],
            )
        except KeyError as e:
            raise ValueError(
                f'Invalid waiter state token, missing field: {e}'
            )
        if state.status not in cls._ALL_STATUSES:
            raise ValueError(f'Invalid waiter status: {state.status}')
        return state


class Waiter:
    def __init__(self, name, config, operation_method, clock=None):
        """

        :type name: string
        :param name: The name of the waiter

        :type config: botocore.waiter.SingleWaiterConfig
        :param config: The configuration for the waiter.

        :type operation_method: callable
        :param operation_method: A callable that accepts **kwargs
            and returns a response.  For example, this can be
            a method from a botocore client.

        :type clock: object
        :param clock: An optional clock object providing ``time()`` and
            ``sleep(seconds)`` methods.  This is primarily useful for
            testing and for resumable waits that need to reason about
            time.  Defaults to a clock backed by the ``time`` module.

        """
        self._operation_method = operation_method
        self._clock = clock if clock is not None else _SystemClock()
        # The two attributes are exposed to allow for introspection
        # and documentation.
        self.name = name
        self.config = config

    @with_current_context(partial(register_feature_id, 'WAITER'))
    def wait(self, **kwargs):
        # pop the invocation specific config
        config = kwargs.pop('WaiterConfig', {})
        sleep_amount = config.get('Delay', self.config.delay)
        max_attempts = config.get('MaxAttempts', self.config.max_attempts)
        state = WaiterState(fingerprint=None, max_attempts=max_attempts)
        self._run_wait_loop(state, kwargs, sleep_amount)
        if state.status == WaiterState.STATUS_SUCCESS:
            return
        raise WaiterError(
            name=self.name,
            reason=state.reason,
            last_response=state.last_response,
        )

    @with_current_context(partial(register_feature_id, 'WAITER'))
    def wait_resumable(self, **kwargs):
        """Poll the waiter, returning a JSON state token when not done.

        This behaves like ``wait()`` except that the wait can be split
        across multiple invocations (and processes).  Each call returns
        a JSON state token (see :class:`WaiterState`) unless the wait
        reaches a terminal failure state, in which case a
        :class:`botocore.exceptions.WaiterError` is raised with the
        state token attached as its ``state`` attribute.

        In addition to the ``Delay`` and ``MaxAttempts`` keys supported
        by ``wait()``, the ``WaiterConfig`` argument supports:

        * ``RunAttempts`` - The maximum number of polls to perform in
          this call before returning a ``waiting`` state token.  If not
          provided, the call polls until a terminal state is reached.
        * ``ResumeState`` - A JSON state token returned by a previous
          ``wait_resumable()`` call.  The wait then continues where the
          previous call left off, honoring the original ``MaxAttempts``
          limit and the earliest allowed time of the next poll.

        Resuming is refused with a ``WaiterError`` if the waiter model
        or request parameters no longer match the state token, or if
        the token already reached a terminal state.

        :rtype: str
        :return: A JSON state token.  Its ``status`` field is one of
            ``waiting`` or ``success`` (failures are raised as
            ``WaiterError``).
        """
        # pop the invocation specific config
        config = dict(kwargs.pop('WaiterConfig', {}))
        resume_token = config.pop('ResumeState', None)
        max_run_attempts = config.pop('RunAttempts', None)
        sleep_amount = config.get('Delay', self.config.delay)
        fingerprint = self._fingerprint(kwargs, config)
        if resume_token is None:
            state = WaiterState(
                fingerprint=fingerprint,
                max_attempts=config.get(
                    'MaxAttempts', self.config.max_attempts
                ),
            )
        else:
            state = self._load_resume_state(resume_token, fingerprint)
            if state.status == WaiterState.STATUS_SUCCESS:
                # A completed wait never issues further requests.
                return state.to_json()
            if state.status == WaiterState.STATUS_FAILURE:
                self._raise_failure(state)
        self._run_wait_loop(state, kwargs, sleep_amount, max_run_attempts)
        if state.status == WaiterState.STATUS_FAILURE:
            self._raise_failure(state)
        return state.to_json()

    def _fingerprint(self, kwargs, invocation_config):
        acceptors = [
            {
                'state': acceptor.state,
                'matcher': acceptor.matcher,
                'expected': acceptor.expected,
                'argument': acceptor.argument,
            }
            for acceptor in self.config.acceptors
        ]
        payload = {
            'name': self.name,
            'operation': self.config.operation,
            'delay': self.config.delay,
            'max_attempts': self.config.max_attempts,
            'acceptors': acceptors,
            'invocation_config': invocation_config,
            'request': kwargs,
        }
        # Only a hash of the model and request parameters is stored in
        # the state token, never the values themselves, so credentials
        # or other sensitive parameter values are never persisted.
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _load_resume_state(self, token, fingerprint):
        try:
            state = WaiterState.from_json(token)
        except ValueError as e:
            raise WaiterError(
                name=self.name,
                reason=f'Cannot resume waiter: {e}',
                last_response=None,
            )
        if state.fingerprint != fingerprint:
            raise WaiterError(
                name=self.name,
                reason=(
                    'Cannot resume waiter: the waiter model or request '
                    'parameters do not match the provided state token.'
                ),
                last_response=None,
            )
        return state

    def _raise_failure(self, state):
        error = WaiterError(
            name=self.name,
            reason=state.reason,
            last_response=state.last_response,
        )
        error.state = state.to_json()
        raise error

    def _run_wait_loop(self, state, kwargs, sleep_amount,
                       max_run_attempts=None):
        acceptors = list(self.config.acceptors)
        last_matched_acceptor = None
        run_attempts = 0
        if state.attempts >= state.max_attempts:
            state.status = WaiterState.STATUS_FAILURE
            state.reason = 'Max attempts exceeded'
            return
        if max_run_attempts is not None and max_run_attempts <= 0:
            return
        if state.next_poll_at is not None:
            # A resumed wait first has to wait until the next poll
            # is allowed before making another request.
            remaining = state.next_poll_at - self._clock.time()
            if remaining > 0:
                self._clock.sleep(remaining)
        while True:
            if run_attempts > 0:
                self._clock.sleep(sleep_amount)
            response = self._operation_method(**kwargs)
            state.attempts += 1
            run_attempts += 1
            state.last_response = response
            state.next_poll_at = self._clock.time() + sleep_amount
            matched_acceptor = None
            for acceptor in acceptors:
                if acceptor.matcher_func(response):
                    matched_acceptor = acceptor
                    last_matched_acceptor = acceptor
                    break
            if matched_acceptor is None and is_valid_waiter_error(response):
                # If none of the acceptors matched, we should
                # transition to the failure state if an error
                # response was received.
                state.status = WaiterState.STATUS_FAILURE
                state.reason = 'An error occurred ({}): {}'.format(
                    response['Error'].get('Code', 'Unknown'),
                    response['Error'].get('Message', 'Unknown'),
                )
                return
            if matched_acceptor is not None:
                if matched_acceptor.state == 'success':
                    logger.debug(
                        "Waiting complete, waiter matched the "
                        "success state."
                    )
                    state.status = WaiterState.STATUS_SUCCESS
                    return
                if matched_acceptor.state == 'failure':
                    state.status = WaiterState.STATUS_FAILURE
                    state.reason = (
                        'Waiter encountered a terminal failure state: '
                        f'{matched_acceptor.explanation}'
                    )
                    return
            if state.attempts >= state.max_attempts:
                state.status = WaiterState.STATUS_FAILURE
                if last_matched_acceptor is None:
                    state.reason = 'Max attempts exceeded'
                else:
                    state.reason = (
                        f'Max attempts exceeded. Previously accepted state: '
                        f'{acceptor.explanation}'
                    )
                return
            if (
                max_run_attempts is not None
                and run_attempts >= max_run_attempts
            ):
                # The budget for this run is exhausted, but the wait
                # itself is not done yet.  The caller can persist the
                # state token and resume later.
                return
