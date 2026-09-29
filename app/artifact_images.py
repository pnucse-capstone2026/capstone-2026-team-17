"""PlantUML 산출물을 화면 주소에 맞춰 미리 렌더링하고 메모리에 보관한다.

구조화된 설계 모델은 계속 MySQL의 기준 데이터로 남는다. 이 모듈은 사람이 보는 SVG/PNG만
잠시 보관하며, 산출물 저장이 끝난 직후 두 형식을 만든다. 이미지 API는 앱 전체 설계를 다시
복원하지 않고 이 cache에서 bytes를 꺼낸다.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from threading import RLock
from typing import Any

from app.design.schemas.architecture_state import ArchitectureState
from app.design.services.common.plantuml import render_plantuml
from app.design.services.deployment_diagram.provider_plantuml import (
    deployment_bundle_provisioning_puml,
    deployment_bundle_runtime_puml,
)
from app.design.services.sequence_diagram.plantuml import generate_sequence_from_model

MAIN_VIEW = "main"
RUNTIME_VIEW = "runtime"
PROVISIONING_VIEW = "provisioning"
IMAGE_FORMATS = ("svg", "png")
ROUTE_CACHE_CAPACITY = 1024

_STAGE_PUML_FIELDS = {
    "usecase_diagram": "usecase_diagram_puml",
    "class_diagram": "class_diagram_puml",
    "erd": "erd_puml",
}


def sequence_view(use_case_id: str) -> str:
    """유스케이스 ID가 다른 stage view 이름과 겹치지 않게 cache key를 만든다."""
    return f"use-case:{use_case_id}"


def deployment_target_view(view: str, target_id: str) -> str:
    """같은 view의 CSP·리전 후보가 cache에서 서로 덮어쓰지 않게 한다."""

    return f"{view}-target:{target_id}"


def sequence_diagrams_from_state(
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """저장 모델을 프론트엔드가 사용하는 유스케이스별 시퀀스 목록으로 정리한다."""
    model = state.get("sequence_diagram_model") or {}
    if not isinstance(model, dict):
        return []
    diagrams = model.get("Diagrams")
    if isinstance(diagrams, list):
        normalized: list[dict[str, Any]] = []
        for index, diagram in enumerate(diagrams):
            if not isinstance(diagram, dict):
                continue
            use_case_id = str(diagram.get("use_case_id") or f"sequence-{index + 1}")
            normalized.append(
                {
                    **diagram,
                    "use_case_id": use_case_id,
                    "use_case_name": str(diagram.get("use_case_name") or use_case_id),
                }
            )
        return normalized
    if model.get("Participants") or model.get("Messages"):
        return [
            {
                "use_case_id": "sequence",
                "use_case_name": "Sequence Diagram",
                **model,
            }
        ]
    return []


def stage_diagram_sources(
    stage: str, state: Mapping[str, Any],
) -> dict[str, str]:
    """한 stage의 HTTP view 이름과 PlantUML 원문을 연결한다.

    시퀀스와 배포는 한 stage 안에 여러 그림이 있으므로 view를 나눈다. 일반 stage 이미지
    주소가 계속 동작하도록 대표 그림은 언제나 ``main``에도 연결한다.
    """
    field = _STAGE_PUML_FIELDS.get(stage)
    if field is not None:
        puml = str(state.get(field) or "")
        return {MAIN_VIEW: puml} if puml.strip() else {}

    if stage == "sequence_diagram":
        diagrams = sequence_diagrams_from_state(state)
        sources = {
            sequence_view(str(diagram["use_case_id"])): generate_sequence_from_model(diagram)
            for diagram in diagrams
        }
        if diagrams:
            sources[MAIN_VIEW] = sources[sequence_view(str(diagrams[0]["use_case_id"]))]
        return {view: puml for view, puml in sources.items() if puml.strip()}

    if stage == "deployment_diagram":
        runtime = str(state.get("deployment_diagram_puml") or "")
        provisioning = str(state.get("deployment_diagram_provisioning_puml") or "")
        deployment_sources: dict[str, str] = {}
        if runtime.strip():
            deployment_sources[MAIN_VIEW] = runtime
            deployment_sources[RUNTIME_VIEW] = runtime
        if provisioning.strip():
            deployment_sources[PROVISIONING_VIEW] = provisioning
        bundle = state.get("deployment_diagram_bundle") or {}
        if isinstance(bundle, dict):
            # 비교 중인 후보도 실제 ResourcePlan에서 렌더한다. 후보 하나만 담은 작은
            # view bundle을 만들면 기존 renderer를 바꾸거나 projection을 복제할 필요가 없다.
            for projection in bundle.get("projections") or []:
                if not isinstance(projection, dict) or not isinstance(
                    projection.get("target"), dict
                ):
                    continue
                target = dict(projection["target"])
                target_id = str(target.get("id") or "")
                if not target_id:
                    continue
                view_bundle = {
                    **bundle,
                    "selectedTarget": target,
                    "projections": [projection],
                }
                candidate_runtime = deployment_bundle_runtime_puml(view_bundle)
                candidate_provisioning = deployment_bundle_provisioning_puml(view_bundle)
                if candidate_runtime.strip():
                    deployment_sources[
                        deployment_target_view(RUNTIME_VIEW, target_id)
                    ] = candidate_runtime
                if candidate_provisioning.strip():
                    deployment_sources[
                        deployment_target_view(PROVISIONING_VIEW, target_id)
                    ] = candidate_provisioning
        return deployment_sources

    return {}


class ArtifactImageCache:
    """앱·stage·view별 최신 이미지를 보관하는 bounded cache다."""

    def __init__(self, capacity: int = ROUTE_CACHE_CAPACITY) -> None:
        self._capacity = max(1, capacity)
        self._images: OrderedDict[tuple[str, str, str, str], bytes] = OrderedDict()
        self._lock = RLock()

    def get(
        self, app_id: str, stage: str, view: str, image_format: str,
    ) -> bytes | None:
        """현재 route에 미리 렌더된 이미지가 있으면 반환한다."""
        key = (app_id, stage, view, image_format)
        with self._lock:
            image = self._images.get(key)
            if image is not None:
                self._images.move_to_end(key)
            return image

    def warm_stage(
        self, app_id: str, stage: str, state: Mapping[str, Any],
    ) -> int:
        """산출물 저장 직후 해당 stage의 SVG와 PNG를 모두 렌더링한다.

        렌더 중에는 이전 버전 이미지를 먼저 지운다. 새 모델 저장 뒤 예전 그림이 보이는 것보다
        잠시 cache miss가 나는 편이 안전하다. 공통 renderer가 내용 SHA cache를 갖고 있으므로
        ``main``과 ``runtime``처럼 원문이 같은 view는 실제로 한 번만 렌더링된다.
        """
        sources = stage_diagram_sources(stage, state)
        if not sources:
            return 0
        self.invalidate_stage(app_id, stage)
        rendered = {
            (view, image_format): render_plantuml(puml, image_format)
            for view, puml in sources.items()
            for image_format in IMAGE_FORMATS
        }
        with self._lock:
            for (view, image_format), image in rendered.items():
                if not image:
                    continue
                key = (app_id, stage, view, image_format)
                self._images[key] = image
                self._images.move_to_end(key)
            while len(self._images) > self._capacity:
                self._images.popitem(last=False)
        return sum(1 for image in rendered.values() if image)

    def invalidate_stage(self, app_id: str, stage: str) -> None:
        """피드백으로 새 버전이 저장되기 전에 그 stage의 예전 route를 제거한다."""
        with self._lock:
            for key in list(self._images):
                if key[0] == app_id and key[1] == stage:
                    self._images.pop(key, None)

    def clear(self) -> None:
        """테스트 또는 개발 서버 재시작 준비에서 route cache를 비운다."""
        with self._lock:
            self._images.clear()


artifact_image_cache = ArtifactImageCache()


def warm_artifact_images(
    app_id: str, stage: str, state: ArchitectureState,
) -> int:
    """repository가 사용할 작은 public 함수다. 이미지가 없는 stage는 아무 일도 하지 않는다."""
    return artifact_image_cache.warm_stage(app_id, stage, state)
