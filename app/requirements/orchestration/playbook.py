"""플레이북 — 지난 실행에서 배운 것을 다음 실행이 읽는 canonical 저장 경계다.

## 지식베이스와 왜 따로 두나

`knowledge/rules.py`가 담는 것은 **규범**이다: 책이 그렇게 적었고, 좌표를 댈 수 있고,
로컬 사본으로 대조된다. 여기 쌓이는 것은 **우리 실행에서 실제로 일어난 일**이다 — 근거가
책이 아니라 우리 로그다.

둘을 한 목록에 섞으면 인용 없는 문장이 인용 있는 문장과 같은 무게로 읽힌다. 이 저장소가
`basis.py`로 이미 그은 선이 그것이고, 여기서도 같은 선을 긋는다: 플레이북 블록은 **자기가
무엇인지 밝히고** 규칙 목록과 다른 절에 실린다.

## 무엇이 배울 자격을 갖는가 — 우리 측정이 정한 것

**LLM 판정은 도메인에 따라 78~90% 흔들린다**(§7~§9). 흔들리는 판정에서 배우면 잡음이
프롬프트에 굳고, 그 프롬프트가 다음 실행의 입력이 되어 스스로를 재생산한다. 그래서 출처마다
문턱이 다르다 — 결정론 검출기는 낮게, LLM 검증자는 높게, 사람이 한 말은 그 사이.

## 무엇을 담나 — "주의하라"가 아니라 **우리가 실제로 쓴 문장**

"규칙 X에 주의하라"는 이미 규칙 목록에 있는 말이라 새 정보가 없다. 더할 수 있는 것은
**우리가 그 규칙을 어떻게 어겼는가**뿐이라, 아티팩트에서 위반 문장을 그대로 꺼내 반례로
싣는다. 지어내지 않으므로 틀릴 수가 없다.

## 아직 없는 것

**반성자(Reflector)가 없다.** ACE/GEPA의 "트레이스를 LLM이 읽고 지침을 고쳐 쓴다" 층은
넣지 않았다 — 반례만으로 얼마나 가는지 먼저 재야 이득의 출처를 가른다. 효과를 아직 안 쟀고,
그래서 기본값이 꺼짐이다(`settings.playbook_enabled`). 자세한 것은 §12.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.requirements.knowledge import rules

#: 검출기가 낸 결함이 실릴 문턱 — **서로 다른 실행** 수. 결정론이므로 낮다.
MIN_RUNS_DETECTOR = 2
#: 검증자가 낸 결함이 실릴 문턱. 판정이 흔들리므로 높다(§7~§9).
MIN_RUNS_VALIDATOR = 3
#: 사용자 피드백이 실릴 문턱. 검증자보다 낮다(사람이 한 말이라 판정 잡음이 아니다).
#: 그래도 1은 아니다 — 한 번의 지시는 그 산출물에 대한 주문일 수 있다.
MIN_RUNS_FEEDBACK = 2
#: 단계 하나에 실을 항목 수 상한. 컨텍스트가 무너지는 것을 막는다 — ACE가 말하는
#: context collapse는 "요약해서 잃는 것"이고, 여기서는 "늘려서 묻는 것"이 같은 값이다.
MAX_ENTRIES_PER_STAGE = 6
#: 항목 하나에 실을 반례 수. 하나면 우연으로 보이고, 많으면 그 도메인 말투가 새어 나간다.
MAX_EXAMPLES_PER_ENTRY = 2
#: 반례 문장 길이 상한(글자). 긴 문장은 잘라서 싣는다.
MAX_EXAMPLE_CHARS = 160


@dataclass(frozen=True)
class Observation:
    """실행 하나에서 관찰된 위반 한 건."""

    rule_id: str
    stage: str
    #: `detector` | `validator`. 문턱이 갈리는 근거라 반드시 있어야 한다.
    source: str
    run_id: str
    dataset: str
    #: 위반한 문장 그대로. 못 찾으면 None(위치가 문장이 아닌 지적도 있다).
    sentence: str | None = None


@dataclass
class Entry:
    """플레이북 항목 하나 — 규칙 하나에 대해 우리가 쌓은 것."""

    rule_id: str
    stage: str
    detector_runs: list[str] = field(default_factory=list)
    validator_runs: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def runs(self) -> int:
        """이 규칙을 어긴 **서로 다른 실행** 수."""
        return len(set(self.detector_runs) | set(self.validator_runs))

    @property
    def qualifies(self) -> bool:
        """다음 실행에 실을 자격이 있는가.

        검출기 쪽이 문턱을 넘으면 그것으로 충분하다 — 결정론 신호가 이미 있는데 검증자
        반복까지 요구하면 확실한 것을 버리게 된다.
        """
        if len(set(self.detector_runs)) >= MIN_RUNS_DETECTOR:
            return True
        return len(set(self.validator_runs)) >= MIN_RUNS_VALIDATOR

    def as_dict(self) -> dict:
        """규칙 위반 항목을 중복 제거된 저장 JSON shape로 바꾼다."""

        return {
            "rule_id": self.rule_id,
            "stage": self.stage,
            "detector_runs": sorted(set(self.detector_runs)),
            "validator_runs": sorted(set(self.validator_runs)),
            "datasets": sorted(set(self.datasets)),
            "examples": self.examples,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Entry:
        """저장 JSON shape에서 규칙 위반 항목을 복원한다."""

        return cls(
            rule_id=raw["rule_id"],
            stage=raw["stage"],
            detector_runs=list(raw.get("detector_runs", [])),
            validator_runs=list(raw.get("validator_runs", [])),
            datasets=list(raw.get("datasets", [])),
            examples=list(raw.get("examples", [])),
        )


@dataclass
class FeedbackLesson:
    """사용자가 되풀이해 요구한 것 하나.

    규칙 위반과 **따로 둔다.** 규칙은 근거를 댈 수 있고 판정자가 있지만 이건 "사람이 그렇게
    원했다"는 사실뿐이다. 한 목록에 섞으면 규칙처럼 읽히고, 그러면 다음 사람이 없는 근거를
    찾는다.
    """

    #: 규칙 단계 그룹(`model_use_cases` 등). 생성 프롬프트가 단계별로 조립되기 때문이다.
    stage: str
    #: **사용자의 말 그대로.** 우리가 요약하지 않는다 — 요약하는 순간 우리 판단이 섞인다.
    instruction: str
    runs: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)

    @property
    def times(self) -> int:
        """서로 다른 실행에서 같은 feedback이 관찰된 횟수를 반환한다."""

        return len(set(self.runs))

    @property
    def qualifies(self) -> bool:
        """feedback이 다음 prompt에 실릴 최소 반복 수를 넘었는지 판단한다."""

        return self.times >= MIN_RUNS_FEEDBACK

    def as_dict(self) -> dict:
        """사용자 feedback lesson을 중복 제거된 저장 JSON shape로 바꾼다."""

        return {
            "stage": self.stage,
            "instruction": self.instruction,
            "runs": sorted(set(self.runs)),
            "datasets": sorted(set(self.datasets)),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> FeedbackLesson:
        """저장 JSON shape에서 사용자 feedback lesson을 복원한다."""

        return cls(
            stage=raw["stage"],
            instruction=raw["instruction"],
            runs=list(raw.get("runs", [])),
            datasets=list(raw.get("datasets", [])),
        )


def _stage_group_of(cascade_key: str) -> str | None:
    """논리 단계(`specs`) → 규칙 단계 그룹(`write_specifications`). 단계 목록에서 파생한다.

    함수 안에서 import하는 것은 import 순서 때문이다(이 모듈은 프롬프트 조립에서 불린다).
    """
    from app.requirements import stage_registry as stages

    return next((s.group for s in stages.PIPELINE if s.key == cascade_key), None)


def _normalized(instruction: str) -> str:
    """같은 요구인지 보려고 다듬은 형태. 저장은 원문으로 한다."""
    return " ".join(instruction.split()).strip().casefold()


def observe_feedback(
    lessons: list[FeedbackLesson], intent, run_id: str, dataset: str = ""
) -> list[FeedbackLesson]:
    """적용된 피드백 하나를 배운 것에 반영한다.

    **`broad`만 받는다.** `local`은 대상 id를 들고 오는 산출물 수술이지 다음 실행에 물릴
    정책이 아니다 — "UC3 이름을 바꿔라"를 얹으면 다음 실행이 없는 UC3를 찾는다. 범위가 곧
    일반화 가능성의 신호라 따로 판단할 필요가 없다.
    """
    instruction = (getattr(intent, "instruction", "") or "").strip()
    if getattr(intent, "scope", "") != "broad" or not instruction:
        return lessons
    group = _stage_group_of(getattr(intent, "stage", ""))
    if group is None:
        return lessons

    key = _normalized(instruction)
    for lesson in lessons:
        if lesson.stage == group and _normalized(lesson.instruction) == key:
            if run_id not in lesson.runs:
                lesson.runs.append(run_id)
            if dataset and dataset not in lesson.datasets:
                lesson.datasets.append(dataset)
            return lessons
    lessons.append(FeedbackLesson(
        stage=group, instruction=instruction,
        runs=[run_id], datasets=[dataset] if dataset else [],
    ))
    return lessons


# ---------------------------------------------------------------------------
# 수확 — 실행 아티팩트에서 관찰을 꺼낸다 (LLM 없음)
# ---------------------------------------------------------------------------
def _sentences_by_location(spec: dict) -> dict[str, str]:
    """`step 3`·`3a1` 같은 위치 → 그 자리의 문장.

    검출기 지적은 위치를 들고 다니지만 문장은 안 들고 다닌다(`detectors.Finding`).
    반례를 실으려면 문장이 필요하므로 명세에서 되찾는다 — 지적 문구를 파싱하지 않는다.
    """
    out = {"trigger": spec.get("trigger", "")}
    for step in spec.get("main_scenario", []) or []:
        out[f"step {step.get('step_number')}"] = step.get("sentence", "")
    for ext in spec.get("extensions", []) or []:
        for handling in ext.get("handling_steps", []) or []:
            out[str(handling.get("sub_step"))] = handling.get("sentence", "")
    return {k: v for k, v in out.items() if v}


def _source_of(rule_id: str) -> str | None:
    """이 규칙을 낸 판정자. 플레이북이 아는 것은 두 갈래뿐이다."""
    try:
        judge = rules.rule(rule_id).judged_by
    except KeyError:
        # 지식베이스에 없는 규칙을 인용한 지적 — `semantic_status="ungrounded"`가 이미
        # 버리는 것이지만, 여기까지 새어 들어오면 **배우지 않는다**.
        return None
    if judge == rules.JUDGED_DETECTOR:
        return "detector"
    if judge == rules.JUDGED_VALIDATOR:
        return "validator"
    return None


def harvest(run_dir: Path | str) -> list[Observation]:
    """실행 하나에서 관찰을 꺼낸다.

    읽는 것은 `use_case_specs.json`의 `issues`다 — **반성 루프가 끝나고도 남은** 결함이다.
    고쳐진 결함에서 배우면 안 된다: 그건 시스템이 이미 처리한 것이고, 그걸 프롬프트에
    얹으면 이미 푼 문제를 계속 상기시키게 된다.
    """
    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    run_id = manifest.get("run_id") or run_dir.name
    dataset = manifest.get("dataset") or ""

    specs_path = run_dir / "use_case_specs.json"
    if not specs_path.exists():
        return []
    specs = json.loads(specs_path.read_text(encoding="utf-8"))

    out: list[Observation] = []
    for spec in specs:
        locations = _sentences_by_location(spec)
        for issue in spec.get("issues", []) or []:
            rule_id = rules.rule_of(issue)
            if rule_id is None:
                continue
            source = _source_of(rule_id)
            if source is None:
                continue
            out.append(Observation(
                rule_id=rule_id,
                stage=rules.rule(rule_id).stage,
                source=source,
                run_id=run_id,
                dataset=dataset,
                sentence=_sentence_for(issue, locations),
            ))
    return out


#: 산문 안에서 스텝을 가리키는 말("... from step 3", "before step 5").
#: 좁은 공백(U+202F)까지 받는 것은 실측이다 — 검증자가 실제로 그렇게 쓴다.
_STEP_IN_PROSE = re.compile(r"step[\s  ]*(\d+)", re.IGNORECASE)


def _sentence_for(issue: str, locations: dict[str, str]) -> str | None:
    """지적이 가리키는 위치의 문장. 못 찾으면 None — 반례 없이 세기만 한다.

    지적 문구의 꼴이 판정자마다 다르다:

      - 검출기: `"step 3: UI 용어 [...] [<꼬리표>]"` — 위치가 머리에 있다.
      - 검증자: `"[semantic] Remove the reference ... from step 3 [<꼬리표>]"` — 위치가
        지시 산문 안에 있다.

    둘 다 받는다. 못 찾으면 조용히 넘어가는 것이 맞다 — 억지로 문장을 하나 고르면
    **엉뚱한 문장이 반례로 굳는다.** 세는 것만으로도 문턱은 작동한다.
    """
    head = issue.split(":", 1)[0].strip()
    if head in locations:
        return locations[head]
    found = _STEP_IN_PROSE.search(issue)
    return locations.get(f"step {found.group(1)}") if found else None


# ---------------------------------------------------------------------------
# 큐레이션 — 델타로 합친다 (전면 재작성 없음)
# ---------------------------------------------------------------------------
def curate(entries: list[Entry], observations: list[Observation]) -> list[Entry]:
    """관찰을 기존 항목에 **더한다.**

    전면 재작성을 하지 않는 것이 핵심이다(ACE의 context collapse). 여기서는 그게 저절로
    지켜진다 — 항목의 내용이 LLM이 쓴 요약이 아니라 **관찰의 누적**이라서, 합치는 연산이
    덧셈뿐이기 때문이다. 나중에 반성자를 얹더라도 이 성질을 깨지 않아야 한다.
    """
    by_id = {e.rule_id: e for e in entries}
    for obs in observations:
        entry = by_id.get(obs.rule_id)
        if entry is None:
            entry = Entry(rule_id=obs.rule_id, stage=obs.stage)
            by_id[obs.rule_id] = entry
        runs = entry.detector_runs if obs.source == "detector" else entry.validator_runs
        if obs.run_id not in runs:
            runs.append(obs.run_id)
        if obs.dataset and obs.dataset not in entry.datasets:
            entry.datasets.append(obs.dataset)
        if obs.sentence:
            example = _trim(obs.sentence)
            # 반례는 **가장 오래된 것부터** 유지한다. 새 것으로 밀어내면 같은 실행을 여러 번
            # 돌릴 때마다 예시가 흔들려, 프롬프트가 실행마다 달라진다.
            if example not in entry.examples and len(entry.examples) < MAX_EXAMPLES_PER_ENTRY:
                entry.examples.append(example)
    return list(by_id.values())


def _trim(sentence: str) -> str:
    flat = " ".join(sentence.split())
    return flat if len(flat) <= MAX_EXAMPLE_CHARS else flat[: MAX_EXAMPLE_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# 저장 — 사람이 읽고 지울 수 있는 한 파일
# ---------------------------------------------------------------------------
def load(path: Path | str) -> list[Entry]:
    """저장된 규칙 위반 playbook을 읽고 파일이 없으면 빈 목록을 반환한다."""

    path = Path(path)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Entry.from_dict(e) for e in raw.get("entries", [])]


def load_feedback(path: Path | str) -> list[FeedbackLesson]:
    """저장된 사용자 feedback lesson을 읽고 파일이 없으면 빈 목록을 반환한다."""

    path = Path(path)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [FeedbackLesson.from_dict(e) for e in raw.get("feedback", [])]


def save(
    path: Path | str,
    entries: list[Entry],
    lessons: list[FeedbackLesson] | None = None,
) -> None:
    """항목을 파일로. **사람이 지울 수 있어야 한다** — 배운 것이 틀렸을 때 되돌리는 길이
    그것뿐이다(프롬프트 안에 굳으면 지울 자리가 없다).

    `lessons`를 안 주면 파일에 있던 것을 그대로 둔다. 규칙 쪽만 갱신하는 호출이
    **사용자 피드백을 조용히 지우는** 일이 없어야 한다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if lessons is None:
        lessons = load_feedback(path)
    body = {
        "note": (
            "실행에서 관찰된 위반(entries)과 사용자가 되풀이해 요구한 것(feedback)의 누적. "
            "책 근거가 아니라 우리 로그가 근거다. 지워도 된다 — 다음 실행이 다시 쌓는다."
        ),
        "entries": [e.as_dict() for e in sorted(entries, key=lambda e: e.rule_id)],
        "feedback": [f.as_dict() for f in sorted(lessons, key=lambda f: (f.stage, f.instruction))],
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def observe_run(path: Path | str, run_dir: Path | str) -> list[Entry]:
    """실행 하나를 읽어 플레이북에 반영하고 저장한다."""
    entries = curate(load(path), harvest(run_dir))
    save(path, entries)
    return entries


def record_feedback(path: Path | str, intent, run_id: str, dataset: str = "") -> None:
    """적용된 피드백을 파일에 반영한다. **실패해도 조용히 넘어간다.**

    요청 경로에서 불리므로, 배우는 층이 사용자의 요청을 실패시키면 안 된다.
    """
    try:
        lessons = observe_feedback(load_feedback(path), intent, run_id, dataset)
        save(path, load(path), lessons)
    except Exception:  # noqa: BLE001 - 배우기 실패가 응답을 망가뜨리면 안 된다
        from app.requirements.runtime import telemetry

        telemetry.record_degradation("playbook.feedback", "피드백을 플레이북에 못 남겼다")


# ---------------------------------------------------------------------------
# 렌더 — 생성 프롬프트에 실리는 절
# ---------------------------------------------------------------------------
def _ranked(entries: list[Entry], stage: str) -> Iterator[Entry]:
    """자격을 갖춘 항목을, 자주 어긴 것부터."""
    qualified = [e for e in entries if e.stage == stage and e.qualifies]
    yield from sorted(qualified, key=lambda e: (-e.runs, e.rule_id))[:MAX_ENTRIES_PER_STAGE]


def render(
    entries: list[Entry], stage: str, lessons: list[FeedbackLesson] | None = None
) -> str:
    """생성 프롬프트에 붙일 절들. 실을 것이 없으면 빈 문자열.

    **각 절이 자기가 무엇인지 밝히고 시작한다.** 규칙 목록과 같은 판에 놓이면, 근거가 우리
    로그일 뿐인 문장이 책 좌표를 단 문장과 같은 무게로 읽힌다.

    규칙 위반과 사용자 요구를 **다른 절로** 낸다. 앞엣것은 "우리가 틀렸다"이고 뒤엣것은
    "사람이 이렇게 원한다"라서, 근거의 성격도 지켜야 할 이유도 다르다.
    """
    blocks = []

    broken = []
    for entry in _ranked(entries, stage):
        where = f"{entry.runs} past runs"
        if entry.datasets:
            where += f" ({', '.join(sorted(set(entry.datasets))[:3])})"
        broken.append(f"- ({entry.rule_id}) broken in {where}.")
        broken.extend(f'    we wrote: "{example}"' for example in entry.examples)
    if broken:
        blocks.append("\n".join([
            "[WHAT WE GOT WRONG BEFORE]",
            "Not rules — these are our own past outputs that broke the rules above. No source",
            "beyond our run logs. Do not copy these sentences; do not write new ones like them.",
            *broken,
        ]))

    asked = [
        f'- asked {lesson.times}x: "{lesson.instruction}"'
        for lesson in sorted(
            (le for le in (lessons or []) if le.stage == stage and le.qualifies),
            key=lambda le: (-le.times, le.instruction),
        )[:MAX_ENTRIES_PER_STAGE]
    ]
    if asked:
        blocks.append("\n".join([
            "[WHAT USERS KEPT ASKING FOR]",
            "Standing requests from the people this output is for, repeated across runs, in",
            "their own words. Not rules — no source but their asking. Where one conflicts with",
            "a rule above, follow the rule and leave the conflict visible.",
            *asked,
        ]))

    return "\n\n".join(blocks)
