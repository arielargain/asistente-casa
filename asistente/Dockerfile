ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 PYTHONUNBUFFERED=1 PIP_BREAK_SYSTEM_PACKAGES=1

# La SDK de agentes de Claude corre sobre el CLI de Claude Code, asi que la
# imagen necesita Node ademas de Python. git va porque el agente clona y
# trabaja sobre los repos de Ariel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ca-certificates curl git openssh-client jq \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY app /app
COPY run.sh /
RUN chmod a+x /run.sh

WORKDIR /app
CMD ["/run.sh"]
