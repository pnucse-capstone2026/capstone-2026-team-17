"""Cloud input, capability와 ``RESOURCE_SPEC`` 단계를 소유하는 bounded context다.

각 public 서비스는 해당 하위 모듈에서 명시적으로 import한다. package import가 runtime,
LLM 또는 stage registry를 eager import하지 않도록 여기서는 re-export를 만들지 않는다.
"""
