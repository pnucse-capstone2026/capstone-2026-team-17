"""리전 이름 ↔ 리전 코드. `서울` → 프로바이더별 리전 코드.

**왜 인프라인가.** costkb는 가격을, perfkb는 스펙을 리전 코드로 색인한다. 그런데 사람은
`ap-northeast-2`라고 묻지 않고 "서울"이라고 묻는다. 둘 이상이 쓰는 공용 인프라라
여기 둔다(`kbcommon/__init__.py`의 규칙).

**왜 이게 필요했나.** 에이전트 실측에서 "서울 리전에서 GPU 인스턴스 쓸 수 있나"에
답하지 못했다. 그런데 데이터는 **있었다** — `output/aws-regions.json`에
`ap-northeast-2`의 EC2 인스턴스 타입 780개가 그대로 담겨 있다. 없던 것은 질문의
`서울`을 색인 키로 바꾸는 길뿐이었다. 이 프로젝트에서 반복해 확인한 실패 모양이다 —
**데이터는 있는데 에이전트가 못 닿는다.**

## 서울은 프로바이더마다 다르다

처음엔 botocore로 붙였는데 그건 **AWS만** 준다. 그래서 "서울에서 GCP 스팟 얼마?"에
`ap-northeast-2`(AWS)를 주고, GCP 서울은 모델의 기억에 맡겨야 했다. 실제로 모델이
`asia-north`**h**`east3`로 오타를 냈고 그게 틀린 답으로 이어졌다(실측).

지금은 cb-tumblebug `cloudinfo.yaml`에서 **AWS·Azure·GCP만** 보존한다:

    aws    ap-northeast-2
    azure  koreacentral·south
    gcp    asia-northeast3

미러(costkb·perfkb)와 **같은 저장소**라 리전 코드가 정확히 맞는다(조인 95%).

## 한국어 별칭은 리전 코드가 아니라 영어 낱말에 붙인다

    "서울" → ("Seoul", "Korea") → (원본 이름에서 찾기) → 프로바이더별 코드

`"서울": "ap-northeast-2"`로 적는 편이 짧지만, 그러면 **리전 코드가 우리 표에
박힌다.** 프로바이더가 리전을 늘리거나 이름을 바꾸면 표가 조용히 거짓이 된다.
낱말에 붙여 두면 코드는 늘 원본에서 온다 — 우리가 더하는 것은 번역뿐이다.

낱말을 **여럿** 두는 이유는 Azure다. Azure 서울은 이름이 `Korea Central`이라
`Seoul`이 안 들어 있다. `Korea`까지 봐야 잡히는데, 그러면 대전(openstack
`Korea Daejeon`)도 함께 잡힌다. **걸러내지 않고 원본 이름과 함께 보여준다** —
고르는 것은 부르는 쪽 몫이고, 우리가 고르면 그게 우리 값이 된다.

`matched_by`로 무엇 덕분에 맞았는지 함께 돌려준다. `code`·`name`은 원본이 말한
것이고 `alias`는 **우리가 더한 번역**이라, 답변에서 둘을 같은 무게로 말하면 안 된다.

## 방위 이름은 도시로 매핑하지 않는다

Azure는 리전 일부를 방위로 적는다 — `Southeast Asia`(실제로는 싱가포르),
`East US`(버지니아), `East Asia`(홍콩). 그래서 "싱가포르"로 물으면 Azure에서
**0건**이 나온다(실측). 이걸 채우려면 "Southeast Asia는 싱가포르다"를 우리가
적어 넣어야 하는데, 그건 **원본이 말한 것이 아니라 우리 지식**이다. 번역과 짐작의
경계가 여기다 — `서울`→`Seoul`은 같은 것을 다른 말로 적은 것이지만,
`Southeast Asia`→`싱가포르`는 새 사실을 주장하는 것이다. 그래서 넣지 않는다.

못 찾았을 때 "그런 리전이 없다"가 아니라 "우리가 못 알아들었다"고 답하는 이유도
같다 — 실제로는 있는데 이름이 달라서 못 찾은 경우가 이 부류다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.cloudkb.kbcommon.artifact import load_json, resolve

SUPPORTED_PROVIDERS = frozenset({"aws", "azure", "gcp"})

ARTIFACT = "cloud-regions.json"

#: 한국어 낱말 → 원본 표시 이름에 들어 있는 영어 낱말들(**어느 하나라도** 맞으면 잡는다).
#: 리전 코드를 여기 적지 않는 이유는 위 docstring 참조.
KOREAN_ALIASES: dict[str, tuple[str, ...]] = {
    # 한국 — 프로바이더마다 이름이 달라 낱말이 여럿이다.
    "서울": ("Seoul", "Korea"),
    "한국": ("Korea", "Seoul"),
    "판교": ("Pangyo",),
    "평촌": ("Pyeongchon",),
    "대전": ("Daejeon",),
    # 아시아·태평양
    # **도시 별칭에 나라 이름도 넣는다.** Azure만 도시명을 안 쓰기 때문이다 —
    # 도쿄가 `Japan East`, 런던이 `UK South`다. 나라를 넣으면 같은 나라의 다른
    # 도시도 함께 잡히지만, 원본 이름을 나란히 보여주므로 사용자가 구분한다.
    "도쿄": ("Tokyo", "Japan"),
    "오사카": ("Osaka", "Japan"),
    "일본": ("Japan", "Tokyo", "Osaka"),
    "타이베이": ("Taipei", "Taiwan"),
    "대만": ("Taiwan", "Taipei", "Changhua"),
    "홍콩": ("Hong Kong",),
    "싱가포르": ("Singapore",),
    "시드니": ("Sydney", "Australia"),
    "멜버른": ("Melbourne",),
    "호주": ("Australia", "Sydney", "Melbourne"),
    "자카르타": ("Jakarta", "Indonesia"),
    "인도네시아": ("Indonesia", "Jakarta"),
    "뭄바이": ("Mumbai", "India"),
    "하이데라바드": ("Hyderabad",),
    "델리": ("Delhi",),
    "인도": ("India", "Mumbai", "Delhi"),
    "말레이시아": ("Malaysia", "Kuala Lumpur"),
    "뉴질랜드": ("New Zealand", "Auckland"),
    "태국": ("Thailand", "Bangkok"),
    "필리핀": ("Philippines", "Manila"),
    "중국": ("China", "Beijing", "Shanghai"),
    "베이징": ("Beijing", "North China"),
    "상하이": ("Shanghai", "East China"),
    "선전": ("Shenzhen",),
    "광저우": ("Guangzhou",),
    # 유럽
    "프랑크푸르트": ("Frankfurt", "Germany"),
    "베를린": ("Berlin",),
    "독일": ("Germany", "Frankfurt", "Berlin"),
    "취리히": ("Zurich", "Switzerland"),
    "스위스": ("Switzerland", "Zurich"),
    "스톡홀름": ("Stockholm", "Sweden"),
    "스웨덴": ("Sweden", "Stockholm"),
    "밀라노": ("Milan", "Lombardy", "Italy"),
    "이탈리아": ("Italy", "Milan", "Lombardy"),
    "마드리드": ("Madrid", "Spain"),
    "스페인": ("Spain", "Madrid"),
    "아일랜드": ("Ireland", "Dublin"),
    "런던": ("London", "UK"),
    "영국": ("UK", "United Kingdom", "London"),
    "파리": ("Paris", "France"),
    "프랑스": ("France", "Paris"),
    "네덜란드": ("Netherlands", "Amsterdam", "Eemshaven"),
    "폴란드": ("Poland", "Warsaw"),
    "노르웨이": ("Norway", "Oslo"),
    "핀란드": ("Finland", "Hamina"),
    "오스트리아": ("Austria", "Vienna"),
    # 미주
    "버지니아": ("Virginia",),
    "오하이오": ("Ohio", "Columbus"),
    "캘리포니아": ("California", "Silicon Valley", "Los Angeles"),
    "오리건": ("Oregon",),
    "텍사스": ("Texas", "Dallas"),
    "토론토": ("Toronto", "Canada"),
    "캐나다": ("Canada", "Toronto", "Montreal"),
    "멕시코": ("Mexico",),
    "상파울루": ("Sao Paulo", "São Paulo", "Brazil"),
    "브라질": ("Brazil", "Sao Paulo"),
    "칠레": ("Chile", "Santiago"),
    # 그 밖
    "케이프타운": ("Cape Town",),
    "요하네스버그": ("Johannesburg",),
    "남아프리카": ("South Africa", "Cape Town", "Johannesburg"),
    "이스라엘": ("Israel", "Tel Aviv"),
    "바레인": ("Bahrain",),
    "아랍에미리트": ("UAE", "Dubai", "Abu Dhabi"),
    "두바이": ("Dubai", "UAE"),
    "사우디": ("Saudi", "Dammam"),
    "카타르": ("Qatar", "Doha"),
    # 넓은 말 — 여러 리전에 걸린다. 걸러내지 않고 여러 건을 그대로 돌려준다.
    "미국": ("US", "United States", "Virginia", "Ohio", "California", "Oregon"),
    "유럽": ("Europe", "EU"),
    "아시아": ("Asia",),
    "중동": ("Middle East",),
    "남미": ("South America", "Brazil", "Chile"),
    "아프리카": ("Africa",),
}


@dataclass(frozen=True, slots=True)
class RegionMatch:
    """리전 하나와, 무엇 덕분에 찾았는지."""

    provider: str
    code: str
    """**원본 표기 그대로.** kt·ncp·nhn은 대문자다(`KR1`). 조인에 쓸 때는
    `code.lower()`를 쓴다 — 미러는 소문자로 적는다."""

    name: str
    matched_by: str
    """`code` | `name` | `alias`.

    앞의 둘은 **원본이 말한 것**이고 `alias`는 우리가 더한 한국어 번역이다.
    """

    latitude: float | None = None
    longitude: float | None = None
    zones: tuple[str, ...] = ()


@lru_cache(maxsize=4)
def _catalog(output_dir: str | None) -> tuple[RegionMatch, ...]:
    """산출물에서 프로바이더별 리전을 읽는다. 없으면 빈 목록."""
    path = resolve(Path(output_dir) if output_dir else Path("output"), ARTIFACT)
    if path is None:
        return ()
    try:
        data = load_json(path)
    except Exception:
        # 산출물이 깨져도 리전 해석 하나 때문에 질의 전체가 죽으면 안 된다.
        return ()
    out = []
    for provider, body in (data.get("providers") or {}).items():
        if provider not in SUPPORTED_PROVIDERS:
            continue
        for region in (body.get("regions") or {}).values():
            out.append(
                RegionMatch(
                    provider=provider,
                    code=region.get("code") or "",
                    name=region.get("name") or region.get("code") or "",
                    matched_by="code",
                    latitude=region.get("latitude"),
                    longitude=region.get("longitude"),
                    zones=tuple(region.get("zones") or ()),
                )
            )
    return tuple(sorted(out, key=lambda r: (r.provider, r.code.lower())))


def catalog(
    *, provider: str | None = None, output_dir: str | None = None
) -> tuple[RegionMatch, ...]:
    """알려진 리전 전체 (프로바이더로 좁힐 수 있다)."""
    rows = _catalog(output_dir)
    if provider:
        wanted = provider.strip().lower()
        rows = tuple(r for r in rows if r.provider == wanted)
    return rows


def providers(*, output_dir: str | None = None) -> tuple[str, ...]:
    """리전 정보를 가진 프로바이더 목록."""
    return tuple(sorted({r.provider for r in _catalog(output_dir)}))


def name_of(
    code: str, *, provider: str | None = None, output_dir: str | None = None
) -> str | None:
    """`ap-northeast-2` → `South Korea (Seoul)`. 모르면 None.

    프로바이더를 안 주면 **아무 프로바이더의** 같은 코드나 맞는다 — `ap-northeast-2`는
    aws와 alibaba 둘 다에 있다. 정확히 알고 싶으면 provider를 준다.
    """
    lowered = code.strip().lower()
    for region in catalog(provider=provider, output_dir=output_dir):
        if region.code.lower() == lowered:
            return region.name
    return None


def resolve_region(
    query: str, *, provider: str | None = None, output_dir: str | None = None
) -> list[RegionMatch]:
    """사람이 쓴 말에서 리전을 찾는다. 코드 → 표시 이름 → 한국어 별칭 순.

    **여러 건이 나올 수 있다.** "서울"은 프로바이더 10곳에 걸리고 "미국"은 수십 곳에
    걸린다. 하나로 좁혀 돌려주면 우리가 고른 것이 사용자가 뜻한 것인 양 보이므로,
    좁히는 일은 부르는 쪽에 맡긴다. `provider`를 주면 그 프로바이더 안에서만 찾는다.

    못 찾으면 빈 목록이다. 이건 "그런 리전이 없다"가 아니라 **"우리가 못 알아들었다"**
    이므로, 부르는 쪽은 없다고 단정하지 말고 아는 리전을 보여 주는 편이 낫다.
    """
    text = query.strip()
    if not text:
        return []
    regions = catalog(provider=provider, output_dir=output_dir)
    lowered = " ".join(text.lower().split())
    # 사람과 LLM은 표시 이름에 흔히 일반 접미사("Seoul region")를 붙인다.
    # 카탈로그 이름은 "Asia Pacific (Seoul)"처럼 그 접미사를 포함하지 않으므로,
    # provider가 정해진 뒤에도 정확한 한 후보를 놓치고 필수 입력을 다시 묻게 됐다.
    # 리전 코드를 추측하지 않고 검색어의 비식별 접미사만 제거한다.
    name_queries = [lowered]
    for suffix in (" cloud region", " region"):
        if lowered.endswith(suffix):
            normalized = lowered[: -len(suffix)].strip(" ,()[]")
            if normalized:
                name_queries.append(normalized)

    exact = [r for r in regions if r.code.lower() == lowered]
    if exact:
        return exact

    # 표시명이 정확히 맞으면 부분 이름보다 먼저 끝낸다. 예를 들어 Azure의
    # ``East US``는 ``East US 2``의 부분 문자열이기도 하지만, 원본 catalog의
    # 정확한 표시명은 전자 하나다. 같은 provider에 동명 리전이 있으면 모두 돌려
    # 호출자가 고르게 하며 여기서 임의 선택하지 않는다.
    exact_names = [
        region
        for region in regions
        if any(
            " ".join(region.name.casefold().split()) == name_query
            for name_query in name_queries
        )
    ]
    if exact_names:
        return [_retag(region, "name") for region in exact_names]

    # UI 선택지는 ``Display Name (canonical-code)`` 형식으로 돌아온다. 앞에
    # provider를 붙이거나 끝에 ``Region``을 붙인 표현도 흔하므로, catalog code가
    # 존재하는지만 보지 않고 정규화한 표시명이 **유일하게 같은 row**를 가리킬 때만
    # 받아들인다. 이로써 ``East US (eastus2)``를 East US로 조용히 바꾸지 않는다.
    # 표시명 자체의 괄호(South Korea (Seoul))는 마지막 괄호 쌍만 code로 읽는다.
    formatted = re.fullmatch(
        r"(?P<name>.+?)\s*\(\s*(?P<code>[^()]+?)\s*\)\s*", text
    )
    if formatted:
        display_name = _normalized_formatted_display_name(
            formatted.group("name"), provider=provider
        )
        code = formatted.group("code").strip().casefold()
        code_matches = [region for region in regions if region.code.casefold() == code]
        if code_matches and display_name:
            described = resolve_region(
                display_name, provider=provider, output_dir=output_dir
            )
            described_keys = {(region.provider, region.code) for region in described}
            if len(described_keys) == 1:
                matched = [
                    region
                    for region in code_matches
                    if (region.provider, region.code) in described_keys
                ]
                if matched:
                    return [_retag(region, "name") for region in matched]
        return []

    by_name = [
        r
        for r in regions
        if any(query in r.name.lower() for query in name_queries)
    ]
    if by_name:
        return [_retag(r, "name") for r in by_name]

    # 한국어는 조사가 붙어도 낱말이 그대로 들어 있으므로 부분 문자열로 찾는다
    # ("서울리전", "서울에서" 모두 걸려야 한다).
    found: list[RegionMatch] = []
    seen: set[tuple[str, str]] = set()
    for korean, english_words in KOREAN_ALIASES.items():
        if korean not in text:
            continue
        for word in english_words:
            needle = word.lower()
            for r in regions:
                key = (r.provider, r.code)
                if key in seen or needle not in r.name.lower():
                    continue
                seen.add(key)
                found.append(_retag(r, "alias"))
    return found


def _normalized_formatted_display_name(value: str, *, provider: str | None) -> str:
    """Remove only generic UI decoration from a formatted region label."""
    normalized = " ".join(value.casefold().split())
    if provider:
        prefix = provider.strip().casefold()
        if prefix and normalized.startswith(f"{prefix} "):
            normalized = normalized[len(prefix) :].strip()
    for suffix in (" cloud region", " region"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].strip(" ,()[]")
            break
    return normalized


def _retag(region: RegionMatch, how: str) -> RegionMatch:
    return RegionMatch(
        provider=region.provider,
        code=region.code,
        name=region.name,
        matched_by=how,
        latitude=region.latitude,
        longitude=region.longitude,
        zones=region.zones,
    )


_MISSING = "No region artifact. Build it with `python -m envkb build-regions`."


def region_lookup(
    query: str,
    provider: str | None = None,
    *,
    output_dir: str | None = None,
) -> str:
    """사람이 쓴 말('서울')을 세 CSP의 리전 코드로 옮긴다.

    가격·성능 카탈로그가 함께 쓰는 공용 리전 해석기다.

    이게 없어서 실측에서 "서울 리전에서 GPU 인스턴스"에 답하지 못했다. 데이터는
    `aws-regions.json`에 있었고 `ap-northeast-2`라는 키만 못 만들고 있었다.

    **서울은 프로바이더마다 다르다** — aws `ap-northeast-2`, gcp `asia-northeast3`,
    azure `koreacentral`. `provider`를 주면 그 안에서만 찾는다.
    """
    found = resolve_region(query, provider=provider, output_dir=output_dir)
    if not found:
        known = catalog(provider=provider, output_dir=output_dir)
        if not known:
            if provider and catalog(output_dir=output_dir):
                have = ", ".join(providers(output_dir=output_dir))
                return (
                    f"No region data for provider '{provider}'. "
                    f"Providers we know: {have}"
                )
            return _MISSING
        scope = f"{provider} " if provider else ""
        return (
            f"Could not make out any {scope}region in '{query}' — that does not "
            f"mean no such region exists, it means **we did not understand it**.\n"
            "  Ask again with a region code (`ap-northeast-2`) or the English "
            "name (`Seoul`).\n"
            f"  A few of the {len(known)} {scope}regions we know: "
            + ", ".join(f"{r.code}({r.name})" for r in known[:5])
        )

    scope = f" ({provider})" if provider else ""
    lines = [f"'{query}'{scope} → {len(found)} regions"]
    for region in found[:12]:
        how = {
            "code": "region code",
            "name": "the source's display name",
            "alias": "a Korean alias we added",
        }[region.matched_by]
        lines.append(
            f"  - [{region.provider}] {region.code} — {region.name} ({how})"
        )
    if len(found) > 12:
        lines.append(f"  … and {len(found) - 12} more")
    if len({r.provider for r in found}) > 1:
        lines.append(
            "  → **Region codes differ by provider.** Pick a provider and use "
            "its code — passing another provider's code finds nothing even when "
            "the data is there."
        )
    # **이 꼬리말은 조건부가 아니다.** 문장이 언제나 참이기 때문이다 — 이 도구는 리전
    # 사실만 돌려주지 규제 준수를 판정한 적이 없다. 열쇠말로 "규제 질문일 때만" 붙이면
    # 손으로 만든 금지어 목록이 되고, 그건 다음 표현에서 새어 나간다(19장).
    #
    # 실측(2026-07-28)에서 뚫린 자리다. "데이터가 한국에 있어야 하는데 ap-northeast-2면
    # 되나?"에 모델이 이 도구를 부른 뒤 **"Yes … so data stored there stays within
    # Korea"**라고 답했다. 도구는 리전 사실만 줬는데 모델이 준수 판정으로 결론냈다.
    # `appkb`의 `dataResidency` 계약이 "판정할 수 없다(법적 판단)"고 못 박아 둔 것을
    # 낱개 질의 경로가 지키지 않고 있었다.
    #
    # 주장 대조기로는 못 잡는다 — 숫자·식별자가 아니라 **문장의 뜻**이 넘어간 것이라
    # 14장이 밝힌 대조기의 한계 그대로다. 그래서 도구 출력에 싣는다: 실측에서 도구
    # 꼬리말은 최종 답변까지 살아남는다(3-11의 낡음 고지가 3/3).
    lines.append(
        "  ※ This is a **region fact, not a compliance judgment**. Whether a "
        "deployment satisfies data-residency or regulatory obligations is a legal "
        "determination this knowledge base does not make — compare the region "
        "against the requirement and say the verdict cannot be given here."
    )
    return "\n".join(lines)
