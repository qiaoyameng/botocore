"""Generate minimal example request parameters from an operation model.

This module is intended for internal testing scenarios where you need a
minimal request that satisfies parameter validation without actually
calling a service.  Given an ``OperationModel`` it produces a dict of
input parameters that passes ``botocore.validate.ParamValidator``.

The generator:

* Honors ``required`` members, ``enum`` values, numeric (``min``/``max``)
  and length ranges, structures, lists, maps, and tagged unions.
* Uses caller-controllable fixed seed values for timestamps and blobs so
  generated examples are deterministic.
* Omits optional members by default to keep examples minimal.
* Detects recursive required structures that have no finite instance and
  raises :class:`ExampleGenerationError` (carrying the shape path)
  instead of recursing forever.
* Allows each tagged union branch to be explicitly selected via
  ``union_choices``.
"""

from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError

#: Default fixed seed values, overridable via the constructor.
DEFAULT_TIMESTAMP_SEED = datetime(2024, 1, 1, tzinfo=timezone.utc)
DEFAULT_BLOB_SEED = b'blob'

_CONTAINER_TYPES = ('structure', 'list', 'map')


class ExampleGenerationError(BotoCoreError):
    """Raised when no valid example can be generated for a shape.

    :ivar path: The dotted shape path (from the operation input shape)
        where generation failed, e.g. ``"PutItemInput.Item.Value"``.
    :ivar reason: A human readable explanation of the failure.
    """

    def __init__(self, path, reason):
        self.path = path
        self.reason = reason
        super().__init__(
            msg=f'Unable to generate example at {path}: {reason}'
        )


class RequestExampleGenerator:
    """Generates minimal example input parameters for an operation.

    :type timestamp_seed: datetime.datetime or str
    :param timestamp_seed: Fixed value used for every ``timestamp``
        member.  Defaults to ``2024-01-01T00:00:00Z``.

    :type blob_seed: bytes
    :param blob_seed: Fixed value used for every ``blob`` member.
        Defaults to ``b'blob'``.
    """

    def __init__(self, timestamp_seed=None, blob_seed=None):
        if timestamp_seed is None:
            timestamp_seed = DEFAULT_TIMESTAMP_SEED
        if blob_seed is None:
            blob_seed = DEFAULT_BLOB_SEED
        self._timestamp_seed = timestamp_seed
        self._blob_seed = blob_seed

    def generate_example(self, operation_model, union_choices=None):
        """Generate example input parameters for an operation.

        :type operation_model: botocore.model.OperationModel
        :param operation_model: The operation to generate input for.

        :type union_choices: dict
        :param union_choices: Optional mapping selecting which branch to
            use for tagged union shapes.  Keys may be either a union
            shape name (applies everywhere the shape is used) or a full
            dotted shape path (applies to a single occurrence, and takes
            precedence).  Values are member names of the union.

        :rtype: dict
        :return: A dict of input parameters that satisfies
            ``botocore.validate.ParamValidator`` for the operation's
            input shape.

        :raise ExampleGenerationError: If no finite valid instance
            exists (e.g. a recursive required structure or contradictory
            ``min``/``max`` constraints).
        """
        input_shape = operation_model.input_shape
        if input_shape is None:
            return {}
        if union_choices is None:
            union_choices = {}
        return self._generate(
            input_shape, input_shape.name, (), union_choices
        )

    def _generate(self, shape, path, stack, union_choices):
        type_name = shape.type_name
        if type_name == 'structure' and shape.is_document_type:
            # Document types are free-form; an empty dict is valid.
            return {}
        if type_name in _CONTAINER_TYPES:
            if shape.name in stack:
                raise ExampleGenerationError(
                    path,
                    f'recursive shape {shape.name!r} has no finite '
                    f'instance (cycle: {" -> ".join(stack + (shape.name,))})',
                )
            stack = stack + (shape.name,)
        handler = getattr(self, f'_generate_{type_name}')
        return handler(shape, path, stack, union_choices)

    def _generate_structure(self, shape, path, stack, union_choices):
        if shape.is_tagged_union:
            return self._generate_union(shape, path, stack, union_choices)
        example = {}
        members = shape.members
        for member_name in shape.required_members:
            example[member_name] = self._generate(
                members[member_name],
                f'{path}.{member_name}',
                stack,
                union_choices,
            )
        return example

    def _generate_union(self, shape, path, stack, union_choices):
        members = shape.members
        choice = union_choices.get(path, union_choices.get(shape.name))
        if choice is not None:
            if choice not in members:
                raise ExampleGenerationError(
                    path,
                    f'unknown union branch {choice!r} for shape '
                    f'{shape.name!r}; valid branches: '
                    f'{", ".join(members)}',
                )
            return {
                choice: self._generate(
                    members[choice], f'{path}.{choice}', stack, union_choices
                )
            }
        failures = []
        for member_name, member_shape in members.items():
            try:
                return {
                    member_name: self._generate(
                        member_shape,
                        f'{path}.{member_name}',
                        stack,
                        union_choices,
                    )
                }
            except ExampleGenerationError as e:
                failures.append(f'{member_name}: {e.reason}')
        raise ExampleGenerationError(
            path,
            f'no branch of union {shape.name!r} has a finite instance '
            f'({"; ".join(failures)})',
        )

    def _generate_list(self, shape, path, stack, union_choices):
        min_length = shape.metadata.get('min', 0)
        max_length = shape.metadata.get('max')
        if max_length is not None and min_length > max_length:
            raise self._contradiction_error(
                shape, path, min_length, max_length
            )
        member_shape = shape.member
        return [
            self._generate(
                member_shape, f'{path}[{i}]', stack, union_choices
            )
            for i in range(min_length)
        ]

    def _generate_map(self, shape, path, stack, union_choices):
        # An empty map always satisfies the validator and keeps the
        # example minimal.
        return {}

    def _generate_string(self, shape, path, stack, union_choices):
        enum = shape.metadata.get('enum')
        if enum:
            return enum[0]
        min_length = self._effective_min(shape, default=0)
        max_length = shape.metadata.get('max')
        if max_length is not None and min_length > max_length:
            raise self._contradiction_error(
                shape, path, min_length, max_length
            )
        return 'x' * min_length

    def _generate_integer(self, shape, path, stack, union_choices):
        return int(self._constrained_number(shape, path))

    _generate_long = _generate_integer

    def _generate_float(self, shape, path, stack, union_choices):
        return float(self._constrained_number(shape, path))

    _generate_double = _generate_float

    def _generate_boolean(self, shape, path, stack, union_choices):
        return False

    def _generate_timestamp(self, shape, path, stack, union_choices):
        return self._timestamp_seed

    def _generate_blob(self, shape, path, stack, union_choices):
        min_length = shape.metadata.get('min', 0)
        seed = self._blob_seed
        if isinstance(seed, str):
            seed = seed.encode('utf-8')
        if not min_length:
            return seed
        if not seed:
            seed = b'\x00'
        repeats = min_length // len(seed) + 1
        return (seed * repeats)[:min_length]

    def _constrained_number(self, shape, path):
        minimum = self._effective_min(shape, default=None)
        maximum = shape.metadata.get('max')
        if minimum is not None and maximum is not None and minimum > maximum:
            raise self._contradiction_error(shape, path, minimum, maximum)
        if minimum is not None:
            return minimum
        if maximum is not None:
            return min(0, maximum)
        return 0

    def _effective_min(self, shape, default):
        # Mirrors botocore.validate.range_check: members bound to the
        # host have an implicit minimum of 1.
        if 'min' in shape.metadata:
            return shape.metadata['min']
        if shape.serialization.get('hostLabel'):
            return 1
        return default

    def _contradiction_error(self, shape, path, minimum, maximum):
        return ExampleGenerationError(
            path,
            f'contradictory constraints on shape {shape.name!r}: '
            f'min ({minimum}) is greater than max ({maximum})',
        )


def generate_example(operation_model, union_choices=None, **seed_kwargs):
    """Generate example input parameters for an operation.

    This is a convenience function that creates a
    :class:`RequestExampleGenerator` with the given seed values.  See
    :meth:`RequestExampleGenerator.generate_example` for details.
    """
    generator = RequestExampleGenerator(**seed_kwargs)
    return generator.generate_example(operation_model, union_choices)
