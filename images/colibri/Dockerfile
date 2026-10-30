# colibri CPU image with the GLM and qwen36 engines. Built with the upstream
# checkout at COLIBRI_REF as context (see .github/workflows/image.yaml).
#
# Based on upstream docker/Dockerfile.slim, which builds only the GLM engine and
# sets COLI_ENGINE + COLI_DOCKER_GLM_ONLY. With COLI_ENGINE set, coli sends every
# family to that one binary; without it, coli looks for <engine_artifact> next to
# itself, which is how qwen36 (Qwen3-Coder-30B-A3B) is found here.
# ponytail: two engines only; add a family's make target and binary when a chart uses it.

FROM debian:stable-slim AS build
# x86-64-v3 (AVX2), not native: a native build SIGILLs on a different host CPU.
ARG ARCH=x86-64-v3
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential make && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY c/ ./c/
RUN make -C c colibri qwen36 ARCH=${ARCH}

FROM debian:stable-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -m -u 1000 colibri
WORKDIR /app
COPY --from=build /src/c/colibri /src/c/qwen36 ./
COPY c/coli c/*.py ./
COPY c/tools/ ./tools/

ENV COLI_MODEL=/model \
    PYTHONUNBUFFERED=1
VOLUME ["/model"]
EXPOSE 5000

USER colibri
ENTRYPOINT ["python3", "/app/coli"]
CMD ["info"]
