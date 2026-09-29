"""리소스 제약 구조화 단계의 조회 도구를 제공한다.

## 왜 도구인가

이 단계는 사용자가 자연어로 쓴 클라우드 제약을 `RESOURCE_SPEC` 계약으로 옮긴다.
예전 판은 그 일을 정규식으로 했고, **못 하는 일을 원칙으로 굳히는** 데까지 갔다 —
`"USD가 아니다 — 계약이 환율 환산을 거부한다"`는 계약의 뜻이 아니라 우리에게 환율
소스가 없었다는 뜻이었다. 없는 능력은 거부할 것이 아니라 **쥐어 줄** 것이다.

그래서 여기 있는 것들은 전부 "모델이 지어내면 안 되지만 알아 올 수는 있는 것"이다.

  - `resolve_region` — 지명 → 리전 코드. **카탈로그가 답한다.** 모델이 코드를
    지어내면 뒤 단계의 조인이 조용히 빈 답이 된다.
  - `list_cloud_providers` — 우리가 실제로 아는 프로바이더 id. 조인이 도는 축이다.
  - `list_workload_kinds` — 이 프로바이더에서 **실측이 있는** 배포 종류. 계약의
    위상축(`workloads`)이 받는 값이고, 계획 전체가 그 위에 선다. 목록이 프로바이더마다
    다른 것이 핵심이라 스키마 enum으로 못 박을 수 없다 — 모델이 외워서 쓰면 실측 없는
    종류가 들어오고, 그러면 그 부분 계획이 조용히 빈다.
  - `convert_to_usd` — 환율. 계약이 USD를 요구하므로 필요하고, 값이 매일 바뀌므로
    **핀을 박을 수 없다.** 그래서 환산값만이 아니라 **환율·기준일·출처를 함께**
    돌려준다 — 근거에 그대로 실린다(`digest` 소스에 쓰는 규율과 같다).
  - `web_search` — 사용자가 쓴 표현을 **해석하는 데에만** 쓴다. 사용자가 말하지 않은
    값을 웹에서 주워 오는 것은 지어냄이고, 그건 프롬프트가 금지한다.

## 도구가 아닌 것

`record_field` · `ask_user` · `finish`는 여기 없다. 그 셋은 **이번 실행의 상태를
바꾸는** 행동이라 실행마다 새로 묶여야 하고, `step_resource.py`가 만든다.
"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from app.cloudkb import regions

#: 환율 출처. ECB 기준환율을 그대로 서빙하는 공개 API(키 없음).
#:
#: **핀을 박을 수 없는 소스다** — 값이 매일 바뀐다. 이 저장소가 데이터셋에 요구하는
#: tag/commit/digest 고정을 여기에는 걸 수 없고, 걸 수 있는 척해서도 안 된다. 대신
#: 규율을 옮긴다: **쓴 환율과 기준일을 근거에 남긴다.** 그러면 나중에 "이 금액이 왜
#: 이 값이 됐나"에 답할 수 있다.
_FX_URL = "https://api.frankfurter.dev/v1/latest"
_FX_SOURCE = "European Central Bank reference rates (via frankfurter.dev)"

#: 환율 조회 상한(초). 이 단계는 사람을 기다리게 하는 자리라 길게 잡지 않는다 —
#: 못 가져오면 **사용자에게 묻는 것이 정상 경로**이고, 그건 실패가 아니다.
_FX_TIMEOUT = 8.0

_LIST_CLOUD_PROVIDERS_DESCRIPTION = """List the cloud provider ids this system actually knows.

`RESOURCE_SPEC.provider` must be one of these exact ids. A provider we do not
know cannot be joined against the pricing, capacity or region datasets, so a
plausible-looking name is worse than an empty field."""
_LIST_WORKLOAD_KINDS_DESCRIPTION = """List the workload kinds this provider has **measurements** for.

`RESOURCE_SPEC.workloads` must hold ids from this list. The whole deployment plan
— creation order, deletion order, what the provider synthesises on its own, what
breaks when a part is detached — is computed from this field, so an id that is not
in the list carries no knowledge at all and the plan for it would be empty.

The list differs per provider on purpose: it is derived from what was actually
measured against each cloud, not from a fixed vocabulary.

Args:
    provider: a provider id from `list_cloud_providers`."""
_RESOLVE_REGION_DESCRIPTION = """Resolve a place name (e.g. "Seoul", "서울", "us-east-1") to region codes.

`RESOURCE_SPEC.region` must be a region **code**, never a place name — every
downstream join is on the code. Do not write a code from memory: ask here.

Pass `provider` once you know it; the same place exists at many providers and
without a provider the answer is usually ambiguous on purpose. An ambiguous
answer is a question for the user, not something for you to pick from.

Args:
    place: the place as the user wrote it.
    provider: a provider id from `list_cloud_providers`, or "" if not yet known."""
_CONVERT_TO_USD_DESCRIPTION = """Convert an amount in another currency to USD at today's reference rate.

`RESOURCE_SPEC.monthlyBudgetUSD` is in USD, so a budget the user stated in
another currency needs converting. Use this instead of doing the arithmetic
yourself — the rate, its date and its source have to end up in the record.

Report the converted number **together with** the rate and date this returns,
so the user can see what their figure was turned into.

Args:
    amount: the numeric amount the user stated.
    currency: its ISO 4217 code (KRW, EUR, JPY, GBP …)."""
_WEB_SEARCH_DESCRIPTION = """Search the web to **interpret what the user wrote** — never to supply a value.

Legitimate use: the user named a company or a data-centre and you need to know
which cloud provider or currency that is. Illegitimate use: the user never said
a budget and you go looking for what a system like theirs usually costs. A value
the user did not state is not theirs, and this step must not invent one.

Args:
    query: the search query.
    max_results: how many results to return."""


@tool(description=_LIST_CLOUD_PROVIDERS_DESCRIPTION)
def list_cloud_providers() -> str:
    """지원하는 cloud provider id를 카탈로그에서 조회한다.

    Returns:
        ``RESOURCE_SPEC.provider``에 기록할 수 있는 provider id 목록이다.

    Notes:
        프로세스에 적재된 cloud 지식만 읽고 외부 호출을 만들지 않는다.
    """
    return ", ".join(regions.providers())


@tool(description=_LIST_WORKLOAD_KINDS_DESCRIPTION)
def list_workload_kinds(provider: str) -> str:
    """Provider별 실측 workload kind를 조회한다.

    Args:
        provider: ``list_cloud_providers``가 반환한 provider id다.

    Returns:
        해당 provider에서 측정 근거가 있는 workload id와 개수다.

    Notes:
        고정 vocabulary가 아니라 provider별 측정 closure를 사용한다.
    """
    from app.requirements.resources import input_registry

    kinds = input_registry.anchors_for((provider or "").strip().lower())
    if not kinds:
        return (f"No measured workload kinds for provider {provider!r}. "
                "Check the provider id with list_cloud_providers.")
    return (f"{provider} has measurements for {len(kinds)} workload kinds:\n"
            + "\n".join(f"- {k}" for k in kinds)
            + "\n\nRecord one or more of these ids in `workloads`, comma separated.")


@tool(description=_RESOLVE_REGION_DESCRIPTION)
def resolve_region(place: str, provider: str = "") -> str:
    """사용자가 쓴 장소를 provider region code 후보로 해소한다.

    Args:
        place: 사용자가 작성한 장소명 또는 region code다.
        provider: 이미 알려진 provider id이며 미정이면 빈 문자열이다.

    Returns:
        일치 후보와 모호성 여부를 보존한 설명 문자열이다.

    Notes:
        후보가 여러 개일 때 임의 선택하지 않고 사용자 질문으로 넘길 정보를 반환한다.
    """
    found = regions.resolve(place, provider=provider or None)
    if not found:
        return (f"No region matches {place!r}"
                + (f" for provider {provider!r}." if provider else ".")
                + " This means we did not understand it, not that it does not exist.")
    lines = [f"{c.code}  ({c.provider}, {c.display_name})" for c in found[:12]]
    codes = {c.code for c in found}
    head = (f"{place!r} resolves to exactly one region code."
            if len(codes) == 1 else
            f"{place!r} is ambiguous — {len(codes)} different region codes match.")
    return head + "\n" + "\n".join(lines)


@tool(description=_CONVERT_TO_USD_DESCRIPTION)
def convert_to_usd(amount: float, currency: str) -> str:
    """사용자가 명시한 예산을 당일 기준환율로 USD로 환산한다.

    Args:
        amount: 사용자가 명시한 금액이다.
        currency: 원 통화의 ISO 4217 code다.

    Returns:
        환산액, 환율, 기준일과 출처를 포함한 JSON 문자열 또는 실패 설명이다.

    Notes:
        외부 환율 조회 실패 시 값을 추측하지 않고 사용자 질문 경로로 넘긴다.
    """
    code = (currency or "").strip().upper()
    if code == "USD":
        return json.dumps({"usd": amount, "note": "already USD, no conversion"})
    if not code.isalpha() or len(code) != 3:
        return f"{currency!r} is not an ISO 4217 currency code."
    try:
        import httpx

        response = httpx.get(_FX_URL, params={"base": code, "symbols": "USD"},
                             timeout=_FX_TIMEOUT)
        response.raise_for_status()
        body = response.json()
        rate = float(body["rates"]["USD"])
    except Exception as exc:  # noqa: BLE001 — 못 가져오면 되묻는 것이 정상 경로다
        return (f"Could not fetch a {code}->USD rate ({type(exc).__name__}). "
                "Do not guess one. Ask the user for the budget in USD instead.")
    return json.dumps({
        "usd": round(amount * rate, 2),
        "rate": rate,
        "from": code,
        "amount": amount,
        "asOf": body.get("date", ""),
        "source": _FX_SOURCE,
    }, ensure_ascii=False)


@tool(description=_WEB_SEARCH_DESCRIPTION)
def web_search(query: str, max_results: int = 4) -> str:
    """사용자가 쓴 표현을 해석하기 위한 검색 결과만 조회한다.

    Args:
        query: 사용자 표현을 해석하기 위한 검색어다.
        max_results: 반환할 최대 결과 수다.

    Returns:
        제목·주소·요약을 묶은 문자열 또는 실패 설명이다.

    Notes:
        사용자가 말하지 않은 resource 값을 보충하는 용도로 사용하지 않는다.
    """
    try:
        from ddgs import DDGS

        hits = list(DDGS().text(query, max_results=max(1, min(max_results, 8))))
    except Exception as exc:  # noqa: BLE001 — 검색 실패는 이 단계를 막지 않는다
        return f"Search failed ({type(exc).__name__}). Proceed without it."
    if not hits:
        return "No results."
    return "\n\n".join(
        f"{h.get('title', '')}\n{h.get('href', '')}\n{h.get('body', '')}" for h in hits
    )


#: 조회 도구들. 상태를 바꾸지 않으므로 실행 간에 공유해도 된다.
LOOKUP_TOOLS = [list_cloud_providers, resolve_region, convert_to_usd, web_search]
