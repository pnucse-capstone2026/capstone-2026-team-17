"""요구사항 정제의 few-shot sampling과 system prompt 조립을 소유한다."""

from app.requirements.legacy.example_sampler import (
    DEFAULT_DATASET,
    format_examples,
    sample_examples,
)

# few-shot 예시 추출 방법 → (strategy, backend) 매핑.
# 실험(docs/research/fewshot-sampling-experiment-results.md) 결과 채택한 두 방법:
#   - "random"  : 무작위 baseline. 쿼리 불필요, 오프라인.
#   - "mmr+nim" : NIM 임베딩으로 관련성+다양성 균형(top-k 중복 억제). 쿼리 필요.
_METHODS = {
    "random": ("random", "tfidf"),
    "mmr+nim": ("mmr", "nim"),
}


def extract_examples_from_xlsx(
    file_path: str = DEFAULT_DATASET,
    sample_size: int = 5,
    query: str = "",
    method: str = "none",
) -> str:
    """데이터셋에서 few-shot 예시를 뽑아 [Reference Examples] 블록으로 반환.

    method="random" 은 기존 baseline(무작위). method="mmr+nim" 은 query(추상 요구사항)와
    의미가 가까우면서 서로 다양한 예시를 NIM 임베딩으로 선별한다. 샘플링 로직은
    example_sampler 모듈 참고.
    """
    if method == "none":
        return ""
    try:
        strategy, backend = _METHODS.get(method, _METHODS["random"])
        items = sample_examples(
            query=query,
            dataset_path=file_path,
            sample_size=sample_size,
            strategy=strategy,
            backend=backend,
        )
        if not items:
            raise ValueError("샘플링 결과가 비었습니다.")
        return format_examples(items)
    except Exception:
        # 선택적 실험 자료나 스프레드시트 리더 때문에 실제 요구사항 경로가 중단되면
        # 안 된다. Windows 레거시 인코딩에서 진단 기호 출력 자체가 실패할 수도 있어
        # 여기서는 콘솔에도 쓰지 않는다.
        return ""


def refine_requirements_prompt(
    abstract_text: str,
    dataset_path: str = DEFAULT_DATASET,
    method: str = "none",
) -> str:
    """
    데이터셋을 참고하여 추상적 요구사항을 분석하고 JSON 딕셔너리로 반환합니다.

    method="mmr+nim" 이면 abstract_text 를 쿼리로 써서 관련 예시를 선별한다.
    (기본값은 기존 동작 유지를 위해 random.)
    """

    # 데이터셋에서 예시 추출
    dataset_examples = extract_examples_from_xlsx(
        dataset_path, query=abstract_text, method=method
    )

    # 2. 동적 시스템 프롬프트 정의 (FR/NFR 분류 제거)
    reference_block = (
        f"[Reference Examples]\n{dataset_examples}"
        if dataset_examples
        else "[Reference Examples]\nNone. Follow the explicit rules below."
    )
    SYSTEM_PROMPT = f"""
    You are a System Architect and an expert in Requirements Engineering.
    You are given a set of user requirement statements. Refine them into specific, testable
    requirements while preserving the user's level of abstraction. Decomposition is selective,
    not a goal by itself.
    Reference examples demonstrate writing style only. Never copy their facts, actors, technology,
    quantities, thresholds, or level of detail into the user's requirements.

    [Refinement boundary]
    - Preserve one refined statement per source statement by default. Split functional behavior
      only when the source contains independently initiated and independently completed actor
      goals with distinct acceptance outcomes. Keep their shared RAW source reference.
    - A refined statement may contain several cohesive steps from one user journey or lifecycle
      operations under one management responsibility. Do not merge a separate optional actor goal
      into its prerequisite goal merely because both were written in one source sentence.
    - Preserve conditional and temporal context that explains when an optional goal becomes
      available, and preserve an explicit statement that its prerequisite goal remains complete
      without it. These clauses are relationship evidence for the optional goal, not independent
      requirements: keep them in the same refined statement. Do not shorten them away or emit
      them as separate requirementDrafts.
    - Separate every non-functional / quality constraint into its OWN statement. NEVER fuse a
      performance ("within 1 second", "within 500 ms"), load ("under 200 concurrent sessions"),
      security ("encrypted at rest"), reliability/atomicity ("recorded atomically"), or availability
      constraint into a functional sentence.
    - A separated constraint MUST carry its subject so it stands alone. Example:
        Compound:  "The system shall let a registered user log in and return a response within 1 second."
        Split ->   "The system shall authenticate a registered user by email and password."
                   "The login response shall be returned within 1 second of request receipt."
    - Keep together actions that share one actor, business object, precondition, and acceptance
      outcome. Lifecycle operations described under one management goal may remain together.
    - Keep one policy statement for an enumerated set of data, roles, resources, or lifecycle
      operations. Do not duplicate the same policy once per list item.
    - Keep declarative domain and role facts declarative. A statement that defines a role,
      specialization, business term, or domain fact is evidence for later analysis; do not
      rewrite it as behavior such as "the system shall define/configure/manage" that the source
      did not request.
    - Keep a constraint and its explicit scope together when the latter only strengthens the same
      acceptance rule, for example a rule that must also hold under concurrency.
    - Every refined statement must stand on its own. Do not emit a sibling sentence that refers
      back with phrases such as "the prevention", "the operation", or "the above behavior".
    - Do not expand an umbrella verb such as "manage" into invented create/update/delete actions.
    - Do not replace a provider-neutral constraint with a particular topology, service, protocol,
      product, or implementation mechanism.
    - Preserve the source's subject, responsibility, modality, and logical polarity. A prohibition
      or allowance must not be rewritten as one stronger positive solution, and a capability
      assigned to an external environment must not become application behavior.
    - Do not introduce numeric values, units, deadlines, thresholds, actors, external systems,
      behavior, or acceptance criteria that are not stated by the source requirements.
    - Split an independently verifiable quality constraint that would otherwise be fused into
      functional behavior, and split independently initiated actor goals as defined above. Do not
      split a source statement merely because it contains multiple verbs. The normal output count
      should remain close to the input count.
    - Whenever you split a constraint out, RECORD it in constraint_links: map the constraint
      sentence to the functional sentence it qualifies (both written verbatim as they appear in
      requirementDrafts[].text). This preserves the FR<-NFR traceability link.

    [Source provenance]
    - The user message labels every source statement RAW1..N. Return each refined sentence as one
      requirementDrafts object containing text and sourceRefs.
    - sourceRefs must cite all and only the RAW statements from which the refined text was derived.
      Do not return a separate provenance mapping.

    {reference_block}
    """

    return SYSTEM_PROMPT
