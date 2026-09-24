## 1. 프로젝트 배경

### 1.1. 국내외 시장 현황 및 문제점

클라우드 시장이 성장하면서 클라우드 네이티브 애플리케이션의 개발 수요도 늘고 있다. 이러한 애플리케이션은 서비스 기능뿐 아니라 클라우드 자원의 구성과 배포까지 고려해야 하므로 개발 과정이 복잡하다. 이에 개발 부담을 줄이고 효율을 높이기 위해 AI를 활용하려는 시도가 확대되고 있다. 그러나 클라우드 네이티브 애플리케이션 개발에 AI 에이전트를 도입하려면 다음과 같은 문제를 해결해야 한다.
  1. LLM 환각 현상에 따른 산출물 오류: AI가 요구사항에 없는 기능을 추가하거나 앞서 작성한 내용과 모순되는 설계와 코드를 만들 수 있다. 결과물이 자연스럽게 보이더라도 실제 사용자 의도와 일치하는지 확인해야 한다.
  2. AI 에이전트간의 결함 추적의 어려움: AI 에이전트가 목표를 수행하기 위해 사용자의 의도를 전달하는 과정에서, 사용자의 목표가 부정확하게 작성되거나 의도와 다르게 파악된 경우, 이로 인해 에이전트들을 거치면서 의도와는 다른 최종 산출물이 생성될 수 있다.
  3. 클라우드 자원 구성의 복잡성: 클라우드 네이티브 애플리케이션이 동작하는 클라우드 환경은 다양한 클라우드 리소스를 연결 · 참조하여 형성된다. 클라우드 리소스는 생성 시 선행 배포가 필요한 리소스가 존재하며, 여러 CSP(Cloud Service Provider)를 동시에 제어하는 환경에서는 CSP 별로 다른 리소스 관리 방법이 요구된다. 즉, CSP 마다 생성하고자 하는 클라우드 리소스를 배포하고 클라우드 네이티브 애플리케이션에 맞춰 클라우드 환경을 구성해야 하는 문제가 있다.

### 1.2. 필요성과 기대효과

클라우드 네이티브 애플리케이션 개발에 AI 에이전트를 활용하려면, 각 에이전트가 결과물을 생성하는 것만으로는 부족하다. 요구사항에 없는 내용이 설계에 포함되면 이후 코드에도 반영될 수 있고, 클라우드 자원 간 의존관계를 놓치면 애플리케이션을 배포하지 못할 수 있다. 따라서 사용자 의도가 개발 단계마다 일관되게 전달되는지 확인하고, 생성된 산출물과 클라우드 배포 구성을 함께 검토할 수 있는 지원 체계가 필요하다.
이에 본 연구는 개발 단계별 산출물을 연결하고 사용자가 중간 결과를 검토해 수정 의견을 반영할 수 있도록 한다. 또한 클라우드 자원의 의존관계와 사양을 배포 구성에 반영하고, 생성 결과를 테스트한다. 이를 통해 오류가 후속 단계로 이어지기 전에 발견하고, 변경이 필요한 산출물을 파악하며, 개발과 배포 과정의 반복 작업을 줄일 수 있을 것으로 기대한다.

## 2. 개발 목표

### 2.1. 목표 및 세부 내용

본 연구는 멀티 AI 에이전트 기반으로 클라우드 네이티브 애플리케이션의 자연어 요구사항을 분석하고, UML 다이어그램으로 설계하며, IaC·소스 코드를 통해 시스템을 구현, 산출물 테스팅을 수행하는 기술을 연구하는 것이다.
  1. 멀티 AI 에이전트 구조 설계: 요구사항 분석, 시스템 설계, 구현과 테스트를 담당하는 에이전트를 구성하고, 각 단계의 구조화된 산출물이 다음 단계의 입력으로 이어지도록 연계한다.
  2. 클라우드 네이티브 애플리케이션 제약사항 해소 방법: AWS·Azure·GCP 리소스 간 의존 관계, VM 사양, 비용과 성능 정보를 활용하여 사용자 요구조건에 맞는 클라우드 리소스 구성과 배포 방안을 제시한다.
  3. 멀티 AI 에이전트 간 산출물 연계 방안 구축: 사용자가 단계별 산출물을 검토하고 피드백을 제공할 수 있도록 하며, 피드백의 대상과 영향 범위에 따라 관련 산출물을 일관되게 수정한다.


### 2.2. 기존 서비스 대비 차별성

기존 AI 기반 개발 지원 방식에서는 에이전트가 만든 결과물이 다음 단계로 전달되는 동안 사용자 요구사항이 어떻게 반영됐는지 확인하기 어렵다. 산출물에 수정이 필요할 때도 관련 설계와 코드에 변경 사항을 일관되게 적용해야 한다는 과제가 남는다.
이에 제안하는 시스템은 요구사항 분석, 설계, 구현, 테스팅 결과를 구조화된 산출물로 연결한다. 사용자는 단계별 결과를 검토하고 피드백을 전달할 수 있으며, 시스템은 수정 대상과 영향을 받는 산출물을 확인해 후속 작업에 반영한다. 이를 통해 사용자의 의도가 개발 과정에서 달라지거나 일부 산출물에만 반영되는 문제를 줄이고자 한다.
또한 클라우드 배포 환경을 개발 과정에 함께 반영하여 클라우드 제공자별 자원 의존관계와 VM 사양·비용 조건을 고려해 배포 구성을 만들고, 생성된 코드와 배포 파일을 검사한다. 애플리케이션 기능뿐 아니라 이를 실행할 클라우드 환경까지 하나의 개발 흐름에서 다룰 수 있도록 설계했다.

### 2.3. 사회적 가치 도입 계획

본 연구는 클라우드 개발 경험이 적은 학생과 소규모 개발팀도 애플리케이션의 설계와 배포 과정에 직접 참여할 수 있도록 지원하고자 한다. 제안하는 시스템은 생성한 요구사항, 설계 문서, 코드와 테스트 결과를 단계별로 보여준다. 사용자는 각 결과를 검토하고 수정 의견을 전달하며 AI와 협력해 개발을 진행할 수 있다.
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

### 4.2. 기능 설명 및 주요 기능 명세서



### 4.3. 디렉토리 구조




## 5. 설치 및 실행 방법

### 5.1. 설치절차 및 실행 방법


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
