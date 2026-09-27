# Copyright 2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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

"""
NOTE: All classes and functions in this module are considered private and are
subject to abrupt breaking changes. Please do not use them directly.

To view the raw JSON that the objects in this module represent, please
go to any `endpoint-rule-set.json` file in /botocore/data/<service>/<api version>/
or you can look at the test files in /tests/unit/data/endpoints/valid-rules/
"""

import logging
import re
from copy import deepcopy
from enum import Enum
from string import Formatter
from typing import NamedTuple

from botocore import xform_name
from botocore.compat import IPV4_RE, quote, urlparse
from botocore.exceptions import EndpointResolutionError
from botocore.utils import (
    ArnParser,
    InvalidArnException,
    is_valid_ipv4_endpoint_url,
    is_valid_ipv6_endpoint_url,
    lru_cache_weakref,
    normalize_url_path,
    percent_encode,
)

logger = logging.getLogger(__name__)

TEMPLATE_STRING_RE = re.compile(r"\{[a-zA-Z#]+\}")
GET_ATTR_RE = re.compile(r"(\w*)\[(\d+)\]")
VALID_HOST_LABEL_RE = re.compile(
    r"^(?!-)[a-zA-Z\d-]{1,63}(?<!-)$",
)
CACHE_SIZE = 100
# S3 endpoint ruleset parameters that are defined but not currently referenced.
# They are excluded from the cache key to avoid cache thrashing when
# accessing multiple objects in the same bucket.
S3_UNREFERENCED_PARAMS = {'Key', 'Prefix', 'CopySource'}
ARN_PARSER = ArnParser()
STRING_FORMATTER = Formatter()
# Sentinel returned when a fully resolvable condition cannot be evaluated at
# specialization time and must be kept in the residual ruleset.
_CONDITION_UNFOLDABLE = object()


class RuleSetStandardLibrary:
    """Rule actions to be performed by the EndpointProvider."""

    def __init__(self, partitions_data):
        self.partitions_data = partitions_data

    def is_func(self, argument):
        """Determine if an object is a function object.

        :type argument: Any
        :rtype: bool
        """
        return isinstance(argument, dict) and "fn" in argument

    def is_ref(self, argument):
        """Determine if an object is a reference object.

        :type argument: Any
        :rtype: bool
        """
        return isinstance(argument, dict) and "ref" in argument

    def is_template(self, argument):
        """Determine if an object contains a template string.

        :type argument: Any
        :rtpe: bool
        """
        return (
            isinstance(argument, str)
            and TEMPLATE_STRING_RE.search(argument) is not None
        )

    def resolve_template_string(self, value, scope_vars):
        """Resolve and inject values into a template string.

        :type value: str
        :type scope_vars: dict
        :rtype: str
        """
        result = ""
        for literal, reference, _, _ in STRING_FORMATTER.parse(value):
            if reference is not None:
                template_value = scope_vars
                template_params = reference.split("#")
                for param in template_params:
                    template_value = template_value[param]
                result += f"{literal}{template_value}"
            else:
                result += literal
        return result

    def resolve_value(self, value, scope_vars):
        """Return evaluated value based on type.

        :type value: Any
        :type scope_vars: dict
        :rtype: Any
        """
        if self.is_func(value):
            return self.call_function(value, scope_vars)
        elif self.is_ref(value):
            return scope_vars.get(value["ref"])
        elif self.is_template(value):
            return self.resolve_template_string(value, scope_vars)

        return value

    def convert_func_name(self, value):
        """Normalize function names.

        :type value: str
        :rtype: str
        """
        normalized_name = f"{xform_name(value)}"
        if normalized_name == "not":
            normalized_name = f"_{normalized_name}"
        return normalized_name.replace(".", "_")

    def call_function(self, func_signature, scope_vars):
        """Call the function with the resolved arguments and assign to `scope_vars`
        when applicable.

        :type func_signature: dict
        :type scope_vars: dict
        :rtype: Any
        """
        func_args = [
            self.resolve_value(arg, scope_vars)
            for arg in func_signature["argv"]
        ]
        func_name = self.convert_func_name(func_signature["fn"])
        func = getattr(self, func_name)
        result = func(*func_args)
        if "assign" in func_signature:
            assign = func_signature["assign"]
            if assign in scope_vars:
                raise EndpointResolutionError(
                    msg=f"Assignment {assign} already exists in "
                    "scoped variables and cannot be overwritten"
                )
            scope_vars[assign] = result
        return result

    def is_set(self, value):
        """Evaluates whether a value is set.

        :type value: Any
        :rytpe: bool
        """
        return value is not None

    def get_attr(self, value, path):
        """Find an attribute within a value given a path string. The path can contain
        the name of the attribute and an index in brackets. A period separating attribute
        names indicates the one to the right is nested. The index will always occur at
        the end of the path.

        :type value: dict or tuple
        :type path: str
        :rtype: Any
        """
        for part in path.split("."):
            match = GET_ATTR_RE.search(part)
            if match is not None:
                name, index = match.groups()
                index = int(index)
                if name:
                    value = value.get(name)
                if value is None or index >= len(value):
                    return None
                return value[index]
            else:
                value = value[part]
        return value

    def format_partition_output(self, partition):
        output = partition["outputs"]
        output["name"] = partition["id"]
        return output

    def is_partition_match(self, region, partition):
        matches_regex = re.match(partition["regionRegex"], region) is not None
        return region in partition["regions"] or matches_regex

    def aws_partition(self, value):
        """Match a region string to an AWS partition.

        :type value: str
        :rtype: dict
        """
        partitions = self.partitions_data['partitions']

        if value is not None:
            for partition in partitions:
                if self.is_partition_match(value, partition):
                    return self.format_partition_output(partition)

        # return the default partition if no matches were found
        aws_partition = partitions[0]
        return self.format_partition_output(aws_partition)

    def aws_parse_arn(self, value):
        """Parse and validate string for ARN components.

        :type value: str
        :rtype: dict
        """
        if value is None or not value.startswith("arn:"):
            return None

        try:
            arn_dict = ARN_PARSER.parse_arn(value)
        except InvalidArnException:
            return None

        # partition, resource, and service are required
        if not all(
            (arn_dict["partition"], arn_dict["service"], arn_dict["resource"])
        ):
            return None

        arn_dict["accountId"] = arn_dict.pop("account")

        resource = arn_dict.pop("resource")
        arn_dict["resourceId"] = resource.replace(":", "/").split("/")

        return arn_dict

    def is_valid_host_label(self, value, allow_subdomains):
        """Evaluates whether a value is a valid host label per
        RFC 1123. If allow_subdomains is True, split on `.` and validate
        each component separately.

        :type value: str
        :type allow_subdomains: bool
        :rtype: bool
        """
        if value is None or allow_subdomains is False and value.count(".") > 0:
            return False

        if allow_subdomains is True:
            return all(
                self.is_valid_host_label(label, False)
                for label in value.split(".")
            )

        return VALID_HOST_LABEL_RE.match(value) is not None

    def string_equals(self, value1, value2):
        """Evaluates two string values for equality.

        :type value1: str
        :type value2: str
        :rtype: bool
        """
        if not all(isinstance(val, str) for val in (value1, value2)):
            msg = f"Both values must be strings, not {type(value1)} and {type(value2)}."
            raise EndpointResolutionError(msg=msg)
        return value1 == value2

    def uri_encode(self, value):
        """Perform percent-encoding on an input string.

        :type value: str
        :rytpe: str
        """
        if value is None:
            return None

        return percent_encode(value)

    def parse_url(self, value):
        """Parse a URL string into components.

        :type value: str
        :rtype: dict
        """
        if value is None:
            return None

        url_components = urlparse(value)
        try:
            # url_parse may assign non-integer values to
            # `port` and will fail when accessed.
            url_components.port
        except ValueError:
            return None

        scheme = url_components.scheme
        query = url_components.query
        # URLs with queries are not supported
        if scheme not in ("https", "http") or len(query) > 0:
            return None

        path = url_components.path
        normalized_path = quote(normalize_url_path(path))
        if not normalized_path.endswith("/"):
            normalized_path = f"{normalized_path}/"

        return {
            "scheme": scheme,
            "authority": url_components.netloc,
            "path": path,
            "normalizedPath": normalized_path,
            "isIp": is_valid_ipv4_endpoint_url(value)
            or is_valid_ipv6_endpoint_url(value),
        }

    def boolean_equals(self, value1, value2):
        """Evaluates two boolean values for equality.

        :type value1: bool
        :type value2: bool
        :rtype: bool
        """
        if not all(isinstance(val, bool) for val in (value1, value2)):
            msg = f"Both arguments must be bools, not {type(value1)} and {type(value2)}."
            raise EndpointResolutionError(msg=msg)
        return value1 is value2

    def is_ascii(self, value):
        """Evaluates if a string only contains ASCII characters.

        :type value: str
        :rtype: bool
        """
        try:
            value.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    def substring(self, value, start, stop, reverse):
        """Computes a substring given the start index and end index. If `reverse` is
        True, slice the string from the end instead.

        :type value: str
        :type start: int
        :type end: int
        :type reverse: bool
        :rtype: str
        """
        if not isinstance(value, str):
            msg = f"Input must be a string, not {type(value)}."
            raise EndpointResolutionError(msg=msg)
        if start >= stop or len(value) < stop or not self.is_ascii(value):
            return None

        if reverse is True:
            r_start = len(value) - stop
            r_stop = len(value) - start
            return value[r_start:r_stop]

        return value[start:stop]

    def _not(self, value):
        """A function implementation of the logical operator `not`.

        :type value: Any
        :rtype: bool
        """
        return not value

    def aws_is_virtual_hostable_s3_bucket(self, value, allow_subdomains):
        """Evaluates whether a value is a valid bucket name for virtual host
        style bucket URLs. To pass, the value must meet the following criteria:
        1. is_valid_host_label(value) is True
        2. length between 3 and 63 characters (inclusive)
        3. does not contain uppercase characters
        4. is not formatted as an IP address

        If allow_subdomains is True, split on `.` and validate
        each component separately.

        :type value: str
        :type allow_subdomains: bool
        :rtype: bool
        """
        if (
            value is None
            or len(value) < 3
            or value.lower() != value
            or IPV4_RE.match(value) is not None
        ):
            return False

        return self.is_valid_host_label(
            value, allow_subdomains=allow_subdomains
        )


# maintains backwards compatibility as `Library` was misspelled
# in earlier versions
RuleSetStandardLibary = RuleSetStandardLibrary


class BaseRule:
    """Base interface for individual endpoint rules."""

    def __init__(self, conditions, documentation=None, bound_vars=None):
        self.conditions = conditions
        self.documentation = documentation
        # Pre-computed scoped variables, used by specialized (partially
        # evaluated) rulesets. They behave as if conditions that have
        # already been folded away had assigned them at runtime.
        self.bound_vars = bound_vars or {}

    def evaluate(self, scope_vars, rule_lib):
        raise NotImplementedError()

    def apply_bound_vars(self, scope_vars):
        """Inject pre-computed scoped variables into the evaluation scope.

        :type scope_vars: dict
        """
        if self.bound_vars:
            scope_vars.update(self.bound_vars)

    def evaluate_conditions(self, scope_vars, rule_lib):
        """Determine if all conditions in a rule are met.

        :type scope_vars: dict
        :type rule_lib: RuleSetStandardLibrary
        :rtype: bool
        """
        for func_signature in self.conditions:
            result = rule_lib.call_function(func_signature, scope_vars)
            if result is False or result is None:
                return False
        return True


class RuleSetEndpoint(NamedTuple):
    """A resolved endpoint object returned by a rule."""

    url: str
    properties: dict
    headers: dict


class EndpointRule(BaseRule):
    def __init__(self, endpoint, **kwargs):
        super().__init__(**kwargs)
        self.endpoint = endpoint

    def evaluate(self, scope_vars, rule_lib):
        """Determine if conditions are met to provide a valid endpoint.

        :type scope_vars: dict
        :rtype: RuleSetEndpoint
        """
        self.apply_bound_vars(scope_vars)
        if self.evaluate_conditions(scope_vars, rule_lib):
            url = rule_lib.resolve_value(self.endpoint["url"], scope_vars)
            properties = self.resolve_properties(
                self.endpoint.get("properties", {}),
                scope_vars,
                rule_lib,
            )
            headers = self.resolve_headers(scope_vars, rule_lib)
            return RuleSetEndpoint(
                url=url, properties=properties, headers=headers
            )

        return None

    def resolve_properties(self, properties, scope_vars, rule_lib):
        """Traverse `properties` attribute, resolving any template strings.

        :type properties: dict/list/str
        :type scope_vars: dict
        :type rule_lib: RuleSetStandardLibrary
        :rtype: dict
        """
        if isinstance(properties, list):
            return [
                self.resolve_properties(prop, scope_vars, rule_lib)
                for prop in properties
            ]
        elif isinstance(properties, dict):
            return {
                key: self.resolve_properties(value, scope_vars, rule_lib)
                for key, value in properties.items()
            }
        elif rule_lib.is_template(properties):
            return rule_lib.resolve_template_string(properties, scope_vars)

        return properties

    def resolve_headers(self, scope_vars, rule_lib):
        """Iterate through headers attribute resolving all values.

        :type scope_vars: dict
        :type rule_lib: RuleSetStandardLibrary
        :rtype: dict
        """
        resolved_headers = {}
        headers = self.endpoint.get("headers", {})

        for header, values in headers.items():
            resolved_headers[header] = [
                rule_lib.resolve_value(item, scope_vars) for item in values
            ]
        return resolved_headers


class ErrorRule(BaseRule):
    def __init__(self, error, **kwargs):
        super().__init__(**kwargs)
        self.error = error

    def evaluate(self, scope_vars, rule_lib):
        """If an error rule's conditions are met, raise an error rule.

        :type scope_vars: dict
        :type rule_lib: RuleSetStandardLibrary
        :rtype: EndpointResolutionError
        """
        self.apply_bound_vars(scope_vars)
        if self.evaluate_conditions(scope_vars, rule_lib):
            error = rule_lib.resolve_value(self.error, scope_vars)
            raise EndpointResolutionError(msg=error)
        return None


class TreeRule(BaseRule):
    """A tree rule is non-terminal meaning it will never be returned to a provider.
    Additionally this means it has no attributes that need to be resolved.
    """

    def __init__(self, rules, **kwargs):
        super().__init__(**kwargs)
        self.rules = [
            rule if isinstance(rule, BaseRule) else RuleCreator.create(**rule)
            for rule in rules
        ]

    def evaluate(self, scope_vars, rule_lib):
        """If a tree rule's conditions are met, iterate its sub-rules
        and return first result found.

        :type scope_vars: dict
        :type rule_lib: RuleSetStandardLibrary
        :rtype: RuleSetEndpoint/EndpointResolutionError
        """
        self.apply_bound_vars(scope_vars)
        if self.evaluate_conditions(scope_vars, rule_lib):
            for rule in self.rules:
                # don't share scope_vars between rules
                rule_result = rule.evaluate(scope_vars.copy(), rule_lib)
                if rule_result:
                    return rule_result
        return None


class RuleCreator:
    endpoint = EndpointRule
    error = ErrorRule
    tree = TreeRule

    @classmethod
    def create(cls, **kwargs):
        """Create a rule instance from metadata.

        :rtype: TreeRule/EndpointRule/ErrorRule
        """
        rule_type = kwargs.pop("type")
        try:
            rule_class = getattr(cls, rule_type)
        except AttributeError:
            raise EndpointResolutionError(
                msg=f"Unknown rule type: {rule_type}. A rule must "
                "be of type tree, endpoint or error."
            )
        else:
            return rule_class(**kwargs)


class ParameterType(Enum):
    """Translation from `type` attribute to native Python type."""

    string = str
    boolean = bool
    stringarray = tuple


class ParameterDefinition:
    """The spec of an individual parameter defined in a RuleSet."""

    def __init__(
        self,
        name,
        parameter_type,
        documentation=None,
        builtIn=None,
        default=None,
        required=None,
        deprecated=None,
    ):
        self.name = name
        try:
            self.parameter_type = getattr(
                ParameterType, parameter_type.lower()
            ).value
        except AttributeError:
            raise EndpointResolutionError(
                msg=f"Unknown parameter type: {parameter_type}. "
                "A parameter must be of type string, boolean, or stringarray."
            )
        self.documentation = documentation
        self.builtin = builtIn
        self.default = default
        self.required = required
        self.deprecated = deprecated

    def validate_input(self, value):
        """Perform base validation on parameter input.

        :type value: Any
        :raises: EndpointParametersError
        """

        if not isinstance(value, self.parameter_type):
            raise EndpointResolutionError(
                msg=f"Value ({self.name}) is the wrong "
                f"type. Must be {self.parameter_type}."
            )
        if self.deprecated is not None:
            depr_str = f"{self.name} has been deprecated."
            msg = self.deprecated.get("message")
            since = self.deprecated.get("since")
            if msg:
                depr_str += f"\n{msg}"
            if since:
                depr_str += f"\nDeprecated since {since}."
            logger.info(depr_str)

        return None

    def process_input(self, value):
        """Process input against spec, applying default if value is None."""
        if value is None:
            if self.default is not None:
                return self.default
            if self.required:
                raise EndpointResolutionError(
                    msg=f"Cannot find value for required parameter {self.name}"
                )
            # in all other cases, the parameter will keep the value None
        else:
            self.validate_input(value)
        return value


class RuleSet:
    """Collection of rules to derive a routable service endpoint."""

    def __init__(
        self,
        version,
        parameters,
        rules,
        partitions,
        documentation=None,
        bound_vars=None,
    ):
        self.version = version
        self.parameters = self._ingest_parameter_spec(parameters)
        self.rules = [RuleCreator.create(**rule) for rule in rules]
        self.rule_lib = RuleSetStandardLibrary(partitions)
        self.documentation = documentation
        # Pre-computed scoped variables, used by specialized (partially
        # evaluated) rulesets. Empty for rulesets built from raw JSON.
        self.bound_vars = bound_vars or {}

    @classmethod
    def _from_components(
        cls,
        version,
        parameters,
        rules,
        rule_lib,
        documentation,
        bound_vars,
    ):
        """Build a RuleSet from pre-ingested components."""
        ruleset = cls.__new__(cls)
        ruleset.version = version
        ruleset.parameters = parameters
        ruleset.rules = rules
        ruleset.rule_lib = rule_lib
        ruleset.documentation = documentation
        ruleset.bound_vars = bound_vars
        return ruleset

    def _ingest_parameter_spec(self, parameters):
        return {
            name: ParameterDefinition(
                name,
                spec["type"],
                spec.get("documentation"),
                spec.get("builtIn"),
                spec.get("default"),
                spec.get("required"),
                spec.get("deprecated"),
            )
            for name, spec in parameters.items()
        }

    def process_input_parameters(self, input_params):
        """Process each input parameter against its spec.

        :type input_params: dict
        """
        for name, spec in self.parameters.items():
            value = spec.process_input(input_params.get(name))
            if value is not None:
                input_params[name] = value
        return None

    def evaluate(self, input_parameters):
        """Evaluate input parameters against rules returning first match.

        :type input_parameters: dict
        """
        self.process_input_parameters(input_parameters)
        if self.bound_vars:
            input_parameters.update(self.bound_vars)
        for rule in self.rules:
            evaluation = rule.evaluate(input_parameters.copy(), self.rule_lib)
            if evaluation is not None:
                return evaluation
        return None

    def specialize(self, fixed_parameters):
        """Create a residual ruleset with the given parameters pre-evaluated.

        Clients typically fix a number of parameters (for example ``Region``,
        ``UseFIPS`` and ``UseDualStack``) for their lifetime while still
        paying the cost of evaluating the full ruleset on every request.
        Specializing a ruleset evaluates, once, every condition whose inputs
        are all known and returns a new, reusable ruleset in which:

        * conditions that are fully determined by the fixed parameters are
          folded away and are never re-evaluated,
        * rules on branches that can never match are removed,
        * scoped variables assigned by folded conditions are pre-computed
          and injected when the residual rule is evaluated,
        * rule order (priority), template strings, error branches and
          endpoint properties such as ``authSchemes`` and ``headers`` are
          preserved.

        A condition is only folded when every value it depends on is known.
        Conditions that depend on remaining (unknown) parameters are kept
        as-is; unknown values are never treated as ``None`` or ``False``.

        The residual ruleset is a new object; this ruleset is not modified.

        :type fixed_parameters: dict
        :param fixed_parameters: Parameter names mapped to their fixed
            values. Values are validated and processed against the
            parameter spec, so defaults are applied and missing required
            parameters raise an error.
        :rtype: RuleSet
        """
        known_values = {}
        bound_vars = {}
        for name, value in fixed_parameters.items():
            if isinstance(value, list):
                # mirror the list-to-tuple conversion performed when
                # resolving endpoints with stringarray parameters
                value = tuple(value)
            spec = self.parameters.get(name)
            if spec is not None:
                value = spec.process_input(value)
            # A parameter fixed to None is known to be unset. It is used to
            # fold conditions but, as at runtime, is not added to the scope
            # of the residual ruleset.
            known_values[name] = value
            if value is not None:
                bound_vars[name] = value
        specializer = _RuleSpecializer(self.rule_lib)
        residual_rules = specializer.specialize_rules(self.rules, known_values)
        residual_parameters = {
            name: spec
            for name, spec in self.parameters.items()
            if name not in fixed_parameters
        }
        return type(self)._from_components(
            version=self.version,
            parameters=residual_parameters,
            rules=residual_rules,
            rule_lib=self.rule_lib,
            documentation=self.documentation,
            bound_vars=bound_vars,
        )


class _RuleSpecializer:
    """Partially evaluates rules against a set of known parameter values.

    Only conditions whose dependencies are all known are folded. Folding a
    condition never modifies the original rule; residual rules are new
    objects that share no mutable state with the original ruleset.
    """

    def __init__(self, rule_lib):
        self.rule_lib = rule_lib

    def specialize_rules(self, rules, known_values):
        """Specialize a list of rules, preserving their order.

        :type rules: list of BaseRule
        :type known_values: dict
        :rtype: list of BaseRule
        """
        residual_rules = []
        for rule in rules:
            residual_rule = self.specialize_rule(rule, known_values)
            if residual_rule is not None:
                residual_rules.append(residual_rule)
        return residual_rules

    def specialize_rule(self, rule, known_values):
        """Specialize a single rule or return None if it can never match.

        :type rule: BaseRule
        :type known_values: dict
        :rtype: BaseRule or None
        """
        # Each rule is specialized against its own copy of the known values
        # so that variables assigned by folded conditions do not leak into
        # sibling rules, matching runtime scoping.
        known_values = dict(known_values)
        bound_vars = {}
        residual_conditions = []
        for condition in rule.conditions:
            if not self._is_resolvable(condition, known_values):
                residual_conditions.append(deepcopy(condition))
                continue
            result, assigned = self._evaluate_condition(
                condition, known_values
            )
            if result is _CONDITION_UNFOLDABLE:
                residual_conditions.append(deepcopy(condition))
                continue
            if result is False or result is None:
                # The condition can never be met with the fixed values, so
                # the entire rule is dead and can be removed.
                return None
            known_values.update(assigned)
            bound_vars.update(assigned)
        if isinstance(rule, TreeRule):
            residual_subrules = self.specialize_rules(rule.rules, known_values)
            if not residual_subrules:
                return None
            return TreeRule(
                rules=residual_subrules,
                conditions=residual_conditions,
                documentation=rule.documentation,
                bound_vars=bound_vars,
            )
        elif isinstance(rule, EndpointRule):
            return EndpointRule(
                endpoint=deepcopy(rule.endpoint),
                conditions=residual_conditions,
                documentation=rule.documentation,
                bound_vars=bound_vars,
            )
        elif isinstance(rule, ErrorRule):
            return ErrorRule(
                error=deepcopy(rule.error),
                conditions=residual_conditions,
                documentation=rule.documentation,
                bound_vars=bound_vars,
            )
        raise EndpointResolutionError(
            msg=f"Cannot specialize unknown rule type: {type(rule)}"
        )

    def _evaluate_condition(self, condition, known_values):
        """Evaluate a fully resolvable condition against known values.

        Returns a tuple of the condition result and a dict of any scoped
        variables assigned by the condition. If evaluation fails, returns
        ``_CONDITION_UNFOLDABLE`` so the condition is kept in the residual
        rule, preserving the runtime behavior of raising the error only
        when the condition is actually reached.

        :rtype: tuple
        """
        scope_vars = dict(known_values)
        try:
            result = self.rule_lib.call_function(condition, scope_vars)
        except (EndpointResolutionError, KeyError, TypeError):
            return _CONDITION_UNFOLDABLE, {}
        assigned = {
            name: value
            for name, value in scope_vars.items()
            if name not in known_values
        }
        return result, assigned

    def _is_resolvable(self, value, known_values):
        """Determine if a value depends only on known values.

        :type value: Any
        :type known_values: dict
        :rtype: bool
        """
        if self.rule_lib.is_func(value):
            return all(
                self._is_resolvable(arg, known_values)
                for arg in value["argv"]
            )
        elif self.rule_lib.is_ref(value):
            return value["ref"] in known_values
        elif self.rule_lib.is_template(value):
            return self._template_is_resolvable(value, known_values)
        return True

    def _template_is_resolvable(self, template, known_values):
        """Determine if all references in a template string are known.

        :type template: str
        :type known_values: dict
        :rtype: bool
        """
        for _, reference, _, _ in STRING_FORMATTER.parse(template):
            if reference is None:
                continue
            root_reference = reference.split("#")[0]
            if root_reference not in known_values:
                return False
        return True


class EndpointProvider:
    """Derives endpoints from a RuleSet for given input parameters."""

    def __init__(self, ruleset_data, partition_data, excluded_params=None):
        self.ruleset = RuleSet(**ruleset_data, partitions=partition_data)
        self._excluded_params = excluded_params or frozenset()

    def specialize(self, **fixed_parameters):
        """Create a residual provider with the given parameters pre-evaluated.

        The returned provider is a reusable executor that no longer
        evaluates rule conditions that depend only on the fixed parameters.
        Resolving the residual provider with the remaining parameters yields
        the same endpoints and errors as resolving this provider with the
        full set of parameters. This provider is not modified.

        :type fixed_parameters: dict
        :rtype: EndpointProvider
        """
        residual = type(self).__new__(type(self))
        residual.ruleset = self.ruleset.specialize(fixed_parameters)
        residual._excluded_params = self._excluded_params
        return residual

    def resolve_endpoint(self, **input_parameters):
        for param in self._excluded_params:
            input_parameters.pop(param, None)
        return self._resolve_endpoint(**input_parameters)

    @lru_cache_weakref(maxsize=CACHE_SIZE)
    def _resolve_endpoint(self, **input_parameters):
        """Match input parameters to a rule.

        :type input_parameters: dict
        :rtype: RuleSetEndpoint
        """
        params_for_error = input_parameters.copy()
        endpoint = self.ruleset.evaluate(input_parameters)
        if endpoint is None:
            param_string = "\n".join(
                [f"{key}: {value}" for key, value in params_for_error.items()]
            )
            raise EndpointResolutionError(
                msg=f"No endpoint found for parameters:\n{param_string}"
            )
        return endpoint
