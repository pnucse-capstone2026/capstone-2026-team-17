## 1. 프로젝트 배경

### 1.1. 국내외 시장 현황 및 문제점

클라우드 시장이 성장하면서 클라우드 네이티브 애플리케이션의 개발 수요도 늘고 있다. 이러한 애플리케이션은 서비스 기능뿐 아니라 클라우드 자원의 구성과 배포까지 고려해야 하므로 개발 과정이 복잡하다. 이에 개발 부담을 줄이고 효율을 높이기 위해 AI를 활용하려는 시도가 확대되고 있다. 그러나 클라우드 네이티브 애플리케이션 개발에 AI 에이전트를 도입하려면 다음과 같은 문제를 해결해야 한다.
  1. LLM 환각 현상에 따른 산출물 오류: AI가 요구사항에 없는 기능을 추가하거나 앞서 작성한 내용과 모순되는 설계와 코드를 만들 수 있다. 결과물이 자연스럽게 보이더라도 실제 사용자 의도와 일치하는지 확인해야 한다.
  2. AI 에이전트간의 결함 추적의 어려움: AI 에이전트가 목표를 수행하기 위해 사용자의 의도를 전달하는 과정에서, 사용자의 목표가 부정확하게 작성되거나 의도와 다르게 파악된 경우, 이로 인해 에이전트들을 거치면서 의도와는 다른 최종 산출물이 생성될 수 있다.
  3. 클라우드 자원 구성의 복잡성: 클라우드 네이티브 애플리케이션이 동작하는 클라우드 환경은 다양한 클라우드 리소스를 연결 · 참조하여 형성된다. 클라우드 리소스는 생성 시 선행 배포가 필요한 리소스가 존재하며, 여러 CSP(Cloud Service Provider)를 동시에 제어하는 환경에서는 CSP 별로 다른 리소스 관리 방법이 요구된다. 즉, CSP 마다 생성하고자 하는 클라우드 리소스를 배포하고 클라우드 네이티브 애플리케이션에 맞춰 클라우드 환경을 구성해야 하는 문제가 있다.

### 1.2. 필요성과 기대효과

클라우드 네이티브 애플리케이션 개발에 AI 에이전트를 활용하려면, 각 에이전트가 결과물을 생성하는 것만으로는 부족하다. 요구사항에 없는 내용이 설계에 포함되면 이후 코드에도 반영될 수 있고, 클라우드 자원 간 의존관계를 놓치면 애플리케이션을 배포하지 못할 수 있다. 따라서 사용자 의도가 개발 단계마다 일관되게 전달되는지 확인하고, 생성된 산출물과 클라우드 배포 구성을 함께 검토할 수 있는 지원 체계가 필요하다.
이에 본 연구는 정제된 요구사항과 유스케이스 명세, 클래스·시퀀스·ER 다이어그램, API 명세와 배포 다이어그램을 단계별로 제시한다. 사용자는 이 자료에서 빠진 기능이나 잘못된 관계를 찾아 피드백을 보낼 수 있다. 시스템은 클라우드 자원의 의존관계와 사양을 배포 설계에 반영하고, 생성된 코드와 배포 파일을 검사한다.

## 2. 개발 목표

### 2.1. 목표 및 세부 내용

본 연구는 멀티 AI 에이전트 기반으로 클라우드 네이티브 애플리케이션의 자연어 요구사항을 분석하고, UML 다이어그램으로 설계하며, IaC·소스 코드를 통해 시스템을 구현, 산출물 테스팅을 수행하는 기술을 연구하는 것이다.
  1. 멀티 AI 에이전트 구조 설계: 요구사항 분석, 시스템 설계, 구현과 테스트를 담당하는 에이전트를 구성한다. 요구사항 ID와 유스케이스 명세를 설계에 전달하고, 설계 결과를 코드 생성과 테스팅에 사용한다.
  2. 클라우드 네이티브 애플리케이션 제약사항 해소 방법: AWS·Azure·GCP 리소스 간 의존 관계, VM 사양, 비용과 성능 정보를 활용하여 사용자 요구조건에 맞는 클라우드 리소스 구성과 배포 방안을 제시한다.
  3. 멀티 AI 에이전트 간 산출물 연계 방안 구축: 사용자가 단계별 산출물을 검토하고 피드백을 제공할 수 있도록 하며, 피드백의 대상과 영향 범위에 따라 관련 산출물을 일관되게 수정한다.


### 2.2. 기존 서비스 대비 차별성

기존 AI 기반 개발 지원 방식에서는 에이전트가 만든 결과물이 다음 단계로 전달되는 동안 사용자 요구사항이 어떻게 반영됐는지 확인하기 어렵다. 산출물에 수정이 필요할 때도 관련 설계와 코드에 변경 사항을 일관되게 적용해야 한다는 과제가 남는다.
이에 제안하는 시스템은 요구사항마다 ID를 붙여 유스케이스와 연결하고, 유스케이스의 동작을 클래스·시퀀스·API·ERD 설계로 옮긴다. 사용자는 요구사항 목록과 유스케이스 명세, 각 설계 결과를 확인하고 피드백을 보낼 수 있다. 시스템은 피드백이 지목한 항목과 관련 유스케이스를 찾아 필요한 설계 결과를 다시 만든다.
또한 클라우드 배포 환경을 개발 과정에 함께 반영하여 클라우드 제공자별 자원 의존관계와 VM 사양·비용 조건을 고려해 배포 구성을 만들고, 생성된 코드와 배포 파일을 검사한다. 애플리케이션 기능뿐 아니라 이를 실행할 클라우드 환경까지 하나의 개발 흐름에서 다룰 수 있도록 설계했다.

### 2.3. 사회적 가치 도입 계획

본 연구는 클라우드 개발 경험이 적은 학생과 소규모 개발팀도 애플리케이션의 설계와 배포 과정에 직접 참여할 수 있도록 지원하고자 한다. 제안하는 시스템은 요구사항·유스케이스 목록, 설계 다이어그램과 API 명세, 구현 코드와 테스트 결과를 단계별로 보여준다. 사용자는 누락된 기능이나 잘못 연결된 항목에 대해 피드백을 전달할 수 있다.
클라우드 제공자마다 다른 자원 구성 방식과 자원 간 의존관계는 처음 배포를 시도하는 사람에게 어려울 수 있다. 제안하는 시스템은 이러한 관계를 배포 설계에 반영해 사용자가 구성 내용을 살펴볼 수 있도록 한다. 이를 통해 완성된 결과물을 받는 데 그치지 않고, 어떤 자원이 왜 필요한지 이해하며 배포 과정을 경험하도록 돕고자 한다.

## 3. 시스템 설계

### 3.1. 시스템 구성도

<img width="1914" height="1103" alt="image" src="https://github.com/user-attachments/assets/a0022c7c-3646-49a6-814a-fbe28c7f88f5" />

### 3.2. 사용 기술

| 분류 | 기술  |
|---|---|
| 프론트엔드 | SvelteKit, Svelte 5, TypeScript, Vite, Tailwind CSS, Monaco Editor |
| 백엔드 | Python, FastAPI, Uvicorn |
| ML/AI 프레임워크 | LangGraph, OpenHands SDK, PyTorch |
| 컨테이너 실행 환경 | Docker |
| 테스팅 프레임워크 | OpenTofu, Playwright, Trivy |
| 데이터베이스 | MySQL |


## 4. 개발 결과

### 4.1. 전체 시스템 흐름도

<img width="1914" height="1103" alt="image" src="https://github.com/user-attachments/assets/a0022c7c-3646-49a6-814a-fbe28c7f88f5" />

시스템은 요구사항 분석, 시스템 설계, 시스템 구현, 테스팅을 담당하는 네 AI 에이전트로 구성된다. 검은 화살표는 앞 단계에서 생성한 산출물을 다음 에이전트에 전달하는 흐름을 나타낸다. 사용자가 자연어 요구사항을 입력하면 요구사항 분석 에이전트가 기능·비기능 요구사항을 분류하고 유스케이스 명세와 다이어그램을 만든다. 클라우드 지식베이스를 활용해 배포에 필요한 자원 제약사항도 정리한다.

시스템 설계 에이전트는 요구사항과 유스케이스 명세에서 클래스·시퀀스·ER 다이어그램과 API 명세를 생성한다. 클라우드 자원 제약사항은 배포 다이어그램에 반영한다. 시스템 구현 에이전트는 이 설계에서 기본 코드를 만들고 OpenHands 코딩 에이전트로 기능을 구현한다. 소스 코드와 테스트를 작성한 뒤 Docker 설정, IaC 코드와 배포 스크립트를 생성한다.

테스팅 에이전트는 요구사항과 API 명세를 바탕으로 테스트 계획을 작성하고, 배포 파일의 정적 검사와 컨테이너에서 실행한 애플리케이션의 동적 검사를 수행한다. 발견한 구현 결함은 분홍색 화살표로 표시한 내부 피드백 경로를 따라 시스템 구현 에이전트에 전달되며, 수정한 파일은 다시 테스트한다. 초록색 화살표는 사용자 피드백 경로다. 사용자는 대화형 UI에서 단계별 명세·다이어그램·코드·테스트 결과를 확인하고 피드백을 전달한다.

### 4.2. 기능 설명 및 주요 기능 명세서

#### 4.2.1. 요구사항 분석

요구사항 분석 에이전트는 사용자가 자연어로 입력한 애플리케이션의 기능·운영 조건을 분석한다. LLM으로 모호한 문장을 구체화하고 BERT 분류기로 기능·비기능 요구사항을 구분한다. 이어 시스템과 상호작용하는 액터와 각 액터의 목표를 찾아 유스케이스를 도출한다.

각 유스케이스의 선행 조건, 기본 시나리오, 대안 및 예외 시나리오와 후행 조건을 명세한다. 액터의 일반화 관계와 유스케이스의 include·extend 관계도 다이어그램에 표시한다. 기능 요구사항이 빠지지 않았는지, 참조한 액터와 유스케이스가 존재하는지 검사한 뒤 유스케이스 명세 목록과 유스케이스 다이어그램을 제공한다.

<img width="1981" height="941" alt="image" src="https://github.com/user-attachments/assets/5436c08d-4c7c-4dee-878e-0cea264fdb6a" />


#### 4.2.2. 클라우드 자원 분석과 VM 후보 안내

사용자는 AWS·Azure·GCP 중 배포할 공급자와 리전, 최소 vCPU·메모리, 영구 저장 용량과 월 예산을 입력한다. 요구사항 분석 에이전트는 이 조건을 클라우드 자원 명세로 정리한다. 시스템은 클라우드 지식베이스의 의존관계·사양·가격 자료를 활용해 VM에 필요한 네트워크·서브넷·방화벽·공인 IP를 찾고, 지정한 리전에서 최소 vCPU와 메모리를 만족하는 VM 후보를 가격순으로 제시한다.

사용자는 후보별 사양과 가격을 확인해 VM과 개수를 선택한다. 시스템 설계 에이전트는 이 선택과 공급자·리전·저장 용량 조건을 사용해 자원의 배치와 연결 관계를 설계한다.

<img width="1983" height="939" alt="image" src="https://github.com/user-attachments/assets/513c0e52-660e-4bb6-ab47-bb4d1d9dd4d2" />


#### 4.2.3. 소프트웨어 및 배포 설계

시스템 설계 에이전트는 요구사항과 유스케이스 명세를 바탕으로 LLM을 활용해 클래스·필드·메서드를 도출한다. 클래스는 사용자 요청을 받는 Boundary, 기능을 처리하는 Control, 저장할 데이터를 나타내는 Entity로 구분하며, 속성·메서드와 클래스 사이의 관계를 클래스 다이어그램으로 나타낸다. 클래스 모델의 메서드 호출 관계를 변환해 유스케이스별 시퀀스 다이어그램을 만든다. 사용자 요청에 대응하는 동작에는 HTTP 메서드·경로·요청·응답을 정의하고 OpenAPI 명세로 변환한다.

시스템 설계 에이전트는 Entity의 속성·자료형·관계에서 테이블·컬럼·기본키·외래키를 도출해 ERD를 생성한다. 클라우드 자원 분석 결과와 애플리케이션 실행 조건은 배포 다이어그램에 반영해 VM 배치, 저장소·네트워크 연결과 자원 생성 순서를 나타낸다. 시퀀스에 사용된 클래스·메서드가 존재하는지, 호출과 반환이 맞는지도 검사한다.

<img width="1981" height="945" alt="image" src="https://github.com/user-attachments/assets/cf551b7b-aef4-48ad-80ad-65c18f1dd1a2" />


#### 4.2.4. 애플리케이션 및 배포 파일 생성

시스템 구현 에이전트는 유스케이스 명세와 클래스·시퀀스·ERD·OpenAPI·배포 설계를 입력으로 받는다. 먼저 클래스·메서드 선언과 API 요청·응답 형식이 포함된 기본 코드인 스켈레톤 코드를 생성한다. OpenHands 코딩 에이전트는 유스케이스 명세의 처리 단계와 시퀀스 다이어그램의 호출 순서에 따라 사용자 요청을 처리하고, 조건을 검사하며, 데이터를 조회·변경하는 소스 코드와 테스트를 작성한다.

클래스 모델과 ERD를 사용해 데이터를 저장하고 읽는 코드와 데이터베이스 초기화 SQL을 생성한다. 프론트엔드는 백엔드와 같은 OpenAPI 명세에서 만든 API 호출 코드를 사용한다. 이 호출 코드를 화면에 연결해 사용자 입력을 서버에 전달하고 응답 결과를 표시하도록 구현한다.

백엔드 테스트와 프론트엔드 빌드로 소스 코드를 검사한 뒤, 애플리케이션을 컨테이너로 빌드하고 실행할 Dockerfile·Docker Compose 설정을 만든다. 배포 설계에 정의된 클라우드 자원과 연결 관계는 Terraform 형식의 IaC 코드에 반영한다. 애플리케이션의 실행 포트와 저장소 설정을 VM 초기화 파일에 반영하고, 배포 계획 확인·적용·상태 조회를 위한 스크립트도 생성한다.

<img width="1980" height="940" alt="image" src="https://github.com/user-attachments/assets/d62872c1-56f3-4224-9765-ea176b9d96d5" />


#### 4.2.5. 구현 산출물 테스팅

테스팅 에이전트는 구현 단계에서 생성한 소스 코드와 배포 파일을 요구사항·유스케이스·OpenAPI 명세와 대조한다. 정적 검사에서는 Trivy로 보안 설정을 살피고, cloud-init·셸·Docker Compose·OpenTofu로 배포 파일의 형식과 구성을 확인한다.

동적 검사에서는 테스팅 에이전트가 LLM을 사용해 요구사항·유스케이스·API 명세에서 API 호출 순서와 성공 조건을 담은 테스트 계획을 작성한다. 애플리케이션을 컨테이너에서 실행하고 요청을 받을 준비가 되면 계획에 따라 API를 호출한다. 응답 코드·데이터 형식과 요구된 기능의 수행 결과를 검사하고, 호출 계획이나 입력값 때문에 실행이 멈춘 경우도 기록한다.

테스팅 보고서에는 항목별 통과·실패 결과, API 요청·응답, 오류 위치와 이유가 담긴다. 구현 결함이 발견되면 시스템 구현 에이전트에 전달해 수정하고, 수정한 파일을 다시 테스트한다.

<img width="1980" height="1068" alt="image" src="https://github.com/user-attachments/assets/5f80a14a-2056-4654-ada7-b04166919953" />


#### 4.2.6. 사용자 피드백

사용자는 유스케이스 명세나 설계 다이어그램에서 수정할 대상과 원하는 변경 사항을 자연어 피드백으로 전달한다. 시스템은 대상 유스케이스·클래스·메서드를 찾아 수정하고, 함께 바뀌어야 하는 다른 명세와 다이어그램을 확인한다.

수정 후에는 요구사항 누락, 클래스·메서드 참조와 호출 관계의 오류를 검사한다. 사용자는 피드백이 반영된 명세와 다이어그램을 확인하고 다음 단계로 진행하거나 추가 피드백을 보낼 수 있다.


### 4.3. 디렉토리 구조

```text
├── app/                            # 백엔드와 AI 에이전트 소스 코드
│   ├── requirements/               # 요구사항 정제·분류, 유스케이스 명세·다이어그램 생성
│   ├── design/                     # 클래스·시퀀스·API·ERD·배포 설계 생성과 수정
│   ├── implementation/             # OpenHands로 코드 구현, 테스트·배포 파일 생성
│   ├── testing/                    # 생성된 애플리케이션과 배포 파일의 정적·동적 검사
│   ├── workspace/                  # 사용자 요청·피드백과 단계별 진행 상황 관리
│   ├── cloudkb/                    # 클라우드 자원 의존관계·가격·성능 자료
│   │   ├── depkb/                  # 자원 생성에 필요한 선행 조건과 연결 규칙
│   │   ├── costkb/                 # VM 가격, 무료 사용 정책과 자원별 과금 규칙
│   │   ├── perfkb/                 # VM 성능 특성과 사양 비교 자료
│   │   ├── speckb/                 # AWS·Azure·GCP의 VM 사양 목록 수집
│   │   └── data/
│   ├── db/                         # MySQL 연결·테이블 정의·체크포인트 저장
│   ├── repositories/               # 앱 정보와 산출물의 DB 저장·조회
│   ├── metrics/                    # LLM 호출 기록과 응답 지연 분석
│   ├── artifacts_api.py
│   ├── config.py
│   ├── llm_connection.py
│   └── validation.py
├── frontend/                       # SvelteKit UI 소스 코드
│   ├── src/
│   │   ├── routes/
│   │   └── lib/
│   │       ├── components/
│   │       ├── api.ts
│   │       ├── types.ts
│   │       └── auto-mode.ts
│   ├── tests/
│   ├── package.json
│   └── vite.config.ts
├── evaluation/                     # 시스템 실행·비교 실험과 평가 자료
│   ├── easydep/                    # Workspace API를 이용한 개발 과정 실행 코드
│   ├── comparison/
│   ├── baselines/                  # MetaGPT·ChatDev 설치 스크립트와 비교 실험 자료
│   ├── checkpoint_e2e/             # 체크포인트를 이용한 단계별 실행 검증 자료
│   ├── dependency_audit/           # 자원 의존관계·배포 실험의 검증 결과
│   └── research_protocol/          # 실험 조건·평가 기준·측정 자료
├── tests/
├── inputs/                         # 애플리케이션별 요구사항 입력 파일
├── materials/                      # 요구사항 분류 모델과 학습 데이터
│   ├── BERT_FR_NFR_Classifier/
│   └── FR_NFR_Dataset/
├── artifacts/                      # 단계별 검증 결과와 실험 측정 기록
├── docs/
├── scripts/                        # 개발 환경 준비·실행 및 배포 스크립트
│   └── run-easydep.ps1
├── docker/                         # 구현·테스팅 도구의 Docker 이미지 빌드 설정
│   └── Dockerfile.toolchain
├── toolchain/                      # OpenTofu 플러그인 버전과 오프라인 저장소 설정
│   └── opentofu/
├── k8s/                            # 서버 배포용 Kubernetes 설정
│   ├── base/
│   └── overlays/
├── .easydep/
│   └── agent-plans/                # 기능 개선 계획 문서
├── install_and_build.sh
├── server.py
├── Dockerfile
├── requirements.txt
├── requirements-common.txt
├── requirements-bert.txt
├── requirements-browser-testing.txt
├── requirements-dev.txt
├── pyproject.toml
├── pytest.ini
├── verify_db.py
├── .env.example
├── .dockerignore
├── .gitignore
├── .gitattributes
├── AGENTS.md
└── README.md
```




## 5. 설치 및 실행 방법

### 5.1. 설치절차 및 실행 방법

설치·실행 전에 다음 프로그램을 설치하고 LLM API 설정을 준비한다.

| 필수 프로그램 | 최소 버전 |
|---|---|
| Python | 3.12 이상 |
| Node.js | 22.12 이상 |
| npm | |
| Git | |
| Docker·Compose | |

환경 설정은 `.env.example`을 복사한 루트의 `.env`에 작성한다. LLM API를 호출하기 위해 다음 네 값을 사용할 서비스에 맞게 설정한다.

| 변수 | 설정할 내용 |
|---|---|
| `LLM_PROVIDER` | API 제공자 구분값: `openrouter`, `nvidia_nim`, `cloudflare`, `openai_compatible` 중 선택 |
| `API_KEY` | 사용할 API 서비스에서 발급받은 인증 키 |
| `BASE_URL` | 해당 서비스의 OpenAI 호환 API 기본 주소 |
| `MODEL` | 해당 서비스에서 호출할 LLM 모델명 |

API 키가 들어간 `.env`는 커밋하지 않는다. 개발용 MySQL 서버와 JDK·Gradle·OpenTofu 등의 구현·테스팅 도구는 실행 스크립트를 통해 Docker 컨테이너로 준비한다.

모든 OS에서 빌드된 프론트엔드와 API를 FastAPI 서버 한 곳에서 제공한다. 로컬 서버 주소는 `http://127.0.0.1:8100/`이다.

#### 5.1.1. Windows

Docker Desktop을 Linux 컨테이너 모드로 실행한 뒤 PowerShell에서 `.env`를 만든다. 기존 파일은 유지한다.

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

LLM API 설정을 마친 뒤 기존 PowerShell 스크립트를 실행한다. `-ProductionLike` 옵션은 프론트엔드를 정적 파일로 빌드하고 FastAPI에서 함께 제공한다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-easydep.ps1 -ProductionLike
```

스크립트가 Python 가상환경·의존성, npm 패키지와 공용 도구 이미지를 준비하고 MySQL과 서버를 시작한다. 실행 상태와 로그는 루트의 `.easydep/dev/`에 저장된다.

첫 설치·빌드가 끝난 뒤 기존 환경과 빌드를 재사용하려면 다음 명령을 사용한다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-easydep.ps1 -ProductionLike -SkipBootstrap -SkipFrontendBuild
```

#### 5.1.2. macOS

Docker Desktop을 실행한 뒤 Bash 터미널에서 `.env`를 만든다. 기존 파일은 유지한다.

```bash
[ -f .env ] || cp .env.example .env
```

LLM API 설정을 마친 뒤 셸 스크립트로 설치·빌드와 서버 실행을 진행한다.

```bash
bash ./install_and_build.sh --run
```

스크립트는 같은 루트의 소스에서 Python 가상환경, 프론트엔드 정적 빌드와 공용 도구 이미지를 준비한다. 도구 이미지는 `linux/amd64`용이다. Apple Silicon Mac에서는 Docker Desktop의 amd64 컨테이너 실행 기능이 필요하다.

기존 설치·빌드를 재사용해 서버를 다시 실행하려면 다음 명령을 사용한다.

```bash
bash ./install_and_build.sh --skip-install --run
```

#### 5.1.3. Linux

x86_64 Linux에서 Docker Engine과 Compose 플러그인을 준비한다. 현재 계정의 Bash에서 Docker 실행 상태를 확인하고 `.env`를 만든다.

```bash
docker info
docker compose version
[ -f .env ] || cp .env.example .env
```

LLM API 설정을 마친 뒤 셸 스크립트로 설치·빌드와 서버 실행을 진행한다.

```bash
bash ./install_and_build.sh --run
```

기존 설치·빌드를 재사용해 서버를 다시 실행하려면 다음 명령을 사용한다.

```bash
bash ./install_and_build.sh --skip-install --run
```

#### 5.1.4. 설치 항목과 실행 옵션

실행 스크립트는 프로젝트 루트 안에 가상환경을 만들고 Python·npm 의존성을 설치한다. 프론트엔드 정적 파일과 구현·테스팅 공용 도구 이미지를 빌드한 뒤 개발용 MySQL과 서버를 시작한다. 기존 `.env`와 MySQL 데이터는 유지한다.

기본 DB 설정은 `DB_HOST=127.0.0.1`, `DB_PORT=33060`, `DB_USER=root`, `DB_PASSWORD=easydep-local`, `DB_NAME=easydep`다.

| 실행 스크립트 | 옵션 | 동작 |
|---|---|---|
| Windows PowerShell | `-ProductionLike` | 프론트엔드 빌드 후 FastAPI에서 화면과 API 제공 |
| Windows PowerShell | `-SkipBootstrap` | 기존 Python·npm 환경과 도구 이미지 재사용 |
| Windows PowerShell | `-SkipFrontendBuild` | 기존 프론트엔드 정적 빌드 재사용. `-ProductionLike`와 함께 사용 |
| Windows PowerShell | `-Stop` | 스크립트가 시작한 서버와 개발용 MySQL 중지 |
| macOS·Linux 셸 | `--run` | 설치·빌드 후 MySQL과 서버 시작 |
| macOS·Linux 셸 | `--skip-install` | 기존 가상환경·프론트엔드 빌드·도구 이미지 재사용 |
| macOS·Linux 셸 | `--help` | 사용법 표시 |

셸 스크립트를 옵션 없이 실행하면 설치·빌드만 수행한다.

#### 5.1.5. 접속과 종료

| 주소 | 용도 |
|---|---|
| `http://127.0.0.1:8100/` | 모든 OS의 요구사항 입력 및 생성 결과 확인 화면 |
| `http://127.0.0.1:8100/docs` | 백엔드 API 문서 |

Windows는 프로젝트 루트의 PowerShell에서 다음 명령으로 FastAPI 서버와 개발용 MySQL 서버를 중지한다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-easydep.ps1 -Stop
```

macOS·Linux는 실행 터미널에서 로그를 확인하며 `Ctrl+C`로 서버를 종료한다. 기본 개발용 MySQL 서버를 중지하려면 다음 명령을 실행한다.

```bash
docker stop easydep-mysql-dev
```

### 5.2. 오류 발생 시 해결 방법


## 6. 소개 자료 및 시연 영상

### 6.1. 프로젝트 소개 자료

[발표자료](docs/03.발표자료/발표자료.pdf)

### 6.2. 시연 영상

[시연 동영상](https://youtu.be/evDqGKBisOM?si=yqHsIXVeWK5AdC7k)

## 7. 팀 구성

### 7.1. 팀원별 소개 및 역할 분담

| 팀원 | 역할 | 이메일 |
|---|---|---|
| 김재우 | 전체 에이전트 개발<br>AWS·Azure·GCP 리소스 간 의존관계 자료 구축<br>Docker·Kubernetes 배포와 전체 과정 사례 연구 수행<br> 대화형 UI 개발 | projw1024@pusan.ac.kr |
| 이상현 | 전체 에이전트 개발<br>산출물간 정합성 검증·피드백 기능 보완<br>AWS·Azure·GCP 리소스 간 VM 사양·가격 자료 구축<br>비교 평가 수행 및 모니터링 시스템 구축 | ask0208ask@pusan.ac.kr |
| 황수환 | 전체 에이전트 개발<br>산출물간 정합성 검증·피드백 기능 보완 | tnghks2866@pusan.ac.kr |


## 8. 참고 문헌 및 출처

[1] Z. Ji, N. Lee, R. Frieske, T. Yu, D. Su, Y. Xu, E. Ishii, Y. J. Bang, A. Madotto, and P. Fung, “Survey of Hallucination in Natural Language Generation,” ACM Computing Sur-veys, Vol. 55, No. 12, Article 248, pp. 1-38, Mar. 2023.<br>
[2] Cloud Native Computing Foundation, "Cloud Native Definition v1.1," Feb. 2024. [Online]. Available: https://github.com/cncf/toc/blob/main/DEFINITION.md (download-ed 2026, Sep. 14).<br>
[3] A. Richardson, "Cloud Native Reference Architecture," Cloud Native Computing Foun-dation, Feb. 2017. [Online]. Available: https://www.cncf.io/wp-content/uploads/2020/08/What-is-Cloud-Native-CNCF-Webinar-23-Feb-2017-1.pdf (downloaded 2026, Sep. 14).<br>
[4] T. B. Brown, B. Mann, N. Ryder, M. Subbiah, J. Kaplan, P. Dhariwal, A. Neelakantan, P. Shyam, G. Sastry, A. Askell, S. Agarwal, A. Herbert-Voss, G. Krueger, T. Henighan, R. Child, A. Ramesh, D. M. Ziegler, J. Wu, C. Winter, C. Hesse, M. Chen, E. Sigler, M. Litwin, S. Gray, B. Chess, J. Clark, C. Berner, S. McCandlish, A. Radford, I. Sutskever, and D. Amodei, "Lan-guage Models are Few-Shot Learners," Proc. of the 34th Conference on Neural Infor-mation Processing Systems, pp. 1877-1901, Jul. 2020.<br>
[5] L. Wang, C. Ma, X. Feng, Z. Zhang, H. Yang, J. Zhang, Z. Chen, J. Tang, X. Chen, Y. Lin, W. X. Zhao, Z. Wei, and J.-R. Wen, "A Survey on Large Language Model Based Au-tono-mous Agents," Frontiers of Computer Science, Vol. 18, No. 6, Article 186345, pp. 1-26,  Mar. 2024.<br>
[6] LangChain, "LangGraph Overview" [Online]. Available: https://docs.langchain.com/oss/python/langgraph/overview (downloaded 2026, Sep. 14).<br>
[7] OpenHands, "Software Agent SDK Architecture Overview" [Online]. Available: https://docs.openhands.dev/sdk/arch/overview (downloaded 2026, Sep. 14).<br>
[8] S. Hong, M. Zhuge, J. Chen, X. Zheng, Y. Cheng, J. Wang, C. Zhang, Z. Wang, S. K. S. Yau, Z. Lin, L. Zhou, C. Ran, L. Xiao, C. Wu, and J. Schmidhuber, "MetaGPT: Meta Pro-gramming for A Multi-Agent Collaborative Framework," Proc. of the Twelfth Inter-national Conference on Learning Representations, Nov. 2024.<br>
[9] C. Qian, W. Liu, H. Liu, N. Chen, Y. Dang, J. Li, C. Yang, W. Chen, Y. Su, X. Cong, J. Xu, D. Li, Z. Liu, and M. Sun, "ChatDev: Communicative Agents for Software Develop-ment," Proc. of the 62nd Annual Meeting of the Association for Computational Lin-guistics, pp. 15174-15186, Aug. 2024.<br>
[10] J. Devlin, M.-W. Chang, K. Lee, and K. Toutanova, "BERT: Pre-training of Deep Bidi-rec-tional Transformers for Language Understanding," Proc. of the 2019 Conference of the North American Chapter of the Association for Computational Linguistics: Human Lan-guage Technologies, pp. 4171-4186, Jun. 2019.<br>
[11] I. Jacobson, M. Christerson, P. Jonsson, and G. Overgaard, Object-Oriented Soft-ware Engineering: A Use Case Driven Approach, Addison-Wesley, 1992.<br>
[12] Object Management Group, "Unified Modeling Language (UML), Version 2.5.1" [Online]. Available: https://www.omg.org/spec/UML/2.5.1 (downloaded 2026, Sep. 14).<br>
[13] OpenAPI Initiative, "OpenAPI Specification, Version 3.1.1" [Online]. Available: https://spec.openapis.org/oas/v3.1.1.html (downloaded 2026, Sep. 14).<br>
[14] Docker, Inc., "Docker Overview" [Online]. Available: https://docs.docker.com/get-started/docker-overview/ (downloaded 2026, Sep. 14).<br>
[15] HashiCorp, "What Is Terraform?" [Online]. Available: https://developer.hashicorp.com/terraform/intro (downloaded 2026, Sep. 14).<br>
[16] Amazon Web Services, "Overview of Amazon Web Services" [Online]. Available: https://docs.aws.amazon.com/whitepapers/latest/aws-overview/amazon-web-services-cloud-platform.html (downloaded 2026, Sep. 14).<br>
[17] Microsoft, "Technology Choices for Azure Solutions" [Online]. Available: https://learn.microsoft.com/en-us/azure/architecture/guide/technology-choices/technology-choices-overview (downloaded 2026, Sep. 14).<br>
[18] Google Cloud, "Products and Services" [Online]. Available: https://cloud.google.com/products (downloaded 2026, Sep. 14).<br>
[19] A. Cockburn, Writing Effective Use Cases, Addison-Wesley Professional, 2000.
