FROM debian:bookworm-slim

ARG AGY_VERSION=1.2.14
ARG AGY_UID=1000
ARG AGY_GID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git jq procps tini \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    if getent group "${AGY_GID}" >/dev/null; then AGY_GROUP="$(getent group "${AGY_GID}" | cut -d: -f1)"; \
    else groupadd -g "${AGY_GID}" agy && AGY_GROUP=agy; fi; \
    useradd -m -u "${AGY_UID}" -g "${AGY_GID}" -s /bin/bash agy

USER agy
WORKDIR /home/agy
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && /home/agy/.local/bin/agy --version | grep -Fx "${AGY_VERSION}"

ENV HOME=/home/agy
ENV PATH=/home/agy/.local/bin:/usr/local/bin:/usr/bin:/bin
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["agy","--help"]
