import pytest

from botocore.examplegenerator import ExampleGenerator
from botocore.loaders import Loader
from botocore.model import ServiceModel
from botocore.validate import ParamValidator

LOADER = Loader()
AVAILABLE_SERVICES = LOADER.list_available_services(type_name='service-2')


def _all_service_models():
    for service_name in AVAILABLE_SERVICES:
        service_description = LOADER.load_service_model(
            service_name, type_name='service-2'
        )
        yield service_name, ServiceModel(service_description, service_name)


def _all_operations():
    for service_name, service_model in _all_service_models():
        for operation_name in service_model.operation_names:
            yield service_name, service_model.operation_model(operation_name)


def _all_tagged_unions():
    for service_name, service_model in _all_service_models():
        for shape_name in service_model.shape_names:
            shape = service_model.shape_for(shape_name)
            if getattr(shape, 'is_tagged_union', False):
                yield service_name, shape


@pytest.mark.parametrize(
    "service_name, operation_model",
    list(_all_operations()),
)
def test_generated_example_passes_param_validation(
    service_name, operation_model
):
    generator = ExampleGenerator()
    params = generator.generate(operation_model)
    if operation_model.input_shape is not None:
        errors = ParamValidator().validate(
            params, operation_model.input_shape
        )
        assert not errors.has_errors(), (
            f'{service_name}.{operation_model.name}: '
            f'{errors.generate_report()}'
        )


@pytest.mark.parametrize(
    "service_name, union_shape",
    list(_all_tagged_unions()),
)
def test_every_union_branch_can_be_explicitly_selected(
    service_name, union_shape
):
    generator = ExampleGenerator()
    validator = ParamValidator()
    for member_name in union_shape.members:
        params = generator.generate_value(
            union_shape, union_selection={union_shape.name: member_name}
        )
        assert list(params) == [member_name]
        errors = validator.validate(params, union_shape)
        assert not errors.has_errors(), (
            f'{service_name}.{union_shape.name}.{member_name}: '
            f'{errors.generate_report()}'
        )
