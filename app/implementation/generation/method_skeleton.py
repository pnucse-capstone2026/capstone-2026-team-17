"""Compile-safe implementation shells backed by exact method projections."""

from __future__ import annotations

from collections.abc import Mapping

from app.design.schemas.class_model import BCEModel, ClassOperation

from ..planning.method_projection import MethodProjection, MethodProjectionResult
from .java_scaffold import IMPLEMENTATION_MARKER, java_method_name, java_type


def render_backend_method_skeletons(
    model: BCEModel,
    projection: MethodProjectionResult,
    base_package: str,
    *,
    persistence_repositories: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Render one Spring service per Control without inventing call bindings."""

    declared_types = {
        *(item.class_name for item in model.Classes),
        *(item.name for item in model.DataTypes),
    }
    projected = {item.method.operation_id: item for item in projection.methods}
    result: dict[str, str] = {}
    package_path = base_package.replace(".", "/")
    for control in sorted(
        (item for item in model.Classes if item.stereotype == "Control"),
        key=lambda item: item.class_name,
    ):
        methods = [projected.get(item.operation_id) for item in control.operations]
        dependencies = _dependencies(methods)
        repository_dependencies = _repository_dependencies(
            methods, persistence_repositories or {}
        )
        source = _render_service(
            control.class_name,
            control.operations,
            methods,
            dependencies,
            repository_dependencies,
            declared_types,
            base_package,
        )
        result[
            f"{package_path}/application/impl/{control.class_name}Service.java"
        ] = source
    return result


def _dependencies(
    methods: list[MethodProjection | None],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                call.target.class_name
                for method in methods
                if method is not None and method.generation == "code"
                for item in method.slices
                for call in item.outgoing
                if call.generation == "code" and call.target is not None
            }
        )
    )


def _repository_dependencies(
    methods: list[MethodProjection | None],
    persistence_repositories: Mapping[str, str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                persistence_repositories[call.target.class_name]
                for method in methods
                if method is not None
                for item in method.slices
                for call in item.outgoing
                if (
                    call.generation == "hint"
                    and call.target is not None
                    and call.target.stereotype == "Entity"
                    and "target_is_not_generated_spring_dependency" in call.reasons
                    and call.target.class_name in persistence_repositories
                )
            }
        )
    )


def _render_service(
    class_name: str,
    operations: list[ClassOperation],
    methods: list[MethodProjection | None],
    dependencies: tuple[str, ...],
    repository_dependencies: tuple[str, ...],
    declared_types: set[str],
    base_package: str,
) -> str:
    lines = [
        f"package {base_package}.application.impl;",
        "",
        "import java.math.BigDecimal;",
        "import java.net.URI;",
        "import java.time.*;",
        "import java.util.*;",
        "",
        f"import {base_package}.bce.*;",
        *(f"import {dependency};" for dependency in repository_dependencies),
        "import org.springframework.stereotype.Service;",
        "",
        "@Service",
        f"public class {class_name}Service implements {class_name} {{",
    ]
    for dependency in dependencies:
        lines.append(f"    private final {dependency} {_field_name(dependency)};")
    for dependency in repository_dependencies:
        repository = dependency.rsplit(".", 1)[-1]
        lines.append(f"    private final {repository} {_field_name(repository)};")
    if dependencies or repository_dependencies:
        parameters = ", ".join(
            f"{dependency} {_field_name(dependency)}" for dependency in dependencies
        )
        parameters = ", ".join(
            item
            for item in (
                parameters,
                *(
                    f"{dependency.rsplit('.', 1)[-1]} "
                    f"{_field_name(dependency.rsplit('.', 1)[-1])}"
                    for dependency in repository_dependencies
                ),
            )
            if item
        )
        lines.extend(["", f"    public {class_name}Service({parameters}) {{"])
        for dependency in dependencies:
            field = _field_name(dependency)
            lines.append(f"        this.{field} = {field};")
        for dependency in repository_dependencies:
            repository = dependency.rsplit(".", 1)[-1]
            field = _field_name(repository)
            lines.append(f"        this.{field} = {field};")
        lines.append("    }")
    for operation, method in zip(operations, methods, strict=True):
        lines.extend(
            _render_method(operation, method, declared_types)
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_method(
    operation: ClassOperation,
    projection: MethodProjection | None,
    declared_types: set[str],
) -> list[str]:
    return_type = java_type(operation.return_type, declared_types=declared_types)
    parameters = ", ".join(
        f"{java_type(item.type, declared_types=declared_types)} {item.name}"
        for item in operation.parameters
    )
    lines = [
        "",
        "    @Override",
        f"    public {return_type} {java_method_name(operation.name)}({parameters}) {{",
    ]
    rendered_calls = False
    if projection is not None and projection.generation == "code" and projection.slices:
        for call in projection.slices[0].outgoing:
            if call.generation != "code" or call.target is None:
                continue
            arguments = ", ".join(
                item.expression or "" for item in call.arguments
            )
            invocation = (
                f"{_field_name(call.target.class_name)}."
                f"{java_method_name(call.target.name)}({arguments})"
            )
            if call.result_variable:
                lines.append(f"        var {call.result_variable} = {invocation};")
            else:
                lines.append(f"        {invocation};")
            rendered_calls = True
    if return_type == "void" and rendered_calls:
        lines.append("        return;")
    else:
        stable_id = (
            projection.method.stable_id
            if projection is not None
            else operation.stable_id or operation.operation_id
        )
        lines.extend(
            [
                f"        // {IMPLEMENTATION_MARKER}: complete {stable_id}",
                "        throw new UnsupportedOperationException("
                f'"{IMPLEMENTATION_MARKER}:{stable_id}");',
            ]
        )
    lines.append("    }")
    return lines


def _field_name(class_name: str) -> str:
    return class_name[:1].lower() + class_name[1:]
