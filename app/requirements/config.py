"""요구사항 단계 전용 설정. 공통 LLM 접속값은 `app.config`가 루트 `.env`에서 읽는다."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 같은 입력에서 0.2와 0.6을 비교했을 때 0.6이 복합 요구사항을 더 잘 분리했고
    # 호출 실패나 구조화 fallback은 없었다. 전역 LLM 설정과 같은 기본값을 사용한다.
    temperature: float = 0.6
    requirements_reasoning_effort: str = "medium"
    requirements_max_completion_tokens: int = 8192
    # 같은 입력에 같은 표본을 **요청**한다. None이면 파라미터를 보내지 않는다.
    #
    # ⚠ 이 둘로 출력이 고정되지는 않는다. 이유는 서버가 seed를 무시할 수 있다는 정도가
    # 아니라 구조적이다(MoE 라우팅이 배치 구성에 좌우된다) — `runtime/structured_llm.py`의
    # "재현성 — 여기서 얻을 수 없는 것"에 한 곳에만 적어 둔다.
    seed: int | None = 42
    # few-shot 예시 코사인 샘플링용 NIM 임베딩 모델(OpenAI 호환 embeddings 엔드포인트).
    # base_url/api_key 는 위 자격증명을 재사용한다. example_sampler 의 backend="nim" 참고.
    embed_model: str = "nvidia/llama-nemotron-embed-1b-v2"
    # step1 구체화 프롬프트의 few-shot 예시 추출 방식.
    #  - "random"  : 무작위(baseline, 오프라인)
    #  - "mmr+nim" : NIM 임베딩으로 요구사항과 관련되면서 다양한 예시 선별(네트워크 필요)
    # PURE의 FR/NFR 문장은 CNA 요구사항 코퍼스가 아니다. 비교실험은 명시적으로 켤 수
    # 있지만, 실제 요구사항 합성 경로가 이 자료에 암묵적으로 의존해서는 안 된다.
    example_sampling_method: str = "none"

    # --- 요구사항 분석 에이전트 설정 ---
    # materials의 파인튜닝 BERT(FR/NFR) 모델 디렉토리. (0=NFR, 1=FR)
    bert_model_path: str = "materials/BERT_FR_NFR_Classifier/bert_model"
    # BERT 분류기 사용 여부. False면 torch 로드를 건너뛴다. 이 모드에서 원문 요구사항을
    # 분류할 수는 없으며, 실행 입력이 이미 FR/NFR로 분류된 체크포인트여야 한다.
    enable_bert_verify: bool = True
    # (2~4단계는 항상 실행한다. 예전 enable_pipeline_stubs 게이트는 제거됨.)
    # step3 명세 생성·semantic 커버리지 채점의 동시 LLM 호출 상한(UC별 병렬).
    # hosted NIM(integrate.api)에서 8 동시까지 429 없이 견딤을 실측했다.
    spec_concurrency: int = 8
    # 의미 수리는 숫자 예산으로 끊지 않는다. 후보·finding·전략 digest 이력이 같은 실패의
    # 반복을 막고, 미사용 전략이 없을 때만 명시적인 stalled 상태로 전환한다.
    # 대화형 모드 스위치. True면 모든 interrupt 기반 상호작용을 켠다:
    #  - step1 clarify(요구사항이 추상적일 때 질문) 루프
    #  - 각 스텝(1~4) 말미의 피드백 게이트(피드백 주면 재생성·루프, 빈 값이면 다음 단계)
    # False면 비대화형(배치/자율) — 질문도 게이트도 없이 파이프라인을 끝까지 진행한다.
    enable_feedback_gates: bool = False
    # 정적 체크에 더해 LLM 의미 검증(hidden branching·scope creep 등)을 병합할지.
    enable_semantic_validator: bool = True
    # 의미 판정을 몇 번 물어 **과반으로 확정할지**. 1이면 한 번 묻는다(다수결 없음).
    #
    # **왜 필요한가는 측정 결과다**(2026-07-26, toystore 명세 11개 × 5회): 같은 명세를 같은
    # 검증자에게 5번 물었을 때 (명세×규칙) 판정 24건 중 **안정된 것이 4건**이었다
    # (흔들림 83%). 두 규칙만의 문제가 아니라 이 층 전체가 그랬다.
    #
    # 판정이 동전 던지기면 그 위에 쌓은 모든 수가 무의미하다 — 반성 루프는 깜빡이는 결함을
    # 쫓고(`no_improvement`가 11개 중 5~7개였다), 실행 비교는 같은 동전을 다시 던지는 일이
    # 된다. 비용은 검증 호출 × 이 값이다.
    validator_votes: int = 1
    # 의미 판정을 **규칙마다 따로** 물을지. False면 한 번에 다 묻는다.
    #
    # 다수결(위)이 흔들림을 90%→75%로만 줄인 뒤 남은 갈래다. 다수결은 한 번 판정의 확률이
    # 0.5에서 떨어져 있을 때만 날카로워지는데, 규칙 6개를 한 응답에 판정하게 하는 지금
    # 구조에서는 그 확률 자체가 0.5 근처다. 과제를 쪼개면 달라지는지 보려는 것이다.
    # 비용은 검증 호출 × 규칙 수(대신 응답이 짧아 호출당 시간은 줄어든다).
    validator_per_rule: bool = False
    # 지난 실행에서 관찰된 위반을 생성 프롬프트에 실을지(`agent/playbook.py`).
    #
    # **기본값이 꺼짐인 이유는 아직 안 재서다.** 이 저장소에서 켠 채로 두고 재지 않은
    # 기능이 세 번 값을 못 냈다(되돌아가기·다수결·규칙별 호출). 켜고 끄고 `evaluation
    # score`/`diff`로 비교한 뒤에 기본값을 정한다.
    #
    # 위험은 명확하다: 배우는 재료가 우리 검증자의 판정인데 그 판정이 도메인에 따라
    # 78~90% 흔들린다(§7~§9). 그래서 플레이북 쪽에서 출처별 문턱을 두지만, 문턱이
    # 충분한지는 측정 전에는 모른다.
    playbook_enabled: bool = False
    # 플레이북 파일. 기본 경로는 gitignore되는 `artifacts/` 아래다 — 배운 내용은
    # 실행 산출물이지 소스가 아니고, 커밋할지는 사람이 정한다.
    playbook_path: str = "artifacts/playbook.json"
    # 클라우드 네이티브 관심사 링크에 **LLM 층을 쓸지**(`knowledge/concerns.py`).
    #
    # 결정론 층(열쇠말)은 이 값과 무관하게 항상 돈다 — 값싸고 흔들리지 않는다. 이 스위치는
    # 열쇠말이 못 보는 관심사(예: `cn.disposability`는 특정 단어로 쓰이지 않는다)를 LLM에게
    # 물을지만 정한다.
    #
    # **기본값이 꺼짐인 이유는 아직 안 재서다.** 켠 채로 두고 재지 않은 기능이 이 저장소에서
    # 세 번 값을 못 냈다(되돌아가기·다수결·규칙별 호출). 끈 실행에서 관심사 상태가
    # `unjudged`로 남는 것은 **정확한 기록**이지 결함이 아니다 — "안 다뤄졌다"고 말하지
    # 않는다는 뜻이다.
    concern_linker_llm: bool = False

    # 제약 구조화 **에이전트**(`resources/service.py`)를 돌릴지.
    #
    # 끄면 이 단계는 아무것도 읽지 않고, 필수 칸 전부가 되묻기 질문으로 나간다. 그건
    # 결함이 아니라 **정확한 기록**이다 — 자연어를 읽는 수단이 없는데 읽은 척하지 않는다.
    # (`resource_intake.degraded`에 이유가 남는다.)
    #
    # `concern_linker_llm`과 달리 **기본이 켜짐**인 근거: 저쪽은 없는 것을 드러내는
    # 판정이라 한 번 물어 빈 답이 나오면 "안 다뤄졌다"인지 "놓쳤다"인지 구별할 수 없어
    # 과반이 필요했다. 이쪽은 값마다 **본 자리를 인용하게 하고 실재하는지 대조**하고
    # (`_ground`), 계약·카탈로그가 다시 검증하며, 모호하면 되묻는다 — 틀린 값이 조용히
    # 통과할 문이 없다.
    resource_agent_llm: bool = True
    # 에이전트가 도구를 부르며 돌 수 있는 최대 턴 수.
    #
    # 상한이 하는 일은 **폭주를 막는 것뿐**이다. 여기 닿아 멈춘 실행은 못 채운 칸을 그대로
    # 두고, 그 칸들은 계약의 이유를 달고 되묻기 질문으로 나간다 — 반쯤 채운 사양이 새어
    # 나가지 않는다. 12는 "값 4~6개 기록 + 리전 해석 + 계약 조회 + 마무리"에 여유를 둔 값.
    resource_agent_max_turns: int = 12
    # Interactive analysis uses one response so that research-only stability measurement
    # does not delay the user workflow.  Confirmatory commands pass their sample count
    # explicitly instead of changing this product default.
    capability_samples: int = 1
    # 관심사 링크를 몇 번 물어 **과반으로 확정할지**.
    #
    # 3인 이유는 `validator_votes`(기본 1)와 다르다. 링크는 **없는 것을 드러내는** 판정이라
    # 한 번 안 걸린 것과 잡음을 구별할 수 없다 — 한 번 물어 빈 답이 나오면 그것이 "안
    # 다뤄졌다"인지 "그 실행에서 놓쳤다"인지 알 길이 없다. 검증자 판정이 도메인에 따라
    # 78~90% 흔들린다는 측정(§7~§9)이 그 문턱의 근거이고, 플레이북이 검증자 반례에 3실행
    # 문턱을 둔 것과 같은 이유다.
    concern_linker_votes: int = 3
    # 한 번에 **몇 개씩** 물을지. 0이면 전부 한 프롬프트에 넣는다(지금 기본값).
    #
    # 조건 변수로 둔 이유: 관심사가 29개가 되면서 링크 프롬프트가 5,209자가 됐는데, §9는
    # **규칙 6개**를 한 프롬프트에 넣었을 때 안정 판정이 0이 된 것을 이미 쟀다. 다른
    # 과제라 수치를 옮길 수는 없지만 방향은 같은 쪽을 가리킨다.
    #
    # 불리언이 아니라 정수인 이유는 1이 `validator_per_rule`과 같은 갈래(하나씩 묻기)가
    # 되어 한 축으로 이어지기 때문이다. 비용은 호출 수 = ceil(29/chunk)다.
    concern_linker_chunk: int = 0
    # 서빙 경로에서 대화형 세션(체크포인트+게이트 모드)을 MySQL에 저장할지.
    # True면 서버가 재시작해도 진행 중인 분석이 이어지고, 파드를 여럿 띄울 수 있다.
    # server.py가 기동 시 init_db()를 부르므로 서빙 경로는 어차피 MySQL을 요구한다.
    # CLI·배치 러너는 이 값과 무관하게 항상 메모리를 쓴다(프로세스와 수명이 같다).
    enable_session_persistence: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
