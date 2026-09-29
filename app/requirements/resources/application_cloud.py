"""EasyDep가 소유하고 진화시키는 애플리케이션·클라우드 정합성 계약이다."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class ModelProvenance(BaseModel):
    """제안 모델의 소유권과 표준 준수 주장 범위를 명시한다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    owner: Literal["EasyDep"] = "EasyDep"
    status: Literal["research-proposal"] = "research-proposal"
    standards_compliant: list[str] = Field(default_factory=list, alias="standardsCompliant")
    inspired_by: list[str] = Field(default_factory=list, alias="inspiredBy")


class ContractFact(BaseModel):
    """열린 namespaced 속성을 감싸는 작고 안정적인 계약 사실이다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list, alias="sourceRefs")
    evidence_refs: list[str] = Field(default_factory=list, alias="evidenceRefs")
    provenance_class: Literal["adopted", "adapted", "hypothesis"] = Field(
        default="hypothesis", alias="provenanceClass"
    )
    extensions: dict[str, Any] = Field(default_factory=dict)


class _FactContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    model_provenance: ModelProvenance = Field(
        default_factory=ModelProvenance, alias="modelProvenance"
    )
    facts: list[ContractFact] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


class ApplicationRuntimeContract(_FactContract):
    """애플리케이션이 소비하는 runtime 사실의 typed 계약이다."""

    schema_version: Literal["ApplicationRuntimeContract/v1"] = Field(
        default="ApplicationRuntimeContract/v1", alias="schemaVersion"
    )


class CloudCapabilityContract(_FactContract):
    """클라우드가 제공하는 capability 사실의 typed 계약이다."""

    schema_version: Literal["CloudCapabilityContract/v1"] = Field(
        default="CloudCapabilityContract/v1", alias="schemaVersion"
    )


class BindingEndpoint(BaseModel):
    """binding이 참조하는 계약 사실과 속성의 끝점이다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    contract: Literal["application", "cloud"]
    fact_id: str = Field(alias="factId")
    attribute: str


class ContractBinding(BaseModel):
    """애플리케이션 소비와 클라우드 제공을 연결하는 단일 binding이다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    kind: str
    consumes: BindingEndpoint
    provides: BindingEndpoint
    invariants: list[dict[str, Any]] = Field(default_factory=list)
    projections: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list, alias="evidenceRefs")
    provenance_class: Literal["adopted", "adapted", "hypothesis"] = Field(
        default="hypothesis", alias="provenanceClass"
    )
    extensions: dict[str, Any] = Field(default_factory=dict)


class DeploymentBindingContract(BaseModel):
    """배포 계획에 수락된 binding 목록과 provenance를 담는 계약이다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["DeploymentBindingContract/v1"] = Field(
        default="DeploymentBindingContract/v1", alias="schemaVersion"
    )
    model_provenance: ModelProvenance = Field(
        default_factory=ModelProvenance, alias="modelProvenance"
    )
    bindings: list[ContractBinding] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


class ConsistencyDiagnostic(BaseModel):
    """애플리케이션·클라우드 계약 정합성 위반 하나를 설명한다."""

    code: str
    message: str
    locations: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


# Detection rules are data, not schema fields. New stacks extend this registry without
# changing the contract format.
_DEPENDENCY_RULES = (
    {
        "id": "java.springdoc-openapi",
        "markers": ("io.swagger.v3.oas.annotations.",),
        "dependencies": (
            (
                "implementation",
                "org.springdoc:springdoc-openapi-starter-webmvc-ui:2.6.0",
            ),
        ),
    },
    {
        "id": "java.openapi-json-nullable",
        "markers": ("org.openapitools.jackson.nullable.",),
        "dependencies": (
            (
                "implementation",
                "org.openapitools:jackson-databind-nullable:0.2.10",
            ),
        ),
    },
    {
        "id": "java.spring-actuator",
        "markers": ("management:\n", "spring-boot-starter-actuator"),
        "dependencies": (
            ("implementation", "org.springframework.boot:spring-boot-starter-actuator"),
        ),
    },
    {
        "id": "java.flyway",
        "markers": ("flyway:\n", "org.flywaydb."),
        "dependencies": (("implementation", "org.flywaydb:flyway-core"),),
    },
    {
        "id": "java.flyway-postgresql",
        "allMarkers": ("flyway:\n", "jdbc:postgresql:"),
        "dependencies": (
            ("runtimeOnly", "org.flywaydb:flyway-database-postgresql"),
        ),
    },
    {
        "id": "java.spring-data-jpa",
        "markers": ("jakarta.persistence.", "org.springframework.data."),
        "dependencies": (
            ("implementation", "org.springframework.boot:spring-boot-starter-data-jpa"),
        ),
    },
    {
        "id": "java.sqlite-jdbc",
        "markers": ("jdbc:sqlite:", "org.sqlite."),
        # Generated sources may directly import SQLiteDataSource/JDBC. The driver
        # must therefore be present on both the compile and runtime classpaths.
        "dependencies": (("implementation", "org.xerial:sqlite-jdbc:3.46.1.0"),),
    },
    {
        "id": "java.hibernate-community-dialects",
        "markers": ("org.hibernate.community.dialect.",),
        "dependencies": (("implementation", "org.hibernate.orm:hibernate-community-dialects"),),
    },
    {
        "id": "java.h2",
        "markers": ("jdbc:h2:", "org.h2."),
        # Keep the same rule for H2 because generated Java may import its driver
        # or datasource classes instead of loading them only through configuration.
        "dependencies": (("implementation", "com.h2database:h2"),),
    },
    {
        "id": "java.postgresql",
        "markers": ("jdbc:postgresql:", "org.postgresql."),
        "dependencies": (("runtimeOnly", "org.postgresql:postgresql"),),
    },
)


def _dependency_rule_matches(text: str, rule: dict[str, Any]) -> bool:
    any_markers = tuple(rule.get("markers") or ())
    all_markers = tuple(rule.get("allMarkers") or ())
    return (not any_markers or any(marker in text for marker in any_markers)) and all(
        marker in text for marker in all_markers
    )


class _DatabaseOrmRule(TypedDict):
    id: str
    databaseMarkers: tuple[str, ...]
    ormMarkers: tuple[str, ...]
    requiredAnyMarkers: tuple[str, ...]
    message: str


class _DatabaseDialectRule(TypedDict):
    id: str
    databaseMarkers: tuple[str, ...]
    invalidClasses: tuple[str, ...]
    supportedClass: str
    coordinate: str
    evidence: str


# Cross-layer runtime requirements are registry data so another database/ORM pair can be
# added without changing the contract schema or tying validation to a benchmark case.
_DATABASE_ORM_RULES: tuple[_DatabaseOrmRule, ...] = (
    {
        "id": "java.jpa-sqlite",
        "databaseMarkers": ("jdbc:sqlite:", "org.sqlite."),
        "ormMarkers": ("jakarta.persistence.", "org.springframework.data.jpa."),
        "requiredAnyMarkers": ("hibernate.dialect", "database-platform"),
        "message": "SQLite used through JPA requires an explicit supported Hibernate dialect.",
    },
)

# Official runtime integrations whose configured class and providing artifact must agree.
# Keep these mappings in replaceable registry data rather than the contract schema.
_DATABASE_DIALECT_RULES: tuple[_DatabaseDialectRule, ...] = (
    {
        "id": "hibernate-community.sqlite",
        "databaseMarkers": ("jdbc:sqlite:", "org.sqlite."),
        "invalidClasses": ("org.hibernate.dialect.SQLiteDialect",),
        "supportedClass": "org.hibernate.community.dialect.SQLiteDialect",
        "coordinate": "org.hibernate.orm:hibernate-community-dialects",
        "evidence": "https://docs.hibernate.org/orm/6.6/dialect/",
    },
)

_DATABASE_ENGINE_ALIASES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "h2": "h2",
    "sqlite": "sqlite",
    "mysql": "mysql",
    "mariadb": "mariadb",
}

_DATABASE_ENGINE_MARKERS = {
    "postgresql": ("jdbc:postgresql:", "org.postgresql.Driver", "PostgreSQLDialect"),
    "h2": ("jdbc:h2:", "org.h2.Driver", "H2Dialect"),
    "sqlite": ("jdbc:sqlite:", "org.sqlite.JDBC", "SQLiteDialect"),
    "mysql": ("jdbc:mysql:", "com.mysql.cj.jdbc.Driver", "MySQLDialect"),
    "mariadb": ("jdbc:mariadb:", "org.mariadb.jdbc.Driver", "MariaDBDialect"),
}

_DATABASE_DRIVER_COORDINATES = {
    "postgresql": ("org.postgresql:postgresql",),
    "h2": ("com.h2database:h2",),
    "sqlite": ("org.xerial:sqlite-jdbc",),
    "mysql": ("com.mysql:mysql-connector-j", "mysql:mysql-connector-java"),
    "mariadb": ("org.mariadb.jdbc:mariadb-java-client",),
}

_JDBC_NETWORK_URL_PREFIX = re.compile(r"^(jdbc:[a-z0-9]+://)", re.IGNORECASE)

_FILE_IO_MARKERS = (
    "java.nio.file.Files",
    "Files.newInputStream(",
    "Files.newOutputStream(",
    "Files.readAllBytes(",
    "Files.readString(",
    "Files.write(",
    "Files.writeString(",
    "FileInputStream(",
    "FileOutputStream(",
    "FileReader(",
    "FileWriter(",
    "RandomAccessFile(",
)
_STATE_PATH_NAME = re.compile(
    r"(?:^|_)(?:STATE|DATA|STORAGE|DB|DATABASE|UPLOAD|CONTENT)(?:_|$)",
    re.IGNORECASE,
)
_SECRET_CONFIGURATION_NAME = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)(?:_|$)",
    re.IGNORECASE,
)


def _application_text(application: Path) -> tuple[str, list[str]]:
    source_root = application / "src" / "main"
    observation_root = source_root if source_root.is_dir() else application
    files = [
        path
        for path in observation_root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".java", ".kt", ".yaml", ".yml", ".properties"}
    ]
    chunks = [path.read_text(encoding="utf-8", errors="replace") for path in files]
    return "\n".join(chunks), [path.relative_to(application).as_posix() for path in files]


def infer_application_contract(
    application: Path, declared: dict[str, Any] | None = None
) -> ApplicationRuntimeContract:
    """선택적 agent 선언과 결정론적 산출물 관찰을 합쳐 앱 계약을 만든다."""
    contract = ApplicationRuntimeContract.model_validate(
        declared
        or {
            "modelProvenance": {
                "owner": "EasyDep",
                "status": "research-proposal",
                "standardsCompliant": [],
                "inspiredBy": ["OAM", "Kubernetes"],
            }
        }
    )
    text, files = _application_text(application)
    facts = {fact.id: fact for fact in contract.facts if not fact.id.startswith("observed.")}
    for rule in _DEPENDENCY_RULES:
        if _dependency_rule_matches(text, rule):
            fact_id = f"observed.{rule['id']}"
            facts.setdefault(
                fact_id,
                ContractFact(
                    id=fact_id,
                    kind="build.dependency",
                    attributes={
                        "declarations": [
                            {"configuration": configuration, "coordinate": coordinate}
                            for configuration, coordinate in rule["dependencies"]
                        ],
                    },
                    sourceRefs=files,
                    provenanceClass="hypothesis",
                    extensions={"detector": rule["id"]},
                ),
            )
    for match in re.finditer(r"\$\{([A-Z][A-Z0-9_]+):([^}]+)\}", text):
        name, default_value = match.groups()
        value_prefix = None
        if default_value.startswith("jdbc:sqlite:"):
            test_template = "jdbc:sqlite:{temp}/database.db"
        elif default_value.startswith("jdbc:h2:file:"):
            test_template = "jdbc:h2:file:{temp}/database"
        else:
            test_template = None
            prefix_match = _JDBC_NETWORK_URL_PREFIX.match(default_value)
            if prefix_match:
                value_prefix = prefix_match.group(1)
            else:
                continue
        fact_id = f"observed.environment.{name.lower()}"
        attributes = {
            "name": name,
            "required": False,
            "default": default_value,
        }
        if test_template:
            attributes["testValueTemplate"] = test_template
        if value_prefix:
            attributes["valuePrefix"] = value_prefix
        facts.setdefault(
            fact_id,
            ContractFact(
                id=fact_id,
                kind="runtime.environment",
                attributes=attributes,
                sourceRefs=files,
                provenanceClass="hypothesis",
                extensions={
                    "detector": (
                        "placeholder.file-database"
                        if test_template
                        else "placeholder.jdbc-network-url"
                    )
                },
            ),
        )
        file_path = _file_database_path(default_value)
        if file_path:
            facts.setdefault(
                "observed.runtime.storage.primary",
                ContractFact(
                    id="observed.runtime.storage.primary",
                    kind="runtime.storage",
                    attributes={
                        "accessPath": str(Path(file_path).parent).replace("\\", "/"),
                        "durability": "persistent",
                        "accessScope": "node-filesystem",
                    },
                    sourceRefs=files,
                    provenanceClass="hypothesis",
                    extensions={"detector": "jdbc.file-path"},
                ),
            )
    # JDBC 이름에 묶이지 않은 일반 파일 상태도 관측한다. 파일 I/O API와 외부설정의
    # 절대 기본 경로가 함께 있어야 하므로 단순 PATH 문자열만으로 상태를 지어내지 않는다.
    if "observed.runtime.storage.primary" not in facts and any(
        marker in text for marker in _FILE_IO_MARKERS
    ):
        configured_path = next(
            (
                match.group("path")
                for match in re.finditer(
                    r"\$\{(?P<name>[A-Z][A-Z0-9_]*):(?P<path>/[^}\s]+)\}",
                    text,
                )
                if _STATE_PATH_NAME.search(match.group("name"))
            ),
            None,
        )
        literal_path = re.search(
            r"(?:Paths\.get|Path\.of)\(\s*[\"'](/[^\"']+)[\"']\s*\)",
            text,
        )
        path = configured_path or (literal_path.group(1) if literal_path else None)
        if path:
            facts["observed.runtime.storage.primary"] = ContractFact(
                id="observed.runtime.storage.primary",
                kind="runtime.storage",
                attributes={
                    "accessPath": str(Path(path).parent).replace("\\", "/"),
                    "durability": "persistent",
                    "accessScope": "node-filesystem",
                },
                sourceRefs=files,
                provenanceClass="hypothesis",
                extensions={"detector": "file-io.external-path"},
            )
    if "observed.runtime.storage.primary" not in facts:
        direct_path = re.search(r"(?:jdbc:sqlite:|jdbc:h2:file:)(/[^\s}'\"]+)", text)
        if direct_path:
            facts["observed.runtime.storage.primary"] = ContractFact(
                id="observed.runtime.storage.primary",
                kind="runtime.storage",
                attributes={
                    "accessPath": str(Path(direct_path.group(1)).parent).replace("\\", "/"),
                    "durability": "persistent",
                    "accessScope": "node-filesystem",
                },
                sourceRefs=files,
                provenanceClass="hypothesis",
                extensions={"detector": "jdbc.file-path"},
            )
    port = _observed_server_port(text)
    facts.setdefault(
        "observed.runtime.port.http",
        ContractFact(
            id="observed.runtime.port.http",
            kind="runtime.port",
            attributes={"name": "http", "port": port, "protocol": "http"},
            sourceRefs=files,
            provenanceClass="hypothesis",
            extensions={"detector": "spring.server-port"},
        ),
    )
    return contract.model_copy(update={"facts": list(facts.values())})


def _file_database_path(value: str) -> str | None:
    if value.startswith("jdbc:sqlite:/"):
        return value.removeprefix("jdbc:sqlite:")
    if value.startswith("jdbc:h2:file:/"):
        return value.removeprefix("jdbc:h2:file:")
    return None


def _observed_server_port(text: str) -> int:
    value = r"(?:\$\{[A-Z][A-Z0-9_]*:)?(\d+)(?:\})?"
    property_match = re.search(rf"(?m)^\s*server\.port\s*=\s*{value}\s*$", text)
    if property_match:
        return int(property_match.group(1))

    lines = text.splitlines()
    for index, line in enumerate(lines):
        server_match = re.match(r"^(\s*)server\s*:\s*(?:#.*)?$", line)
        if not server_match:
            continue
        server_indent = len(server_match.group(1).expandtabs())
        for nested in lines[index + 1 :]:
            if not nested.strip() or nested.lstrip().startswith("#"):
                continue
            nested_indent = len(nested) - len(nested.lstrip())
            if nested_indent <= server_indent:
                break
            port_match = re.match(rf"^\s*port\s*:\s*{value}\s*(?:#.*)?$", nested)
            if port_match:
                return int(port_match.group(1))
    return 8080


def derive_deployment_bindings(
    application: ApplicationRuntimeContract,
    cloud: CloudCapabilityContract,
    declared: dict[str, Any] | None = None,
) -> tuple[CloudCapabilityContract, DeploymentBindingContract]:
    """schema에 기술을 고정하지 않고 앱 소비값에 맞는 배포 binding을 도출한다."""
    binding = DeploymentBindingContract.model_validate(declared or {})
    cloud_facts = {fact.id: fact for fact in cloud.facts if not fact.id.startswith("planned.")}
    bindings = {item.id: item for item in binding.bindings if not item.id.startswith("planned.")}
    app_port = next((fact for fact in application.facts if fact.kind == "runtime.port"), None)
    if app_port is not None:
        cloud_facts["planned.cloud.network.backend"] = ContractFact(
            id="planned.cloud.network.backend",
            kind="cloud.network.backend",
            attributes={
                "port": app_port.attributes.get("port"),
                "protocol": app_port.attributes.get("protocol", "http"),
            },
            sourceRefs=[app_port.id],
            provenanceClass="adapted",
            extensions={"planner": "identity-binding/v1"},
        )
        bindings["planned.binding.http"] = ContractBinding(
            id="planned.binding.http",
            kind="network",
            consumes=BindingEndpoint(contract="application", factId=app_port.id, attribute="port"),
            provides=BindingEndpoint(
                contract="cloud",
                factId="planned.cloud.network.backend",
                attribute="port",
            ),
            invariants=[{"operator": "equals"}],
            provenanceClass="adapted",
            extensions={"planner": "identity-binding/v1"},
        )

    persistent_requested = any(
        fact.attributes.get("required") is True
        and (
            fact.kind
            in {
                "cloud.capability.persistent_storage",
                "cloud.capability.persistent_storage_mount",
            }
            or (
                fact.kind.startswith("cloud.capability.")
                and isinstance(fact.attributes.get("applicationState"), dict)
                and fact.attributes["applicationState"].get("durability") == "persistent"
            )
        )
        for fact in cloud.facts
    )
    intent_storage = next(
        (
            fact
            for fact in application.facts
            if fact.kind == "runtime.storage.intent" and fact.attributes.get("accessPath")
        ),
        None,
    )
    observed_storage = next(
        (fact for fact in application.facts if fact.kind == "runtime.storage"), None
    )
    app_storage = intent_storage or observed_storage
    if persistent_requested and app_storage is not None:
        access_path = app_storage.attributes.get("accessPath")
        cloud_facts["planned.cloud.storage.mount"] = ContractFact(
            id="planned.cloud.storage.mount",
            kind="cloud.storage.mount",
            attributes={"mountPath": access_path},
            sourceRefs=[app_storage.id],
            provenanceClass="adapted",
            extensions={"planner": "identity-binding/v1"},
        )
        bindings["planned.binding.storage"] = ContractBinding(
            id="planned.binding.storage",
            kind="storage",
            consumes=BindingEndpoint(
                contract="application",
                factId=app_storage.id,
                attribute="accessPath",
            ),
            provides=BindingEndpoint(
                contract="cloud",
                factId="planned.cloud.storage.mount",
                attribute="mountPath",
            ),
            invariants=[{"operator": "equals"}],
            provenanceClass="adapted",
            extensions={"planner": "identity-binding/v1"},
        )
    return (
        cloud.model_copy(update={"facts": list(cloud_facts.values())}),
        binding.model_copy(update={"bindings": list(bindings.values())}),
    )


def contract_value(
    contract: ApplicationRuntimeContract | CloudCapabilityContract,
    kind: str,
    attribute: str,
    default: Any = None,
) -> Any:
    """계약에서 kind·attribute가 일치하는 첫 값을 조회한다."""

    fact = next((item for item in contract.facts if item.kind == kind), None)
    return fact.attributes.get(attribute, default) if fact else default


def test_environment(
    contract: ApplicationRuntimeContract, temporary_directory: Path
) -> dict[str, str]:
    """계약이 허용한 EASYDEP 전용 테스트 환경 변수를 구성한다."""

    environment: dict[str, str] = {}
    for fact in contract.facts:
        if fact.kind != "runtime.environment":
            continue
        name = str(fact.attributes.get("name") or "")
        template = str(fact.attributes.get("testValueTemplate") or "")
        # Agent-authored contracts must not override PATH, JAVA_TOOL_OPTIONS, or other
        # inherited process controls. The testing adapter owns this namespace.
        if name.startswith("EASYDEP_") and template:
            environment[name] = template.replace("{temp}", temporary_directory.as_posix())
    return environment


def dependency_declarations(
    contract: ApplicationRuntimeContract,
) -> list[tuple[str, str]]:
    """앱 계약의 검증된 Gradle dependency 선언을 중복 없이 반환한다."""

    declarations: set[tuple[str, str]] = set()
    allowed_configurations = {
        "implementation",
        "runtimeOnly",
        "compileOnly",
        "annotationProcessor",
    }
    coordinate_pattern = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.+\-]+)?$")
    for fact in contract.facts:
        if fact.kind != "build.dependency":
            continue
        raw = fact.attributes.get("declarations") or []
        for item in raw:
            if not isinstance(item, dict):
                continue
            configuration = str(item.get("configuration") or "implementation")
            coordinate = str(item.get("coordinate") or "")
            if coordinate:
                if configuration not in allowed_configurations:
                    raise ValueError(
                        f"Unsupported Gradle dependency configuration: {configuration}"
                    )
                if coordinate_pattern.fullmatch(coordinate) is None:
                    raise ValueError(f"Unsafe dependency coordinate: {coordinate}")
                declarations.add((configuration, coordinate))
    return sorted(declarations)


def cloud_capability_contract_from_requirements(
    requirements_result: dict[str, Any],
) -> CloudCapabilityContract:
    """현재 requirements 결과를 typed cloud capability 계약으로 투영한다.

    Args:
        requirements_result: 현재 분석의 ``deployment_needs``를 포함한 JSON 결과다.

    Returns:
        수락된 capability를 임의 키 손실 없이 옮긴 typed cloud 계약이다.

    Notes:
        저장 checkpoint 호환 adapter가 아니라 현재 requirements→implementation handoff의
        결정론적 projection이다. 따라서 과거 형상을 뜻하는 fallback은 제공하지 않는다.
    """
    from app.requirements.resources.capability_contract import accepted_needs

    needs = accepted_needs(requirements_result.get("deployment_needs") or {})
    facts = []
    for name, need in sorted(needs.items()):
        facts.append(
            ContractFact(
                id=f"capability.{name}",
                kind=f"cloud.capability.{name}",
                attributes={
                    "required": bool(need.get("required")),
                    **dict(need.get("metadata") or {}),
                },
                sourceRefs=list(need.get("requirementIds") or need.get("requirement_ids") or []),
                evidenceRefs=list(need.get("evidenceSpans") or need.get("evidence_spans") or []),
                provenanceClass="hypothesis",
                extensions={
                    key: value
                    for key, value in need.items()
                    if key
                    not in {
                        "required",
                        "metadata",
                        "requirementIds",
                        "requirement_ids",
                        "evidenceSpans",
                        "evidence_spans",
                    }
                },
            )
        )
    return CloudCapabilityContract(facts=facts)


def application_intent_contract_from_requirements(
    requirements_result: dict[str, Any],
) -> ApplicationRuntimeContract:
    """수락된 열린 need에서 명시적으로 근거가 있는 앱 상태 의도만 투영한다."""
    from app.requirements.resources.capability_contract import accepted_needs

    facts: list[ContractFact] = []
    needs = accepted_needs(requirements_result.get("deployment_needs") or {})
    database_values: dict[str, set[Any]] = {
        "engine": set(),
        "version": set(),
        "deploymentMode": set(),
        "embedded": set(),
        "managedServiceAllowed": set(),
    }
    database_source_refs: set[str] = set()
    database_evidence_refs: set[str] = set()
    required_configuration: set[str] = set()
    configuration_source_refs: set[str] = set()
    configuration_evidence_refs: set[str] = set()
    for name, need in sorted(needs.items()):
        metadata = need.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        source_refs = {
            str(item)
            for item in (need.get("requirementIds") or need.get("requirement_ids") or [])
        }
        evidence_refs = {
            str(item)
            for item in (need.get("evidenceSpans") or need.get("evidence_spans") or [])
        }

        raw_database_values = {
            "engine": metadata.get("databaseEngine") or metadata.get("database_engine"),
            "version": metadata.get("databaseVersion") or metadata.get("database_version"),
            "deploymentMode": metadata.get("deploymentMode")
            or metadata.get("deployment_mode"),
            "embedded": metadata.get("embedded"),
            "managedServiceAllowed": (
                metadata.get("managedServiceAllowed")
                if "managedServiceAllowed" in metadata
                else metadata.get("managed_service_allowed")
            ),
        }
        if metadata.get("managed_service_prohibited") is True:
            raw_database_values["managedServiceAllowed"] = False
        contributed_database = False
        for key, value in raw_database_values.items():
            if not isinstance(value, (str, int, float, bool)):
                continue
            if value == "":
                continue
            if key == "engine":
                value = _normalize_database_engine(str(value))
            database_values[key].add(value)
            contributed_database = True
        if contributed_database:
            database_source_refs.update(source_refs)
            database_evidence_refs.update(evidence_refs)

        names = metadata.get("environment_variables") or metadata.get(
            "environmentVariables"
        )
        if isinstance(names, list):
            accepted_names = {
                str(item)
                for item in names
                if re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(item))
            }
            if accepted_names:
                required_configuration.update(accepted_names)
                configuration_source_refs.update(source_refs)
                configuration_evidence_refs.update(evidence_refs)

        state = metadata.get("applicationState") or metadata.get("application_state")
        if not isinstance(state, dict) or not state:
            continue
        attributes = {
            key: state[key]
            for key in ("durability", "accessScope", "accessPath")
            if state.get(key) not in {None, ""}
        }
        if not attributes:
            continue
        facts.append(
            ContractFact(
                id=f"intent.{name}.state",
                kind="runtime.storage.intent",
                attributes={"required": bool(need.get("required")), **attributes},
                sourceRefs=list(need.get("requirementIds") or need.get("requirement_ids") or []),
                evidenceRefs=list(need.get("evidenceSpans") or need.get("evidence_spans") or []),
                provenanceClass="adapted",
                extensions={
                    "source": "accepted-deployment-need",
                    "needId": name,
                    "basis": "TOSCA-requirement-capability-separation",
                },
            )
        )

    database_attributes: dict[str, Any] = {"required": True}
    database_conflicts: dict[str, list[Any]] = {}
    for key, values in database_values.items():
        if len(values) == 1:
            database_attributes[key] = next(iter(values))
        elif len(values) > 1:
            database_conflicts[key] = sorted(values, key=str)
    if database_conflicts:
        database_attributes["conflicts"] = database_conflicts
    if len(database_attributes) > 1:
        facts.append(
            ContractFact(
                id="intent.database",
                kind="runtime.database.intent",
                attributes=database_attributes,
                sourceRefs=sorted(database_source_refs),
                evidenceRefs=sorted(database_evidence_refs),
                provenanceClass="adapted",
                extensions={"source": "accepted-deployment-needs"},
            )
        )
    if required_configuration:
        facts.append(
            ContractFact(
                id="intent.configuration",
                kind="runtime.configuration.intent",
                attributes={
                    "required": True,
                    "requiredKeys": sorted(required_configuration),
                },
                sourceRefs=sorted(configuration_source_refs),
                evidenceRefs=sorted(configuration_evidence_refs),
                provenanceClass="adapted",
                extensions={"source": "accepted-deployment-needs"},
            )
        )
    return ApplicationRuntimeContract(facts=facts)


def _normalize_database_engine(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return _DATABASE_ENGINE_ALIASES.get(normalized, normalized)


def merge_application_contracts(
    requirement_intent: ApplicationRuntimeContract,
    declared: dict[str, Any] | None,
) -> ApplicationRuntimeContract:
    """요구사항 소유 intent가 생성 에이전트 선언으로 덮이지 않게 합친다."""
    agent = ApplicationRuntimeContract.model_validate(declared or {})
    facts = {fact.id: fact for fact in agent.facts if not fact.id.startswith("intent.")}
    facts.update({fact.id: fact for fact in requirement_intent.facts})
    return agent.model_copy(update={"facts": list(facts.values())})


def validate_application_consistency(
    application: Path, contract: ApplicationRuntimeContract
) -> list[ConsistencyDiagnostic]:
    """생성 애플리케이션이 runtime intent와 일치하는지 결정론적으로 검사한다."""

    text, files = _application_text(application)
    build_files = [
        path
        for path in (application / "build.gradle", application / "build.gradle.kts")
        if path.is_file()
    ]
    build_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in build_files
    )
    diagnostics: list[ConsistencyDiagnostic] = []
    for rule in _DEPENDENCY_RULES:
        if not _dependency_rule_matches(text, rule):
            continue
        missing = [
            coordinate
            for _configuration, coordinate in rule["dependencies"]
            if coordinate not in build_text
        ]
        if missing:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-DEP-001",
                    message=f"Observed API/configuration has no matching build dependency: {rule['id']}",
                    locations=files,
                    details={"rule": rule["id"], "missingCoordinates": missing},
                )
            )

    configured_engines = {
        engine: {marker for marker in markers if marker in text}
        for engine, markers in _DATABASE_ENGINE_MARKERS.items()
    }
    active_configured = [
        engine for engine, markers in configured_engines.items() if markers
    ]
    available_drivers = {
        engine
        for engine, coordinates in _DATABASE_DRIVER_COORDINATES.items()
        if any(coordinate in build_text for coordinate in coordinates)
    }
    if len(active_configured) > 1:
        diagnostics.append(
            ConsistencyDiagnostic(
                code="APP-DB-001",
                message="Database URL, driver, or dialect markers select conflicting engines.",
                locations=files,
                details={
                    "configured": {
                        key: sorted(value) for key, value in configured_engines.items()
                    }
                },
            )
        )
    for fact in contract.facts:
        if fact.kind not in {"runtime.database", "runtime.database.intent"}:
            continue
        conflicts = fact.attributes.get("conflicts")
        if isinstance(conflicts, dict) and conflicts:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-DB-INTENT-001",
                    message="Accepted requirements contain conflicting database intentions.",
                    locations=files,
                    details={"conflicts": conflicts, "sourceRefs": fact.source_refs},
                )
            )
        declared_engine = _normalize_database_engine(
            str(fact.attributes.get("engine") or "")
        )
        if declared_engine:
            configured_mismatch = (
                bool(active_configured) and declared_engine not in active_configured
            )
            driver_mismatch = (
                not active_configured
                and declared_engine not in available_drivers
            )
            if configured_mismatch or driver_mismatch:
                diagnostics.append(
                    ConsistencyDiagnostic(
                        code=(
                            "APP-DB-ENGINE-001"
                            if fact.kind == "runtime.database.intent"
                            else "APP-DB-001"
                        ),
                        message=(
                            "Required database engine is absent or conflicts with generated "
                            "runtime configuration."
                        ),
                        locations=files,
                        details={
                            "required": declared_engine,
                            "configured": active_configured,
                            "availableDrivers": sorted(available_drivers),
                            "sourceRefs": fact.source_refs,
                        },
                    )
                )

        deployment_mode = re.sub(
            r"[^a-z0-9]+",
            " ",
            str(fact.attributes.get("deploymentMode") or "").lower(),
        ).strip()
        requires_separate_process = (
            fact.attributes.get("embedded") is False
            or any(
                token in deployment_mode
                for token in ("separate", "container", "self hosted", "external")
            )
        )
        embedded_runtime = (
            "jdbc:h2:mem:" in text
            or "jdbc:sqlite:" in text
        )
        if requires_separate_process and embedded_runtime:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-DB-MODE-001",
                    message=(
                        "A separate database process was required, but generated "
                        "configuration selects an embedded database."
                    ),
                    locations=files,
                    details={
                        "deploymentMode": fact.attributes.get("deploymentMode"),
                        "embeddedRequired": fact.attributes.get("embedded"),
                        "configured": active_configured,
                        "sourceRefs": fact.source_refs,
                    },
                )
            )

    persistent_intent = any(
        fact.kind == "runtime.storage.intent"
        and fact.attributes.get("required") is True
        and fact.attributes.get("durability") == "persistent"
        for fact in contract.facts
    )
    if persistent_intent and "jdbc:h2:mem:" in text:
        diagnostics.append(
            ConsistencyDiagnostic(
                code="APP-STORAGE-001",
                message=(
                    "Persistent application state was required, but generated "
                    "configuration uses an in-memory database."
                ),
                locations=files,
                details={"configured": "jdbc:h2:mem:"},
            )
        )

    for fact in contract.facts:
        if fact.kind != "runtime.configuration.intent":
            continue
        required_keys = {
            str(item)
            for item in (fact.attributes.get("requiredKeys") or [])
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(item))
        }
        missing_keys = sorted(key for key in required_keys if key not in text)
        if missing_keys:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-CONFIG-001",
                    message="Required runtime configuration inputs are not consumed by the app.",
                    locations=files,
                    details={
                        "requiredKeys": sorted(required_keys),
                        "missingKeys": missing_keys,
                        "sourceRefs": fact.source_refs,
                    },
                )
            )
        secret_defaults: dict[str, str] = {}
        for key in sorted(required_keys):
            if _SECRET_CONFIGURATION_NAME.search(key) is None:
                continue
            match = re.search(rf"\$\{{{re.escape(key)}:([^}}]+)\}}", text)
            if match is not None and match.group(1).strip():
                secret_defaults[key] = match.group(1).strip()
        if secret_defaults:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-CONFIG-SECRET-001",
                    message=(
                        "Required secret configuration contains a source-controlled "
                        "non-empty default value."
                    ),
                    locations=files,
                    details={
                        "keysWithDefaults": sorted(secret_defaults),
                        "sourceRefs": fact.source_refs,
                    },
                )
            )
    for orm_rule in _DATABASE_ORM_RULES:
        database_present = any(marker in text for marker in orm_rule["databaseMarkers"])
        orm_present = any(marker in text for marker in orm_rule["ormMarkers"])
        has_runtime_integration = any(
            marker in text for marker in orm_rule["requiredAnyMarkers"]
        )
        if database_present and orm_present and not has_runtime_integration:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-DB-002",
                    message=orm_rule["message"],
                    locations=files,
                    details={
                        "rule": orm_rule["id"],
                        "requiredAnyMarkers": list(orm_rule["requiredAnyMarkers"]),
                    },
                )
            )
    for dialect_rule in _DATABASE_DIALECT_RULES:
        if not any(marker in text for marker in dialect_rule["databaseMarkers"]):
            continue
        configured_invalid = [
            class_name
            for class_name in dialect_rule["invalidClasses"]
            if class_name in text
        ]
        if configured_invalid:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-DB-003",
                    message=(
                        "Configured ORM dialect class is not provided by the selected "
                        "runtime integration."
                    ),
                    locations=files,
                    details={
                        "rule": dialect_rule["id"],
                        "configuredClasses": configured_invalid,
                        "supportedClass": dialect_rule["supportedClass"],
                        "requiredCoordinate": dialect_rule["coordinate"],
                        "evidence": dialect_rule["evidence"],
                    },
                )
            )
        elif (
            dialect_rule["supportedClass"] in text
            and dialect_rule["coordinate"] not in build_text
        ):
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="APP-DEP-001",
                    message="Configured ORM dialect class has no providing build dependency.",
                    locations=files,
                    details={
                        "rule": dialect_rule["id"],
                        "missingCoordinates": [dialect_rule["coordinate"]],
                        "evidence": dialect_rule["evidence"],
                    },
                )
            )
    return diagnostics


def validate_binding_consistency(
    application: ApplicationRuntimeContract,
    cloud: CloudCapabilityContract,
    binding: DeploymentBindingContract,
) -> list[ConsistencyDiagnostic]:
    """애플리케이션 소비값과 클라우드 제공값의 binding 불일치를 검사한다."""

    facts = {
        "application": {fact.id: fact for fact in application.facts},
        "cloud": {fact.id: fact for fact in cloud.facts},
    }
    diagnostics: list[ConsistencyDiagnostic] = []
    for item in binding.bindings:
        left = facts[item.consumes.contract].get(item.consumes.fact_id)
        right = facts[item.provides.contract].get(item.provides.fact_id)
        if left is None or right is None:
            diagnostics.append(
                ConsistencyDiagnostic(
                    code="BIND-REF-001",
                    message=f"Binding {item.id} references an absent contract fact.",
                    details={"binding": item.model_dump(mode="json", by_alias=True)},
                )
            )
            continue
        left_value = left.attributes.get(item.consumes.attribute)
        right_value = right.attributes.get(item.provides.attribute)
        for invariant in item.invariants:
            if invariant.get("operator") == "equals" and left_value != right_value:
                code = {
                    "network": "BIND-PORT-001",
                    "storage": "BIND-STORAGE-001",
                }.get(item.kind, "BIND-VALUE-001")
                diagnostics.append(
                    ConsistencyDiagnostic(
                        code=code,
                        message=f"Binding {item.id} requires equal values.",
                        details={"consumes": left_value, "provides": right_value},
                    )
                )
    return diagnostics
