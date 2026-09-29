"""speckb — AWS·Azure·GCP가 직접 발행한 VM 카탈로그 원본 수집.

## 무엇을 담는가

세 CSP의 공식 엔드포인트에서 받은 **응답 본문 그대로**를 `raw/` 아래에 둔다.
정규화도, 필드 이름 통일도, CSP 간 병합도 하지 않는다. 어떤 필드가 어디서
왔는지는 `README.md`에 정리돼 있다.

## 왜 다른 KB와 달리 독립인가

`costkb`는 cb-tumblebug의 PostgreSQL 덤프를, `perfkb`는 Cyclenerd·vantage-sh
GitHub 저장소를 파싱한다. 둘 다 CSP가 아니라 **제3자가 이미 가공한 값**이고,
그 과정에서 상류가 실제로 무엇을 주는지는 저장소에 남지 않는다.

speckb는 그 공백을 메우려고 만들었으므로, 저장소에 이미 있는 값이 수집 경로에
단 한 건도 섞이면 안 된다. 그래서 `kbcommon`·`costkb`·`perfkb`·`depkb`를
import하지 않고, `data/cloud-regions.json.gz` 같은 기존 아티팩트도 읽지 않는다.
리전 목록·리전 표시명처럼 있으면 편한 값들조차 전부 CSP 응답에서 다시 뽑는다.

그 대가로 `kbcommon/fetch.py`와 겹치는 다운로드·provenance 로직이 `_http.py`에
중복돼 있다. `kbcommon/__init__.py`가 정한 "두 KB가 쓰면 kbcommon으로" 규칙과
어긋나 보이지만, 이건 실수가 아니라 위 제약을 지키려고 택한 것이다.

## 실행

    python -m app.cloudkb.speckb.fetch_aws
    python -m app.cloudkb.speckb.fetch_azure
    python -m app.cloudkb.speckb.fetch_gcp

이미 받은 파일은 건너뛴다. 다시 받으려면 `--refresh`.
"""
