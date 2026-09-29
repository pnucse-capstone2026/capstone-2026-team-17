"""에이전트가 공유할 수 있는 기반 모듈.

이 패키지의 모듈은 요구사항 에이전트 내부 상태를 모른다. 여러 단계가 함께 쓰는 기능은
소유권에 따라 `app.metrics`, `app.validation`, `app.cloudkb` 같은 공개 경계로 승격한다.
그래서 이 패키지 안에서는 `app.requirements.*`를 import하지 않으며,
`tests/test_common_isolation.py`가 그 규칙을 지킨다.
"""
