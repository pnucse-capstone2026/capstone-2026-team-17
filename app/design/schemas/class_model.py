"""클래스·시퀀스 상호작용 설계가 공유하는 엄격한 영속 계약.

연산은 클래스 수준 시그니처다. 협업은 그 시그니처의 구체적인 실행을 기록하며,
argument provenance와 호출 순서를 저장하는 유일한 위치다. graph adapter는 raw JSON을 이
모델로 검증하고 service 결과를 ``model_dump(by_alias=True)``로 기존 state에 되돌린다.

이 schema는 LLM 응답 계약이 아니다. 제안·repair용 일시 모델은
``services.class_diagram.proposals``에 있으며 prompt나 telemetry field는 여기에 추가하지
않는다. validator는 canonical operation/call ID와 이름 유일성을 보장하지만 LLM을 호출하지
않는다.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.design.contracts.type_system import (
    canonical_design_type,
    parse_type_expression,
    referenced_names,
)


def _parameter_value(parameter: object, name: str) -> str:
    if isinstance(parameter, dict):
        return str(parameter.get(name) or "")
    return str(getattr(parameter, name, "") or "")


def canonical_operation_id(
    class_name: str, operation_name: str, parameters: Sequence[object],
) -> str:
    """owner·이름·parameter 순서와 타입으로 안정적인 operation 식별자를 반환한다.

    반환 타입은 overload 선택에 쓰지 않으므로 ID에 넣지 않는다. 예를 들어
    ``OrderService::place(orderId:UUID)``는 renderer와 collaboration이 공유하는 참조다.
    """

    signature = ",".join(
        f"{_parameter_value(parameter, 'name')}:{_parameter_value(parameter, 'type')}"
        for parameter in parameters
    )
    return f"{class_name}::{operation_name}({signature})"


def operation_method_signature(
    operation_name: str, parameters: Sequence[object], return_type: str,
) -> str:
    """UML 시그니처가 필요한 호출자를 위한 표시 투영을 반환한다."""

    arguments = ", ".join(
        f"{_parameter_value(parameter, 'name')} : {_parameter_value(parameter, 'type')}"
        for parameter in parameters
    )
    return f"{operation_name}({arguments}): {return_type}"


def canonical_call_id(collaboration_id: str, position: int) -> str:
    """1부터 시작하는 호출 위치로 결정론적 호출 식별자를 만든다."""

    return f"{collaboration_id}::call:{position}"


class ClassModelBase(BaseModel):
    """저장되는 클래스 모델 레코드가 공유하는 엄격한 설정이다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ClassParameter(ClassModelBase):
    """operation signature의 이름 있는 입력 하나다."""
    name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: str = Field(min_length=1)
    # Exact accepted required-value declaration used to justify this input.
    required_value_ref: str | None = Field(default=None, alias="requiredValueRef", min_length=1)
    # Application-issued identity for the parameter itself.
    stable_ref: str | None = Field(default=None, alias="stableRef", min_length=1)


class ClassOperation(ClassModelBase):
    """BCE class가 소유하며 use-case step을 추적하는 수락 operation이다."""
    operation_id: str = Field(alias="operationId", min_length=1)
    # ``operationId`` remains the renderer/collaboration signature.  A stable
    # identity is deliberately optional at the schema boundary so artifacts
    # written before stable identities were introduced still hydrate.
    stable_id: str | None = Field(default=None, alias="stableId", min_length=1)
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    parameters: list[ClassParameter] = Field(default_factory=list)
    return_type: str = Field(default="void", alias="returnType", min_length=1)
    step_refs: list[str] = Field(default_factory=list, alias="stepRefs")

    @field_validator("step_refs")
    @classmethod
    def normalize_step_refs(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values))

    def method_signature(self) -> str:
        return operation_method_signature(self.name, self.parameters, self.return_type)


class AcceptedBCEClass(ClassModelBase):
    """구조 inventory와 operation fragment가 합쳐진 영속 BCE class다."""
    class_name: str = Field(alias="className", min_length=1)
    # Class fields remain renderer-compatible strings.  Their application
    # identities live in this aligned sidecar, just as DataType fields do.
    stable_id: str | None = Field(default=None, alias="stableId", min_length=1)
    stereotype: Literal["Boundary", "Control", "Entity"]
    description: str = ""
    fields: list[str] = Field(default_factory=list)
    field_refs: list[str] = Field(default_factory=list, alias="fieldRefs")
    use_case_ids: list[str] = Field(alias="use_case_ids", default_factory=list)
    identifier: list[str] = Field(default_factory=list)
    operations: list[ClassOperation] = Field(default_factory=list)

    @field_validator("class_name")
    @classmethod
    def reject_unknown_class_placeholder(cls, value: str) -> str:
        normalized = "".join(character for character in value.casefold() if character.isalnum())
        if normalized.startswith("unknownclass"):
            raise ValueError("className must identify a concrete BCE class")
        return value

    @model_validator(mode="after")
    def canonicalize_operations(self) -> AcceptedBCEClass:
        """중복 method를 거부하고 owner가 포함된 canonical operation ID를 덮어쓴다."""
        names = [operation.name for operation in self.operations]
        if len(names) != len(set(names)):
            raise ValueError("operation names must be unique within a class")
        for operation in self.operations:
            parameter_names = [parameter.name for parameter in operation.parameters]
            if len(parameter_names) != len(set(parameter_names)):
                raise ValueError("operation parameter names must be unique")
            operation.operation_id = canonical_operation_id(
                self.class_name, operation.name, operation.parameters
            )
        return self

    @model_validator(mode="after")
    def validate_field_refs(self) -> AcceptedBCEClass:
        if self.field_refs and len(self.field_refs) != len(self.fields):
            raise ValueError("fieldRefs must align with Class fields")
        if len(self.field_refs) != len(set(self.field_refs)):
            raise ValueError("Class fieldRefs must be unique")
        return self


class DataType(ClassModelBase):
    """구조 또는 operation signature가 참조하는 valueObject/enum 선언이다."""
    name: str = Field(min_length=1)
    # DataType fields are strings for backwards-compatible UML rendering.
    # ``fieldRefs`` is an aligned, application-managed sidecar rather than a
    # wire-format migration of those declarations.
    stable_id: str | None = Field(default=None, alias="stableId", min_length=1)
    kind: Literal["valueObject", "enumeration"]
    fields: list[str] = Field(default_factory=list)
    field_refs: list[str] = Field(default_factory=list, alias="fieldRefs")
    values: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def reject_unknown_data_type(cls, value: str) -> str:
        normalized = "".join(character for character in value.casefold() if character.isalnum())
        if normalized.startswith("unknownclass"):
            raise ValueError("DataType name must be concrete")
        return value

    @model_validator(mode="after")
    def require_a_minimal_definition(self) -> DataType:
        if self.kind == "enumeration" and not self.values:
            raise ValueError("an enumeration needs values")
        if self.kind == "valueObject" and not self.fields:
            raise ValueError("a valueObject needs fields")
        if not self.fields and not self.values:
            raise ValueError("a DataType needs fields and/or values")
        return self

    @model_validator(mode="after")
    def validate_field_refs(self) -> DataType:
        if self.field_refs and len(self.field_refs) != len(self.fields):
            raise ValueError("fieldRefs must align with DataType fields")
        if len(self.field_refs) != len(set(self.field_refs)):
            raise ValueError("DataType fieldRefs must be unique")
        return self


class AcceptedBCERelationship(ClassModelBase):
    """inventory가 승인한 구조 관계다. 호출 dependency는 저장하지 않는다."""
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: str = "Association"
    source_multiplicity: str = Field(default="", alias="sourceMultiplicity")
    target_multiplicity: str = Field(default="", alias="targetMultiplicity")
    description: str = ""


class ArgumentBinding(ClassModelBase):
    """receiver parameter를 유한 provenance 문법의 source 하나에 연결한다."""
    parameter: str = Field(min_length=1)
    source_ref: str = Field(alias="sourceRef", min_length=1)


class CollaborationCall(ClassModelBase):
    """한 execution group call tree의 canonical call node다."""
    call_id: str = Field(alias="callId", min_length=1)
    # ``callId`` is position-based legacy identity; ``stableId`` is the
    # application-managed identity used when a call is renamed/repositioned.
    stable_id: str | None = Field(default=None, alias="stableId", min_length=1)
    parent_call_id: str | None = Field(default=None, alias="parentCallId")
    receiver_operation_id: str = Field(alias="receiverOperationId", min_length=1)
    step_refs: list[str] = Field(default_factory=list, alias="stepRefs")
    argument_bindings: list[ArgumentBinding] = Field(
        default_factory=list, alias="argumentBindings"
    )

    @field_validator("step_refs")
    @classmethod
    def normalize_step_refs(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values))


class Collaboration(ClassModelBase):
    """한 execution group이 소유하는 순서 있는 call tree다."""
    collaboration_id: str = Field(alias="collaborationId", min_length=1)
    use_case_ids: list[str] = Field(alias="useCaseIds", min_length=1)
    entry_actor: str | None = Field(default=None, alias="entryActor")
    calls: list[CollaborationCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonicalize_call_ids(self) -> Collaboration:
        """저장 배열 위치를 기준으로 call ID를 canonical form으로 고정한다."""
        for position, call in enumerate(self.calls, start=1):
            call.call_id = canonical_call_id(self.collaboration_id, position)
        return self


class BCEModel(ClassModelBase):
    """graph state와 API에 저장되는 클래스 상호작용 설계 최상위 모델이다."""
    Classes: list[AcceptedBCEClass] = Field(default_factory=list)
    DataTypes: list[DataType] = Field(default_factory=list)
    Relationships: list[AcceptedBCERelationship] = Field(default_factory=list)
    Collaborations: list[Collaboration] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonicalize_and_resolve_types(self) -> BCEModel:
        """Persist one type notation and reject references absent from this model."""

        declared = {
            *(item.class_name for item in self.Classes),
            *(item.name for item in self.DataTypes),
        }

        def canonical(value: str, *, allow_void: bool) -> str:
            expression = parse_type_expression(value)
            if not allow_void and expression.kind == "scalar" and expression.name == "void":
                raise ValueError("void is not a valid field or parameter type")
            missing = referenced_names(expression) - declared
            if missing:
                raise ValueError(
                    "type references must name a declared Class or DataType: "
                    + ", ".join(sorted(missing))
                )
            return canonical_design_type(value)

        operation_ids: dict[str, str] = {}
        for class_item in self.Classes:
            normalized_fields: list[str] = []
            for declaration in class_item.fields:
                name, separator, raw_type = declaration.partition(":")
                if not separator or not name.strip() or not raw_type.strip():
                    raise ValueError(
                        f"{class_item.class_name} has an invalid field declaration: {declaration}"
                    )
                normalized_fields.append(
                    f"{name.strip()} : {canonical(raw_type, allow_void=False)}"
                )
            class_item.fields = normalized_fields
            for operation in class_item.operations:
                previous_id = operation.operation_id
                for parameter in operation.parameters:
                    parameter.type = canonical(parameter.type, allow_void=False)
                operation.return_type = canonical(operation.return_type, allow_void=True)
                operation.operation_id = canonical_operation_id(
                    class_item.class_name, operation.name, operation.parameters
                )
                operation_ids[previous_id] = operation.operation_id

        for data_type in self.DataTypes:
            normalized_fields = []
            for declaration in data_type.fields:
                name, separator, raw_type = declaration.partition(":")
                if not separator or not name.strip() or not raw_type.strip():
                    raise ValueError(
                        f"{data_type.name} has an invalid field declaration: {declaration}"
                    )
                normalized_fields.append(
                    f"{name.strip()} : {canonical(raw_type, allow_void=False)}"
                )
            data_type.fields = normalized_fields

        for collaboration in self.Collaborations:
            for call in collaboration.calls:
                call.receiver_operation_id = operation_ids.get(
                    call.receiver_operation_id, call.receiver_operation_id
                )
        return self

    @model_validator(mode="after")
    def unique_named_elements(self) -> BCEModel:
        names = [item.class_name for item in self.Classes]
        if len(names) != len(set(names)):
            raise ValueError("className values must be unique")
        data_type_names = [item.name for item in self.DataTypes]
        if len(data_type_names) != len(set(data_type_names)):
            raise ValueError("DataType names must be unique")
        if set(names) & set(data_type_names):
            raise ValueError("Class and DataType names must not overlap")
        collaboration_ids = [item.collaboration_id for item in self.Collaborations]
        if len(collaboration_ids) != len(set(collaboration_ids)):
            raise ValueError("collaborationId values must be unique")
        operation_stable_ids = [
            operation.stable_id
            for item in self.Classes
            for operation in item.operations
            if operation.stable_id is not None
        ]
        if len(operation_stable_ids) != len(set(operation_stable_ids)):
            raise ValueError("operation stableId values must be unique")
        parameter_stable_refs = [
            parameter.stable_ref
            for item in self.Classes
            for operation in item.operations
            for parameter in operation.parameters
            if parameter.stable_ref is not None
        ]
        if len(parameter_stable_refs) != len(set(parameter_stable_refs)):
            raise ValueError("parameter stableRef values must be unique")
        data_type_stable_ids = [
            item.stable_id for item in self.DataTypes if item.stable_id is not None
        ]
        if len(data_type_stable_ids) != len(set(data_type_stable_ids)):
            raise ValueError("DataType stableId values must be unique")
        class_stable_ids = [
            item.stable_id for item in self.Classes if item.stable_id is not None
        ]
        if len(class_stable_ids) != len(set(class_stable_ids)):
            raise ValueError("Class stableId values must be unique")
        class_field_refs = [
            field_ref for item in self.Classes for field_ref in item.field_refs
        ]
        if len(class_field_refs) != len(set(class_field_refs)):
            raise ValueError("Class fieldRefs must be unique")
        data_type_field_refs = [
            field_ref for item in self.DataTypes for field_ref in item.field_refs
        ]
        if len(data_type_field_refs) != len(set(data_type_field_refs)):
            raise ValueError("DataType fieldRefs must be unique")
        call_stable_ids = [
            call.stable_id
            for item in self.Collaborations
            for call in item.calls
            if call.stable_id is not None
        ]
        if len(call_stable_ids) != len(set(call_stable_ids)):
            raise ValueError("call stableId values must be unique")
        return self
