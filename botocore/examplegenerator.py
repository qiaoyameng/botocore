"""Generate example request parameters from operation models.

This module is intended for internal testing workflows that need a
minimal set of request parameters satisfying the modeled constraints of
an operation.  It only builds the parameter dict based on the
operation's input shape; it never calls a service.

The generated parameters honor ``required`` members, ``enum`` values,
``min``/``max`` numeric and length ranges, structures, lists, maps and
tagged unions.  Optional members are omitted by default.  Timestamps and
blobs are derived from a caller controllable seed so that generated
examples are deterministic.
"""

import hashlib
from datetime import datetime, timedelta, timezone

DEFAULT_SEED = 0
DEFAULT_TIMESTAMP = datetime(2020, 1, 1, tzinfo=timezone.utc)


class ExampleGenerationError(Exception):
    """Raised when no valid example value can be generated for a shape."""

    def __init__(self, shape_path, reason):
        self.shape_path = shape_path
        self.reason = reason
        super().__init__(
            f'Unable to generate an example for "{shape_path}": {reason}'
        )


class _Unsatisfiable(Exception):
    """Internal signal that a shape has no finite valid instance."""

    def __init__(self, shape_path, reason):
        self.shape_path = shape_path
        self.reason = reason
        super().__init__(reason)


class _Context:
    """Internal state threaded through the generation recursion."""

    __slots__ = (
        'visiting',
        'union_selection',
        'include_optional',
        'reentry_allowed',
    )

    def __init__(
        self,
        visiting=(),
        union_selection=None,
        include_optional=False,
        reentry_allowed=frozenset(),
    ):
        self.visiting = visiting
        self.union_selection = union_selection or {}
        self.include_optional = include_optional
        self.reentry_allowed = reentry_allowed

    def enter(self, shape, path):
        """Mark a container shape as being expanded on the current path.

        A structure, list or map may only appear once on a path: their
        required members leave no choice, so a repeated occurrence can
        only recurse forever.  A union may appear twice, but only when
        the first occurrence was explicitly selected through
        ``union_selection``; the nested occurrence then terminates
        through a non-recursive branch.
        """
        if shape.name in self.visiting:
            if shape.name not in self.reentry_allowed:
                cycle = self.visiting[
                    self.visiting.index(shape.name) :
                ] + (shape.name,)
                raise _Unsatisfiable(
                    path,
                    'recursive shape has no finite instance: '
                    + ' -> '.join(cycle),
                )
            reentry_allowed = self.reentry_allowed - {shape.name}
        else:
            reentry_allowed = self.reentry_allowed
        return self._replace(
            visiting=self.visiting + (shape.name,),
            reentry_allowed=reentry_allowed,
        )

    def select_union_member(self, shape, path):
        """Return the explicitly selected member for a union, if any.

        The selection is consumed so that nested occurrences of the same
        union shape fall back to the default branch selection.
        """
        selected = self.union_selection.get(shape.name)
        if selected is None:
            return None, self
        if selected not in shape.members:
            valid = ', '.join(sorted(shape.members))
            raise ExampleGenerationError(
                path,
                f'"{selected}" is not a member of union shape '
                f'"{shape.name}", valid members are: {valid}',
            )
        nested_selection = dict(self.union_selection)
        del nested_selection[shape.name]
        context = self._replace(
            union_selection=nested_selection,
            reentry_allowed=self.reentry_allowed | {shape.name},
        )
        return selected, context

    def _replace(self, **kwargs):
        values = {
            'visiting': self.visiting,
            'union_selection': self.union_selection,
            'include_optional': self.include_optional,
            'reentry_allowed': self.reentry_allowed,
        }
        values.update(kwargs)
        return _Context(**values)


def generate_example(operation_model, seed=DEFAULT_SEED, union_selection=None):
    """Generate minimal request parameters for an operation.

    This is a convenience function around :class:`ExampleGenerator`.  See
    :meth:`ExampleGenerator.generate` for the full documentation.
    """
    generator = ExampleGenerator(seed=seed)
    return generator.generate(
        operation_model, union_selection=union_selection
    )


class ExampleGenerator:
    """Generates example request parameters from operation models.

    :type seed: int
    :param seed: Seed used to derive deterministic timestamp and blob
        values.  Two generators created with the same seed produce
        identical examples.

    :type timestamp: datetime.datetime
    :param timestamp: Explicit value to use for all timestamp members.
        Overrides the seed derived timestamp.

    :type blob: bytes
    :param blob: Explicit value to use for all blob members.  Overrides
        the seed derived blob.
    """

    def __init__(self, seed=DEFAULT_SEED, timestamp=None, blob=None):
        self._seed = seed
        if timestamp is None:
            timestamp = DEFAULT_TIMESTAMP + timedelta(seconds=seed)
        self._timestamp = timestamp
        self._blob = blob

    def generate(
        self, operation_model, union_selection=None, include_optional=False
    ):
        """Generate minimal request parameters for an operation.

        Only members required by the model are included unless
        ``include_optional`` is set.  The returned dict is not validated
        against or sent to any service.

        :type operation_model: botocore.model.OperationModel
        :param operation_model: The operation to generate parameters for.

        :type union_selection: dict
        :param union_selection: Maps a tagged union shape name to the
            member name that should be selected for the first occurrence
            of that union.  Nested occurrences of the same union shape
            fall back to the default branch selection so that recursive
            unions remain finite.  Any union not present in the mapping
            uses the first member that has a finite valid instance.

        :type include_optional: bool
        :param include_optional: When True, optional members are also
            generated.  Optional members that have no finite valid
            instance (for example infinitely recursive structures) are
            silently omitted.

        :rtype: dict
        :return: The generated request parameters.

        :raises ExampleGenerationError: If no finite valid instance of
            the input shape exists (for example contradictory
            constraints or infinitely recursive required structures).
        """
        input_shape = operation_model.input_shape
        if input_shape is None:
            return {}
        return self.generate_value(
            input_shape,
            union_selection=union_selection,
            include_optional=include_optional,
        )

    def generate_value(
        self, shape, union_selection=None, include_optional=False
    ):
        """Generate an example value for an arbitrary shape.

        This accepts the same ``union_selection`` and
        ``include_optional`` arguments as :meth:`generate`.
        """
        context = _Context(
            union_selection=union_selection,
            include_optional=include_optional,
        )
        try:
            return self._generate(shape, shape.name, context)
        except _Unsatisfiable as e:
            raise ExampleGenerationError(e.shape_path, e.reason)

    def _generate(self, shape, path, context):
        type_name = shape.type_name
        if type_name == 'structure':
            return self._generate_structure(shape, path, context)
        elif type_name == 'list':
            return self._generate_list(shape, path, context)
        elif type_name == 'map':
            return self._generate_map(shape, path, context)
        elif type_name == 'string':
            return self._generate_string(shape, path)
        elif type_name in ('integer', 'long'):
            return self._generate_number(shape, path, integer=True)
        elif type_name in ('float', 'double'):
            return self._generate_number(shape, path, integer=False)
        elif type_name == 'boolean':
            return False
        elif type_name == 'timestamp':
            return self._timestamp
        elif type_name == 'blob':
            return self._generate_blob(shape, path)
        raise ExampleGenerationError(
            path, f'unsupported shape type "{type_name}"'
        )

    def _effective_min_max(self, shape):
        metadata = shape.metadata
        min_value = metadata.get('min')
        max_value = metadata.get('max')
        if shape.serialization.get('hostLabel'):
            # Members bound to the host label have an implicit min of 1.
            min_value = max(min_value or 0, 1)
        return min_value, max_value

    def _check_range(self, shape, path):
        min_value, max_value = self._effective_min_max(shape)
        if (
            min_value is not None
            and max_value is not None
            and min_value > max_value
        ):
            raise _Unsatisfiable(
                path,
                f'minimum {min_value} exceeds maximum {max_value}',
            )
        return min_value, max_value

    def _length_satisfies(self, length, min_length, max_length):
        if min_length is not None and length < min_length:
            return False
        if max_length is not None and length > max_length:
            return False
        return True

    def _generate_number(self, shape, path, integer):
        min_value, max_value = self._check_range(shape, path)
        value = max(min_value or 0, 0)
        if max_value is not None and value > max_value:
            value = max_value
        if integer:
            return int(value)
        return float(value)

    def _generate_string(self, shape, path):
        min_length, max_length = self._check_range(shape, path)
        enum = shape.metadata.get('enum', [])
        if enum:
            for candidate in enum:
                if self._length_satisfies(
                    len(candidate), min_length, max_length
                ):
                    return candidate
            raise _Unsatisfiable(
                path,
                'no enum value satisfies the length constraints '
                f'(min={min_length}, max={max_length})',
            )
        length = min_length or 0
        return 'a' * length

    def _generate_blob(self, shape, path):
        min_length, max_length = self._check_range(shape, path)
        if self._blob is not None:
            return self._blob
        length = max(min_length or 0, 1)
        digest = hashlib.sha256(
            f'botocore-example-blob:{self._seed}'.encode('utf-8')
        ).digest()
        repeats = length // len(digest) + 1
        return (digest * repeats)[:length]

    def _generate_structure(self, shape, path, context):
        if shape.is_document_type:
            # Document types accept any JSON serializable value.
            return {}
        if shape.is_tagged_union:
            return self._generate_union(shape, path, context)
        context = context.enter(shape, path)
        members = shape.members
        result = {}
        for member_name in shape.required_members:
            member_shape = members[member_name]
            result[member_name] = self._generate(
                member_shape, f'{path}.{member_name}', context
            )
        if context.include_optional:
            for member_name, member_shape in members.items():
                if member_name in result:
                    continue
                try:
                    result[member_name] = self._generate(
                        member_shape, f'{path}.{member_name}', context
                    )
                except _Unsatisfiable:
                    continue
        return result

    def _generate_union(self, shape, path, context):
        context = context.enter(shape, path)
        members = shape.members
        selected, nested_context = context.select_union_member(shape, path)
        if selected is not None:
            return {
                selected: self._generate(
                    members[selected], f'{path}.{selected}', nested_context
                )
            }
        failures = []
        for member_name, member_shape in members.items():
            try:
                return {
                    member_name: self._generate(
                        member_shape, f'{path}.{member_name}', context
                    )
                }
            except _Unsatisfiable as e:
                failures.append(f'{member_name} ({e.reason})')
        raise _Unsatisfiable(
            path,
            'no union member has a finite instance: '
            + '; '.join(failures),
        )

    def _generate_list(self, shape, path, context):
        min_length, max_length = self._check_range(shape, path)
        min_length = min_length or 0
        if shape.name in context.visiting:
            if min_length:
                raise _Unsatisfiable(
                    path,
                    f'recursive list shape "{shape.name}" requires at '
                    f'least {min_length} element(s)',
                )
            return []
        context = context.enter(shape, path)
        count = max(min_length, 1)
        elements = []
        try:
            for i in range(count):
                elements.append(
                    self._generate(shape.member, f'{path}[{i}]', context)
                )
        except _Unsatisfiable:
            if len(elements) >= min_length:
                return elements
            raise
        return elements

    def _generate_map(self, shape, path, context):
        min_length, max_length = self._check_range(shape, path)
        min_length = min_length or 0
        if shape.name in context.visiting:
            if min_length:
                raise _Unsatisfiable(
                    path,
                    f'recursive map shape "{shape.name}" requires at '
                    f'least {min_length} entries',
                )
            return {}
        context = context.enter(shape, path)
        count = max(min_length, 1)
        result = {}
        for i in range(count):
            try:
                key = self._generate_map_key(shape.key, path, i, result)
                result[key] = self._generate(
                    shape.value, f'{path}["{key}"]', context
                )
            except _Unsatisfiable:
                if len(result) >= min_length:
                    return result
                raise
        return result

    def _generate_map_key(self, key_shape, path, index, existing):
        key = self._generate_string(key_shape, path)
        if key not in existing:
            return key
        min_length, max_length = self._effective_min_max(key_shape)
        candidate = f'{key}{index}'
        if not self._length_satisfies(len(candidate), min_length, max_length):
            raise _Unsatisfiable(
                path,
                'unable to generate enough distinct map keys within '
                'the length constraints',
            )
        return candidate
