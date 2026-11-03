# llama-server from llama.cpp (or ik_llama.cpp, see ../ik-llama-cpp), CPU only.
# Built with the upstream checkout at REF as context (see .github/workflows/image.yaml).
#
# One static binary, no web UI (no npm, no Hugging Face asset download at build
# time), no TLS client (models come from the chart's download Job, never from
# the server). The ik-llama-cpp image uses this same file with ENGINE=ik.

FROM debian:stable-slim AS build
# x86-64-v3 (AVX2/FMA/F16C/BMI2) as named flags, not GGML_NATIVE: a native
# build SIGILLs on a different host CPU. Matches the colibri image.
ARG ENGINE=mainline
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential cmake && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN set -eu; \
    common="-DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DLLAMA_BUILD_TESTS=OFF \
      -DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON"; \
    case "$ENGINE" in \
      mainline) extra="-DGGML_BMI2=ON -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_OPENSSL=OFF" ;; \
      ik)       extra="-DLLAMA_CURL=OFF" ;; \
      *) echo "unknown ENGINE=$ENGINE" >&2; exit 1 ;; \
    esac; \
    cmake -S . -B build $common $extra && \
    cmake --build build --target llama-server -j"$(nproc)" && \
    install -D -m 0755 build/bin/llama-server /out/llama-server

FROM debian:stable-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -m -u 1000 llama
COPY --from=build /out/llama-server /app/llama-server
EXPOSE 8080
USER llama
ENTRYPOINT ["/app/llama-server"]
CMD ["--help"]
