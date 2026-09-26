from datetime import datetime, timezone

import pytest

import botocore.session
from botocore.examplegen import (
    ExampleGenerationError,
    generate_example,
)
from botocore.model import ServiceModel
from botocore.validate import ParamValidator


def make_operation_model(shapes, input_shape='Input'):
    operations = {'TestOperation': {'name': 'TestOperation'}}
    if input_shape is not None:
        operations['TestOperation']['input'] = {'shape': input_shape}
    service_model = ServiceModel(
        {
            'metadata': {
                'apiVersion': '2020-01-01',
                'endpointPrefix': 'test',
                'protocol': 'json',
                'serviceId': 'Test',
            },
            'operations': operations,
            'shapes': shapes,
        },
        service_name='test',
    )
    return service_model.operation_model('TestOperation')


def assert_valid(params, operation_model):
    report = ParamValidator().validate(params, operation_model.input_shape)
    assert not report.has_errors(), report.generate_report()


def generate(shapes, input_shape='Input', **kwargs):
    operation_model = make_operation_model(shapes, input_shape)
    params = generate_example(operation_model, **kwargs)
    assert_valid(params, operation_model)
    return params


class TestScalarGeneration:
    def test_required_included_optional_omitted(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {
                        'A': {'shape': 'StringType'},
                        'B': {'shape': 'StringType'},
                    },
                },
                'StringType': {'type': 'string'},
            }
        )
        assert params == {'A': ''}

    def test_enum_uses_first_value(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'EnumType'}},
                },
                'EnumType': {'type': 'string', 'enum': ['one', 'two']},
            }
        )
        assert params == {'A': 'one'}

    def test_string_min_length(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'BoundedString'}},
                },
                'BoundedString': {'type': 'string', 'min': 5, 'max': 10},
            }
        )
        assert params == {'A': 'xxxxx'}

    def test_integer_min(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'IntType'}},
                },
                'IntType': {'type': 'integer', 'min': 3, 'max': 10},
            }
        )
        assert params == {'A': 3}

    def test_integer_max_only(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'IntType'}},
                },
                'IntType': {'type': 'integer', 'max': -2},
            }
        )
        assert params == {'A': -2}

    def test_long_float_double_boolean(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['L', 'F', 'D', 'B'],
                    'members': {
                        'L': {'shape': 'LongType'},
                        'F': {'shape': 'FloatType'},
                        'D': {'shape': 'DoubleType'},
                        'B': {'shape': 'BoolType'},
                    },
                },
                'LongType': {'type': 'long', 'min': 2},
                'FloatType': {'type': 'float', 'min': 1},
                'DoubleType': {'type': 'double'},
                'BoolType': {'type': 'boolean'},
            }
        )
        assert params == {'L': 2, 'F': 1.0, 'D': 0.0, 'B': False}

    def test_host_label_implicit_min(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {
                        'A': {'shape': 'StringType', 'hostLabel': True}
                    },
                },
                'StringType': {'type': 'string'},
            }
        )
        assert params == {'A': 'x'}

    def test_timestamp_and_blob_default_seeds(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['T', 'B'],
                    'members': {
                        'T': {'shape': 'TimestampType'},
                        'B': {'shape': 'BlobType'},
                    },
                },
                'TimestampType': {'type': 'timestamp'},
                'BlobType': {'type': 'blob'},
            }
        )
        assert params['T'] == datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert params['B'] == b'blob'

    def test_timestamp_and_blob_custom_seeds(self):
        seed_time = datetime(2030, 5, 6, tzinfo=timezone.utc)
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['T', 'B'],
                    'members': {
                        'T': {'shape': 'TimestampType'},
                        'B': {'shape': 'BlobType'},
                    },
                },
                'TimestampType': {'type': 'timestamp'},
                'BlobType': {'type': 'blob'},
            },
            timestamp_seed=seed_time,
            blob_seed=b'\x01\x02',
        )
        assert params == {'T': seed_time, 'B': b'\x01\x02'}

    def test_blob_min_length(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['B'],
                    'members': {'B': {'shape': 'BlobType'}},
                },
                'BlobType': {'type': 'blob', 'min': 10},
            },
            blob_seed=b'ab',
        )
        assert params == {'B': b'ababababab'}


class TestContainerGeneration:
    def test_nested_required_structure(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {
                        'A': {'shape': 'Sub'},
                        'Optional': {'shape': 'Sub'},
                    },
                },
                'Sub': {
                    'type': 'structure',
                    'required': ['B'],
                    'members': {
                        'B': {'shape': 'StringType'},
                        'C': {'shape': 'StringType'},
                    },
                },
                'StringType': {'type': 'string'},
            }
        )
        assert params == {'A': {'B': ''}}

    def test_list_min_length(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'ListType'}},
                },
                'ListType': {
                    'type': 'list',
                    'min': 2,
                    'member': {'shape': 'StringType'},
                },
                'StringType': {'type': 'string', 'min': 1},
            }
        )
        assert params == {'A': ['x', 'x']}

    def test_list_default_empty(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'ListType'}},
                },
                'ListType': {
                    'type': 'list',
                    'member': {'shape': 'StringType'},
                },
                'StringType': {'type': 'string'},
            }
        )
        assert params == {'A': []}

    def test_map_default_empty(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'MapType'}},
                },
                'MapType': {
                    'type': 'map',
                    'key': {'shape': 'StringType'},
                    'value': {'shape': 'StringType'},
                },
                'StringType': {'type': 'string'},
            }
        )
        assert params == {'A': {}}

    def test_document_type(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'DocType'}},
                },
                'DocType': {'type': 'structure', 'document': True},
            }
        )
        assert params == {'A': {}}


class TestUnionGeneration:
    UNION_SHAPES = {
        'Input': {
            'type': 'structure',
            'required': ['U'],
            'members': {'U': {'shape': 'UnionType'}},
        },
        'UnionType': {
            'type': 'structure',
            'union': True,
            'members': {
                'S': {'shape': 'StringType'},
                'I': {'shape': 'IntType'},
            },
        },
        'StringType': {'type': 'string'},
        'IntType': {'type': 'integer', 'min': 7},
    }

    def test_default_picks_first_branch(self):
        assert generate(self.UNION_SHAPES) == {'U': {'S': ''}}

    def test_explicit_branch_by_shape_name(self):
        params = generate(
            self.UNION_SHAPES, union_choices={'UnionType': 'I'}
        )
        assert params == {'U': {'I': 7}}

    def test_explicit_branch_by_path(self):
        params = generate(
            self.UNION_SHAPES, union_choices={'Input.U': 'I'}
        )
        assert params == {'U': {'I': 7}}

    def test_unknown_branch_raises(self):
        operation_model = make_operation_model(self.UNION_SHAPES)
        with pytest.raises(ExampleGenerationError) as excinfo:
            generate_example(
                operation_model, union_choices={'UnionType': 'Nope'}
            )
        assert excinfo.value.path == 'Input.U'

    def test_union_branch_with_recursive_fallback(self):
        # The first branch is infinitely recursive, the second is finite;
        # the generator should fall back to the finite branch.
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['U'],
                    'members': {'U': {'shape': 'UnionType'}},
                },
                'UnionType': {
                    'type': 'structure',
                    'union': True,
                    'members': {
                        'Recurse': {'shape': 'Input'},
                        'S': {'shape': 'StringType'},
                    },
                },
                'StringType': {'type': 'string'},
            }
        )
        assert params == {'U': {'S': ''}}


class TestUngeneratableModels:
    def test_recursive_required_structure(self):
        operation_model = make_operation_model(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'Sub'}},
                },
                'Sub': {
                    'type': 'structure',
                    'required': ['B'],
                    'members': {'B': {'shape': 'Input'}},
                },
            }
        )
        with pytest.raises(ExampleGenerationError) as excinfo:
            generate_example(operation_model)
        assert excinfo.value.path == 'Input.A.B'
        assert 'Sub' in excinfo.value.reason

    def test_recursive_through_list_with_min(self):
        operation_model = make_operation_model(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'ListType'}},
                },
                'ListType': {
                    'type': 'list',
                    'min': 1,
                    'member': {'shape': 'Input'},
                },
            }
        )
        with pytest.raises(ExampleGenerationError) as excinfo:
            generate_example(operation_model)
        assert excinfo.value.path == 'Input.A[0]'

    def test_recursion_through_optional_member_is_fine(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['Name'],
                    'members': {
                        'Name': {'shape': 'StringType'},
                        'Parent': {'shape': 'Input'},
                    },
                },
                'StringType': {'type': 'string'},
            }
        )
        assert params == {'Name': ''}

    def test_recursion_through_empty_list_is_fine(self):
        params = generate(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['Children'],
                    'members': {'Children': {'shape': 'ListOfInput'}},
                },
                'ListOfInput': {'type': 'list', 'member': {'shape': 'Input'}},
            }
        )
        assert params == {'Children': []}

    def test_contradictory_integer_range(self):
        operation_model = make_operation_model(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'IntType'}},
                },
                'IntType': {'type': 'integer', 'min': 10, 'max': 5},
            }
        )
        with pytest.raises(ExampleGenerationError) as excinfo:
            generate_example(operation_model)
        assert excinfo.value.path == 'Input.A'
        assert 'min' in excinfo.value.reason

    def test_contradictory_string_length(self):
        operation_model = make_operation_model(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['A'],
                    'members': {'A': {'shape': 'StringType'}},
                },
                'StringType': {'type': 'string', 'min': 10, 'max': 5},
            }
        )
        with pytest.raises(ExampleGenerationError) as excinfo:
            generate_example(operation_model)
        assert excinfo.value.path == 'Input.A'

    def test_all_union_branches_ungeneratable(self):
        operation_model = make_operation_model(
            {
                'Input': {
                    'type': 'structure',
                    'required': ['U'],
                    'members': {'U': {'shape': 'UnionType'}},
                },
                'UnionType': {
                    'type': 'structure',
                    'union': True,
                    'members': {
                        'Recurse': {'shape': 'Input'},
                        'AlsoRecurse': {'shape': 'Input'},
                    },
                },
            }
        )
        with pytest.raises(ExampleGenerationError) as excinfo:
            generate_example(operation_model)
        assert excinfo.value.path == 'Input.U'


class TestRealServiceModels:
    def assert_generates_valid_params(self, service_name, operation_name):
        session = botocore.session.get_session()
        service_model = session.get_service_model(service_name)
        operation_model = service_model.operation_model(operation_name)
        params = generate_example(operation_model)
        assert_valid(params, operation_model)
        return params

    def test_dynamodb_put_item(self):
        params = self.assert_generates_valid_params('dynamodb', 'PutItem')
        assert set(params) == {'TableName', 'Item'}
        # Item is a map; the minimal example leaves it empty.
        assert params['Item'] == {}

    def test_real_tagged_union_default_branch(self):
        session = botocore.session.get_session()
        operation_model = session.get_service_model('redshift-serverless')
        operation_model = operation_model.operation_model(
            'CreateScheduledAction'
        )
        params = generate_example(operation_model)
        assert_valid(params, operation_model)
        assert set(params['schedule']) == {'at'}

    def test_real_tagged_union_branch_selection(self):
        session = botocore.session.get_session()
        operation_model = session.get_service_model('redshift-serverless')
        operation_model = operation_model.operation_model(
            'CreateScheduledAction'
        )
        params = generate_example(
            operation_model, union_choices={'Schedule': 'cron'}
        )
        assert_valid(params, operation_model)
        assert set(params['schedule']) == {'cron'}

    @pytest.mark.parametrize(
        'service_name,operation_name',
        [
            ('dynamodb', 'CreateTable'),
            ('dynamodb', 'BatchWriteItem'),
            ('ec2', 'RunInstances'),
            ('ec2', 'DescribeInstances'),
            ('s3', 'CreateBucket'),
            ('s3', 'PutObject'),
            ('sqs', 'SendMessage'),
            ('lambda', 'CreateFunction'),
            ('vpc-lattice', 'CreateService'),
            ('mgn', 'CreateApplication'),
        ],
    )
    def test_real_operation(self, service_name, operation_name):
        self.assert_generates_valid_params(service_name, operation_name)

    @pytest.mark.parametrize(
        'service_name', ['dynamodb', 'sqs', 'sns', 'vpc-lattice']
    )
    def test_all_operations_of_service(self, service_name):
        session = botocore.session.get_session()
        service_model = session.get_service_model(service_name)
        for operation_name in service_model.operation_names:
            operation_model = service_model.operation_model(operation_name)
            params = generate_example(operation_model)
            assert_valid(params, operation_model)

    def test_operation_without_input(self):
        operation_model = make_operation_model({}, input_shape=None)
        assert generate_example(operation_model) == {}
