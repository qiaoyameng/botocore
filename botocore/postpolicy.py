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
"""Local validation of S3 presigned POST policies.

This module implements an optional, client-side consistency check for the
fields and conditions used to build a presigned POST. It understands the
``eq``, ``starts-with``, and ``content-length-range`` condition operators
as well as exact-match dictionary conditions, intersects every constraint
placed on the same field, and reports problems before anything is signed.
"""

from botocore.exceptions import (
    ContradictoryConditionsError,
    MissingFieldConditionError,
    UnknownConditionTypeError,
)

#: Condition operators understood by the validator.
KNOWN_CONDITION_OPERATORS = ('eq', 'starts-with', 'content-length-range')

#: Placeholder in a key that is substituted with the uploaded file name.
FILENAME_PLACEHOLDER = '${filename}'

# Fields that never require a matching condition in the policy.
_CONDITION_EXEMPT_FIELDS = frozenset(['file', 'policy', 'x-amz-signature'])

# Fields whose values must never appear in error messages because they
# contain credentials or signature material.
_SENSITIVE_FIELDS = frozenset(
    ['policy', 'x-amz-credential', 'x-amz-security-token', 'x-amz-signature']
)

#: Placeholder used in error messages instead of sensitive values.
REDACTED_VALUE = '<redacted>'


def _display_value(field, value):
    if field in _SENSITIVE_FIELDS:
        return REDACTED_VALUE
    return repr(value)


class _FieldConstraints:
    """Tracks and intersects the constraints placed on a single field."""

    def __init__(self, field):
        self.field = field
        self.exact = None
        self.prefix = None

    def add_exact(self, value):
        if self.exact is not None and self.exact != value:
            raise ContradictoryConditionsError(
                detail=(
                    f"field '{self.field}' must equal both "
                    f'{_display_value(self.field, self.exact)} and '
                    f'{_display_value(self.field, value)}'
                )
            )
        self.exact = value
        self._check_exact_against_prefix()

    def add_prefix(self, prefix):
        if self.prefix is None:
            self.prefix = prefix
        elif prefix.startswith(self.prefix):
            # The new prefix is stricter; it becomes the effective one.
            self.prefix = prefix
        elif not self.prefix.startswith(prefix):
            raise ContradictoryConditionsError(
                detail=(
                    f"field '{self.field}' cannot start with both "
                    f'{_display_value(self.field, self.prefix)} and '
                    f'{_display_value(self.field, prefix)}'
                )
            )
        self._check_exact_against_prefix()

    def _check_exact_against_prefix(self):
        if (
            self.exact is not None
            and self.prefix is not None
            and not self.exact.startswith(self.prefix)
        ):
            raise ContradictoryConditionsError(
                detail=(
                    f"field '{self.field}' must equal "
                    f'{_display_value(self.field, self.exact)} but also '
                    f'start with {_display_value(self.field, self.prefix)}'
                )
            )


class _ContentLengthRange:
    """Intersects content-length-range conditions."""

    def __init__(self):
        self.minimum = None
        self.maximum = None

    def add(self, minimum, maximum):
        new_minimum = (
            minimum if self.minimum is None else max(self.minimum, minimum)
        )
        new_maximum = (
            maximum if self.maximum is None else min(self.maximum, maximum)
        )
        if new_minimum > new_maximum:
            raise ContradictoryConditionsError(
                detail=(
                    'content-length-range conditions do not overlap; '
                    f'the intersection [{new_minimum}, {new_maximum}] '
                    'is empty'
                )
            )
        self.minimum = new_minimum
        self.maximum = new_maximum


def _normalize_field_reference(condition, operator):
    if len(condition) != 3:
        raise UnknownConditionTypeError(
            detail=(
                f"a '{operator}' condition takes exactly 3 elements, "
                f'got {len(condition)}'
            )
        )
    field = condition[1]
    if not isinstance(field, str) or not field.startswith('$'):
        raise UnknownConditionTypeError(
            detail=(
                f"a '{operator}' condition must reference a field as "
                "'$field'"
            )
        )
    return field[1:]


def _parse_length(value, operator):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise UnknownConditionTypeError(
            detail=(
                f"a '{operator}' condition requires integer bounds, "
                f'got {value!r}'
            )
        )


def validate_post_policy(fields, conditions):
    """Validates presigned POST fields and conditions for consistency.

    Every constraint placed on the same field is intersected. This
    includes the values of the form fields themselves, so automatically
    injected fields (such as ``bucket``, ``key``, and the ``x-amz-*``
    signing fields) are checked against user supplied conditions. A
    ``${filename}`` suffix in the ``key`` field is treated as a
    placeholder and only constrains the key prefix.

    :raises UnknownConditionTypeError: A condition uses an operator other
        than ``eq``, ``starts-with``, or ``content-length-range``, or is
        malformed.
    :raises ContradictoryConditionsError: The constraints on a field (or
        the content length range) can never be satisfied together.
    :raises MissingFieldConditionError: A form field is not covered by
        any condition in the policy.
    """
    constraints = {}
    covered_fields = set()
    length_range = _ContentLengthRange()

    def constraints_for(field):
        if field not in constraints:
            constraints[field] = _FieldConstraints(field)
        return constraints[field]

    for condition in conditions:
        if isinstance(condition, dict):
            for field, value in condition.items():
                covered_fields.add(field)
                if isinstance(value, str):
                    constraints_for(field).add_exact(value)
        elif isinstance(condition, (list, tuple)) and condition:
            operator = condition[0]
            if operator == 'eq':
                field = _normalize_field_reference(condition, operator)
                covered_fields.add(field)
                value = condition[2]
                if isinstance(value, str):
                    constraints_for(field).add_exact(value)
            elif operator == 'starts-with':
                field = _normalize_field_reference(condition, operator)
                covered_fields.add(field)
                prefix = condition[2]
                if not isinstance(prefix, str):
                    raise UnknownConditionTypeError(
                        detail=(
                            "a 'starts-with' condition requires a string "
                            f'prefix, got {prefix!r}'
                        )
                    )
                constraints_for(field).add_prefix(prefix)
            elif operator == 'content-length-range':
                if len(condition) != 3:
                    raise UnknownConditionTypeError(
                        detail=(
                            "a 'content-length-range' condition takes "
                            f'exactly 3 elements, got {len(condition)}'
                        )
                    )
                length_range.add(
                    _parse_length(condition[1], operator),
                    _parse_length(condition[2], operator),
                )
            else:
                raise UnknownConditionTypeError(
                    detail=f'unknown condition operator {operator!r}'
                )
        else:
            raise UnknownConditionTypeError(
                detail=(
                    'conditions must be dictionaries or non-empty lists, '
                    f'got {condition!r}'
                )
            )

    # Fold the form field values into the constraints so that conflicts
    # between automatically injected fields (bucket, key, x-amz-*) and
    # user supplied conditions are detected.
    for field, value in fields.items():
        if field in _CONDITION_EXEMPT_FIELDS or field.startswith('x-ignore-'):
            continue
        if not isinstance(value, str):
            continue
        if field == 'key' and value.endswith(FILENAME_PLACEHOLDER):
            # ${filename} is a placeholder for the uploaded file name,
            # not a literal part of the key.
            constraints_for(field).add_prefix(
                value[: -len(FILENAME_PLACEHOLDER)]
            )
        else:
            constraints_for(field).add_exact(value)

    for field in fields:
        if field in _CONDITION_EXEMPT_FIELDS or field.startswith('x-ignore-'):
            continue
        if field not in covered_fields:
            raise MissingFieldConditionError(field=field)
