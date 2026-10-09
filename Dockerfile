FROM docker:27.3.1-cli AS docker-cli

FROM python:3.12.7-slim AS base
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
