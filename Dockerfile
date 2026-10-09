# Same Docker/Compose generation as the NAS engine (Docker 27.5, Compose v2.32): newer Compose
# clients misread its network info. Bump only together with TrueNAS.
FROM docker:27.5.0-cli AS docker-cli

FROM python:3.14.7-slim AS base
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
WORKDIR /app
COPY pyproject.toml ./
COPY deployer ./deployer

FROM base AS dev
RUN pip install --no-cache-dir -e ".[dev]"

FROM base AS runtime
RUN pip install --no-cache-dir .
EXPOSE 8080
CMD ["python", "-m", "deployer"]
