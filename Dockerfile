FROM plantuml/plantuml@sha256:47870c1f76cfb3747bc7090bfe83013a4e3105b5a0bb1515e2baf5d3e2b3ee9d AS plantuml-runtime
FROM ghcr.io/astral-sh/uv:0.8.22 AS uv-runtime

FROM eclipse-temurin:21-jre-jammy AS jre-runtime

FROM docker:27.5.1-cli AS docker-runtime
FROM node:22-bookworm-slim AS node-runtime

# 런타임과 같은 Node/npm을 사용한다. 별도 Alpine 이미지를 쓰면 도구 버전이 달라지고
# npm 자체 오류가 나도 로컬 실행 환경과 비교하기 어렵다.
FROM node-runtime AS frontend-build
WORKDIR /src
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --include=dev --no-audit --no-fund \
      --registry="${NPM_REGISTRY}" --replace-registry-host=always \
    && test -x node_modules/.bin/vite
COPY frontend ./
RUN npm run build

# BERT FR/NFR 검증 가중치를 되살리는 단계. 저장소에는 45MiB 조각으로 쪼개 들어 있어서
# (GitHub 파일당 100MiB 한도) 한 번 이어 붙여야 한다.
# 별도 stage에서 하고 결과만 가져와야 조각과 완성본이 이미지에 함께 남지 않는다.
# 런타임에는 이미 준비된 상태라 파드 기동에 재조립 비용이 없다.
FROM python:3.13-slim-bookworm AS weights
WORKDIR /build
COPY materials/BERT_FR_NFR_Classifier/bert_model ./materials/BERT_FR_NFR_Classifier/bert_model
COPY app/requirements/model_assets.py ./app/requirements/model_assets.py
RUN python app/requirements/model_assets.py --dest /opt/bert_model

FROM python:3.13-slim-bookworm AS python-common-dependencies

# API runtime이 import하는 Python 패키지를 설치한다. 개발용 구현·Testing 툴체인은
# docker/Dockerfile.toolchain에서 별도 계층으로 관리한다.
COPY --from=uv-runtime /uv /usr/local/bin/uv
COPY requirements-common.txt /tmp/easydep-requirements-common.txt
ENV UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system \
      --requirements /tmp/easydep-requirements-common.txt

# 요구사항 분류기가 필요한 runtime만 BERT Python 패키지를 가진다.
FROM python-common-dependencies AS python-runtime-dependencies
COPY requirements-bert.txt /tmp/easydep-requirements-bert.txt
# PyPI와 PyTorch CPU 저장소가 함께 선언돼 있다. uv의 기본 first-index 정책을 쓰면
# PyTorch 저장소에 우연히 있는 일반 패키지만 볼 수 있어 두 공식 저장소를 함께 탐색한다.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --index-strategy unsafe-best-match \
      --requirements /tmp/easydep-requirements-bert.txt

# 원격 배포에서 FastAPI와 프런트엔드를 제공하는 대상이다. 빌드·테스트 도구는 별도의
# toolchain image로 실행하므로 여기에는 PlantUML용 JRE와 Docker CLI만 추가한다.
FROM python-runtime-dependencies AS runtime

COPY --from=jre-runtime /opt/java/openjdk /opt/java/openjdk
COPY --from=docker-runtime /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-runtime /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"
ENV LITELLM_LOCAL_MODEL_COST_MAP=true
ENV OPENHANDS_SUPPRESS_BANNER=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz fonts-dejavu-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY --from=plantuml-runtime /opt/plantuml.jar /opt/plantuml/plantuml.jar
ENV PLANTUML_JAR=/opt/plantuml/plantuml.jar

COPY app ./app
COPY server.py ./server.py
COPY --from=frontend-build /src/build ./frontend/build

EXPOSE 8000
RUN useradd -m appuser \
    && mkdir -p /app/.easydep \
    && chown -R appuser:appuser /app/.easydep

# runtime만 BERT를 소유한다. toolchain preflight에서는 요구하지 않는다.
ENV BERT_MODEL_CACHE_DIR=/app/.easydep/models/bert_fr_nfr
COPY --from=weights /opt/bert_model /app/.easydep/models/bert_fr_nfr

USER appuser

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
