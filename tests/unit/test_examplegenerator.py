from datetime import datetime, timezone

import pytest

from botocore.examplegenerator import (
    ExampleGenerationError,
    ExampleGenerator,
    generate_example,
)
from botocore.model import ServiceModel
from botocore.validate import ParamValidator


def make_operation_model(shapes, input_shape='Input'):
    service_model = ServiceModel(
        {
            'metadata': {'apiVersion': '2020-01-01'},
            'operations': {
                'TestOperation': {
                    'name': 'TestOperation',
                    'input': {'shape': input_shape},
                },
                'NoInputOperation': {'name': 'NoInputOperation'},
            },
            'shapes': shapes,
        },
        service_name='testservice',
    )
    return service_model


def generate(shapes, input_shape='Input', **kwargs):
    service_model = make_operation_model(shapes, input_shape)
    operation_model = service_model.operation_model('TestOperation')
    return ExampleGenerator().generate(operation_model, **kwargs)


def assert_passes_validation(params, shapes, input_shape='Input'):
    service_model = make_operation_model(shapes, input_shape)
    operation_model = service_model.operation_model('TestOperation')
    errors = ParamValidator().validate(params, operation_model.input_shape)
    assert not errors.has_errors(), errors.generate_report()


def generate_and_validate(shapes, input_shape='Input', **kwargs):
    params = generate(shapes, input_shape, **kwargs)
    assert_passes_validation(params, shapes, input_shape)
    return params


def test_no_input_shape_returns_empty_dict():
    service_model = make_operation_model({})
    operation_model = service_model.operation_model('NoInputOperation')
    assert ExampleGenerator().generate(operation_model) == {}


def test_only_required_members_generated_by_default():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Required'],
            'members': {
                'Required': {'shape': 'StringType'},
                'Optional': {'shape': 'StringType'},
            },
        },
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes)
    assert params == {'Required': ''}


def test_optional_members_included_on_request():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Required'],
            'members': {
                'Required': {'shape': 'StringType'},
                'Optional': {'shape': 'StringType'},
            },
        },
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes, include_optional=True)
    assert params == {'Required': '', 'Optional': ''}


def test_string_min_length_is_respected():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Name'],
            'members': {'Name': {'shape': 'BoundedString'}},
        },
        'BoundedString': {'type': 'string', 'min': 5, 'max': 10},
    }
    params = generate_and_validate(shapes)
    assert params == {'Name': 'a' * 5}


def test_enum_uses_valid_enum_value():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['State'],
            'members': {'State': {'shape': 'StateEnum'}},
        },
        'StateEnum': {'type': 'string', 'enum': ['on', 'off']},
    }
    params = generate_and_validate(shapes)
    assert params == {'State': 'on'}


def test_enum_value_violating_length_is_skipped():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['State'],
            'members': {'State': {'shape': 'StateEnum'}},
        },
        'StateEnum': {'type': 'string', 'enum': ['a', 'valid'], 'min': 3},
    }
    params = generate_and_validate(shapes)
    assert params == {'State': 'valid'}


def test_integer_min_max_is_respected():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Count', 'Limit'],
            'members': {
                'Count': {'shape': 'BoundedInt'},
                'Limit': {'shape': 'NegativeInt'},
            },
        },
        'BoundedInt': {'type': 'integer', 'min': 3, 'max': 10},
        'NegativeInt': {'type': 'long', 'max': -2},
    }
    params = generate_and_validate(shapes)
    assert params == {'Count': 3, 'Limit': -2}


def test_scalar_types():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['F', 'D', 'B'],
            'members': {
                'F': {'shape': 'FloatType'},
                'D': {'shape': 'DoubleType'},
                'B': {'shape': 'BoolType'},
            },
        },
        'FloatType': {'type': 'float', 'min': 1},
        'DoubleType': {'type': 'double'},
        'BoolType': {'type': 'boolean'},
    }
    params = generate_and_validate(shapes)
    assert params == {'F': 1.0, 'D': 0.0, 'B': False}


def test_nested_required_structure():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Config'],
            'members': {'Config': {'shape': 'Config'}},
        },
        'Config': {
            'type': 'structure',
            'required': ['Name'],
            'members': {
                'Name': {'shape': 'StringType'},
                'Extra': {'shape': 'StringType'},
            },
        },
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes)
    assert params == {'Config': {'Name': ''}}


def test_list_min_length_is_respected():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Items', 'Tags'],
            'members': {
                'Items': {'shape': 'BoundedList'},
                'Tags': {'shape': 'PlainList'},
            },
        },
        'BoundedList': {
            'type': 'list',
            'member': {'shape': 'StringType'},
            'min': 2,
        },
        'PlainList': {'type': 'list', 'member': {'shape': 'StringType'}},
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes)
    assert params == {'Items': ['', ''], 'Tags': ['']}


def test_map_generates_entry():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Attributes'],
            'members': {'Attributes': {'shape': 'AttributeMap'}},
        },
        'AttributeMap': {
            'type': 'map',
            'key': {'shape': 'KeyType'},
            'value': {'shape': 'StringType'},
        },
        'KeyType': {'type': 'string', 'min': 2},
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes)
    assert params == {'Attributes': {'aa': ''}}


def test_tagged_union_defaults_to_single_member():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Choice'],
            'members': {'Choice': {'shape': 'Choice'}},
        },
        'Choice': {
            'type': 'structure',
            'union': True,
            'members': {
                'First': {'shape': 'StringType'},
                'Second': {'shape': 'IntType'},
            },
        },
        'StringType': {'type': 'string'},
        'IntType': {'type': 'integer'},
    }
    params = generate_and_validate(shapes)
    assert params == {'Choice': {'First': ''}}


def test_tagged_union_branch_can_be_explicitly_selected():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Choice'],
            'members': {'Choice': {'shape': 'Choice'}},
        },
        'Choice': {
            'type': 'structure',
            'union': True,
            'members': {
                'First': {'shape': 'StringType'},
                'Second': {'shape': 'IntType'},
            },
        },
        'StringType': {'type': 'string'},
        'IntType': {'type': 'integer'},
    }
    params = generate_and_validate(
        shapes, union_selection={'Choice': 'Second'}
    )
    assert params == {'Choice': {'Second': 0}}


def test_tagged_union_invalid_selection_raises():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Choice'],
            'members': {'Choice': {'shape': 'Choice'}},
        },
        'Choice': {
            'type': 'structure',
            'union': True,
            'members': {'First': {'shape': 'StringType'}},
        },
        'StringType': {'type': 'string'},
    }
    with pytest.raises(ExampleGenerationError) as excinfo:
        generate(shapes, union_selection={'Choice': 'Nope'})
    assert 'Nope' in str(excinfo.value)
    assert 'First' in str(excinfo.value)


def test_recursive_required_structure_raises_with_shape_path():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Node'],
            'members': {'Node': {'shape': 'Node'}},
        },
        'Node': {
            'type': 'structure',
            'required': ['Next'],
            'members': {
                'Next': {'shape': 'Node'},
                'Label': {'shape': 'StringType'},
            },
        },
        'StringType': {'type': 'string'},
    }
    with pytest.raises(ExampleGenerationError) as excinfo:
        generate(shapes)
    error = excinfo.value
    assert error.shape_path == 'Input.Node.Next'
    assert 'Node -> Node' in str(error)


def test_mutually_recursive_required_structure_raises():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['A'],
            'members': {'A': {'shape': 'A'}},
        },
        'A': {
            'type': 'structure',
            'required': ['B'],
            'members': {'B': {'shape': 'B'}},
        },
        'B': {
            'type': 'structure',
            'required': ['A'],
            'members': {'A': {'shape': 'A'}},
        },
    }
    with pytest.raises(ExampleGenerationError) as excinfo:
        generate(shapes)
    assert 'A -> B -> A' in str(excinfo.value)


def test_recursive_optional_member_is_simply_omitted():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Node'],
            'members': {'Node': {'shape': 'Node'}},
        },
        'Node': {
            'type': 'structure',
            'members': {
                'Next': {'shape': 'Node'},
                'Label': {'shape': 'StringType'},
            },
        },
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes, include_optional=True)
    assert params == {'Node': {'Label': ''}}


def test_union_skips_recursive_branch():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Choice'],
            'members': {'Choice': {'shape': 'Choice'}},
        },
        'Choice': {
            'type': 'structure',
            'union': True,
            'members': {
                'Loop': {'shape': 'Loop'},
                'Value': {'shape': 'StringType'},
            },
        },
        'Loop': {
            'type': 'structure',
            'required': ['Choice'],
            'members': {'Choice': {'shape': 'Choice'}},
        },
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes)
    assert params == {'Choice': {'Value': ''}}


def test_recursive_union_branch_selection_stays_finite():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Filter'],
            'members': {'Filter': {'shape': 'Filter'}},
        },
        'Filter': {
            'type': 'structure',
            'union': True,
            'members': {
                'All': {'shape': 'FilterList'},
                'Name': {'shape': 'StringType'},
            },
        },
        'FilterList': {
            'type': 'list',
            'member': {'shape': 'Filter'},
            'min': 1,
        },
        'StringType': {'type': 'string'},
    }
    # The explicit selection applies to the first occurrence of the
    # union.  Nested occurrences use the default branch selection so
    # that the generated example remains finite.
    params = generate_and_validate(
        shapes, union_selection={'Filter': 'All'}
    )
    assert params == {'Filter': {'All': [{'Name': ''}]}}


def test_contradictory_numeric_range_raises():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Count'],
            'members': {'Count': {'shape': 'Contradiction'}},
        },
        'Contradiction': {'type': 'integer', 'min': 10, 'max': 5},
    }
    with pytest.raises(ExampleGenerationError) as excinfo:
        generate(shapes)
    assert excinfo.value.shape_path == 'Input.Count'
    assert 'minimum 10 exceeds maximum 5' in str(excinfo.value)


def test_contradictory_string_length_raises():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Name'],
            'members': {'Name': {'shape': 'Contradiction'}},
        },
        'Contradiction': {'type': 'string', 'min': 10, 'max': 5},
    }
    with pytest.raises(ExampleGenerationError) as excinfo:
        generate(shapes)
    assert excinfo.value.shape_path == 'Input.Name'


def test_enum_with_no_value_satisfying_length_raises():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['State'],
            'members': {'State': {'shape': 'Contradiction'}},
        },
        'Contradiction': {'type': 'string', 'enum': ['a', 'b'], 'min': 5},
    }
    with pytest.raises(ExampleGenerationError) as excinfo:
        generate(shapes)
    assert excinfo.value.shape_path == 'Input.State'
    assert 'no enum value' in str(excinfo.value)


def test_timestamp_is_seeded_and_deterministic():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['When'],
            'members': {'When': {'shape': 'TimestampType'}},
        },
        'TimestampType': {'type': 'timestamp'},
    }
    first = generate(shapes)
    second = generate(shapes)
    assert first == second
    assert first['When'] == datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert_passes_validation(first, shapes)


def test_timestamp_seed_and_override_are_caller_controllable():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['When'],
            'members': {'When': {'shape': 'TimestampType'}},
        },
        'TimestampType': {'type': 'timestamp'},
    }
    service_model = make_operation_model(shapes)
    operation_model = service_model.operation_model('TestOperation')
    seeded = ExampleGenerator(seed=60).generate(operation_model)
    assert seeded['When'] == datetime(2020, 1, 1, 0, 1, tzinfo=timezone.utc)
    explicit = datetime(2030, 5, 5, tzinfo=timezone.utc)
    overridden = ExampleGenerator(timestamp=explicit).generate(operation_model)
    assert overridden['When'] == explicit


def test_blob_is_seeded_and_respects_min_length():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Data'],
            'members': {'Data': {'shape': 'BlobType'}},
        },
        'BlobType': {'type': 'blob', 'min': 40},
    }
    service_model = make_operation_model(shapes)
    operation_model = service_model.operation_model('TestOperation')
    first = ExampleGenerator(seed=1).generate(operation_model)
    second = ExampleGenerator(seed=1).generate(operation_model)
    third = ExampleGenerator(seed=2).generate(operation_model)
    assert first == second
    assert first != third
    assert len(first['Data']) == 40
    assert_passes_validation(first, shapes)
    overridden = ExampleGenerator(blob=b'explicit').generate(operation_model)
    assert overridden['Data'] == b'explicit'


def test_document_type_generates_valid_value():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Doc'],
            'members': {'Doc': {'shape': 'DocType'}},
        },
        'DocType': {'type': 'structure', 'document': True},
    }
    params = generate_and_validate(shapes)
    assert params == {'Doc': {}}


def test_host_label_member_has_implicit_min():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Bucket'],
            'members': {
                'Bucket': {'shape': 'StringType', 'hostLabel': True}
            },
        },
        'StringType': {'type': 'string'},
    }
    params = generate_and_validate(shapes)
    assert params == {'Bucket': 'a'}


def test_generate_example_convenience_function():
    shapes = {
        'Input': {
            'type': 'structure',
            'required': ['Name'],
            'members': {'Name': {'shape': 'StringType'}},
        },
        'StringType': {'type': 'string'},
    }
    service_model = make_operation_model(shapes)
    operation_model = service_model.operation_model('TestOperation')
    assert generate_example(operation_model) == {'Name': ''}
