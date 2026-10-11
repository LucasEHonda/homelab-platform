# ADR 0002: Request observability for the apps

Status: Proposed
Date: 2026-10-11

## Context

Three apps run on the TrueNAS NAS: games (Django and React), pinguei (a support widget) and financas (Django and TanStack Start). Dozzle already shows the raw container logs. The owner wants more than logs:

- the number of requests per app and per route, over time;
- every request with all the data that can be captured: method, URL, headers, request body, response body, status and duration.

Constraints from the owner:

- It must use little CPU, RAM and disk on the NAS (`/mnt/svd`).
- Only the backends are instrumented. The frontends are not. Their calls reach the backends and are captured there.
- Data is kept for 30 days at most. It is not needed for audits, so losing a few seconds of data is acceptable.
- Recording does not need to be real time. A queue that records the data when there is spare capacity is preferred, so that the three apps under load at the same time do not spend extra CPU.
- No masking of sensitive data for now, to avoid the processing cost.

## Options

### OpenTelemetry SDK in each backend, OpenObserve as the backend (chosen)

Each backend records one span per request with the OpenTelemetry SDK. The span carries the HTTP data as attributes. The SDK puts spans in an in-memory queue and a background thread exports them in batches over OTLP/HTTP to one shared OpenObserve instance on the NAS.

- OpenObserve is a single Rust binary. It needs no ClickHouse, Kafka or Elasticsearch. In local mode it keeps its metadata in SQLite and its data in compressed Parquet files.
- It receives traces, logs and metrics over OTLP and has a UI with SQL search and dashboards.
- Request counts, latency and error rate are computed from the spans, so no separate metrics pipeline is needed.
- Retention is one setting: `ZO_COMPACT_DATA_RETENTION_DAYS`.
- Expected cost: about 150 to 400 MB of RAM for the one OpenObserve instance, and about 20 to 60 MB per instrumented Python process. These are estimates, to be measured with `make nas-stats` after rollout.

### Sentry self-hosted

Good request and error views, but the self-hosted stack needs 8 to 16 GB of RAM and many containers. Rejected for the resource cost.

### SigNoz, HyperDX or Uptrace

Good OpenTelemetry-native UIs, but all of them store data in ClickHouse, which needs at least 2 to 4 GB of RAM to run well. Rejected for the resource cost.

### Grafana LGTM (Tempo, Loki, Prometheus, Grafana)

Mature, but four components to run, configure and upgrade, at about 1 GB of RAM or more together. Rejected for the operating cost.

### Reverse proxy access logs with GoAccess

Very light, but access logs do not carry request or response bodies, and the apps are reached through Tailscale serve, which does not write access logs. Rejected because it does not meet the main need.

## Decision

Use the OpenTelemetry SDK in each backend and one OpenObserve instance in the platform stack.

- Only the HTTP server side of each backend is instrumented, plus the database client, so that slow queries show inside the request span. Other auto-instrumentations stay off.
- Headers are captured by the OpenTelemetry Django instrumentation through environment variables, with no code.
- Request and response bodies are captured by a small middleware in each app. Bodies are truncated to 8 KB. Multipart uploads, streaming responses and binary content types are skipped.
- Spans are exported in batches every 15 seconds by the `BatchSpanProcessor`. Its in-memory queue is the "record when possible" queue. If OpenObserve is slow or down, the SDK retries with backoff, and if the queue fills up it drops spans. It never blocks a request.
- OpenObserve runs with CPU and memory limits, so that under load ingestion slows down instead of taking CPU from the apps.
- Data is deleted after 30 days.
- The OpenObserve UI is reachable only through its own Tailscale node, for admins.
- No masking is done. See Consequences.

## Consequences

- **Secrets and personal data are stored in plain text in OpenObserve.** With no masking, the stored spans include `Authorization` and `Cookie` headers, JWT access and refresh tokens in login and refresh responses, passwords in login, register and password change bodies, and financial data in financas responses. Anyone who can open the OpenObserve UI can read all of it for 30 days. The apps use an ingestion passcode, which can write traces but cannot read them (verified on v1.0.4). Access is limited to tailnet admins, and OpenObserve credentials must be kept in Infisical. Masking can be added later in the same middleware without changing the rest of the design. A cheaper first step is to skip body capture for the auth routes, which costs one string comparison per request.
- The in-memory queue is lost when a process stops. A deploy, a restart, or a gunicorn worker recycled by `--max-requests` can drop up to one batch window (about 15 seconds) of spans that were not exported yet. The SDK flushes on a clean shutdown, so most of these are still sent. A queue on disk would need an OpenTelemetry Collector with a persistent queue, at about 50 to 100 MB of RAM more. This is not worth it for data that has no audit value.
- In Python, the export thread shares the GIL with the app. The cost is small because the export runs once per batch window, not once per request.
- The body capture middleware runs inside the request. It reads the cached request body and the rendered response, then truncates them. This cost is small, but it is not zero.
- Each app gets a new external network, `platform-telemetry`, which must be added to `shared_networks` in `deploy/apps.yml`, or the deployer's compose policy rejects the app.
- Each app gets new dependencies (OpenTelemetry packages), a `wsgi.py` call that starts tracing, and a middleware. Without the OpenTelemetry environment variables the code does nothing, so local development and tests do not need OpenObserve.
- OpenObserve data is not backed up. Losing it loses only observability history.

## Implementation

The step-by-step plan, with the platform changes and the changes for each app (pinguei as the worked example), is in [docs/observability-plan.md](../observability-plan.md).
