# Request observability: implementation plan

This plan implements [ADR 0002](adr/0002-request-observability.md). It has two parts:

1. **Platform** (this repository): one OpenObserve instance, its Tailscale node, a shared `platform-telemetry` network, and the setup script changes.
2. **Apps** (each app repository): the OpenTelemetry SDK, a body capture middleware, and the compose and secret changes. Pinguei is the worked example and is implemented. Financas and games follow the same steps, because they are also Django.

Status: Platform part and pinguei are implemented. Financas and games are pending.

## Goals and limits

- Count of requests per app, route and status, over time.
- Every request with method, URL, route, headers, request body, response body, status, duration and the database queries it ran.
- Backends only. No frontend instrumentation.
- Data kept for 30 days, then deleted.
- No masking of sensitive data (see the Security section).
- Spans exported in batches in the background. Losing a few seconds of spans on a restart is acceptable.
- Low resource use. Targets, to be measured after rollout:
  - OpenObserve: 512 MB memory limit, 0.5 CPU limit.
  - Each instrumented gunicorn worker: about 20 to 60 MB more than today.

## Architecture

```
                     tailnet (admins only)
                              |
                              v
                   +---------------------+
                   | observe-tailscale   |  Tailscale node "observe", HTTPS
                   +----------+----------+
                              | http://openobserve:5080 (UI)
  network: platform           v
                   +---------------------+
                   | openobserve         |  /mnt/svd/platform/data/openobserve
                   +----------+----------+  30-day retention, 0.5 CPU, 512 MB
                              ^
  network: platform-telemetry | OTLP/HTTP, batch every 15 s
          +-------------------+-------------------+
          |                   |                   |
   financas api         games api           pinguei api
   (gunicorn workers: OTel SDK + BatchSpanProcessor queue in memory)
```

- `platform-telemetry` is an internal network (no route to the internet). Only the app API containers and OpenObserve join it.
- The apps never reach the UI port through Tailscale. They send to OpenObserve directly on the internal network.
- Worker and beat containers are not instrumented in this phase.

## How the "queue" works

There is no extra queue service. The OpenTelemetry SDK already has one:

1. The request ends. The Django instrumentation closes the span. `BatchSpanProcessor.on_end` appends the span to an in-memory queue. This is a cheap append, and the request returns right away.
2. A background thread in each gunicorn worker wakes every `OTEL_BSP_SCHEDULE_DELAY` milliseconds, or earlier when a full batch is ready, and sends up to `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` spans in one OTLP/HTTP request.
3. If OpenObserve is slow or down, the exporter retries with exponential backoff. While it waits, new spans keep going into the queue.
4. If the queue reaches `OTEL_BSP_MAX_QUEUE_SIZE`, new spans are dropped. The request is never blocked. This caps the memory the queue can use.
5. On the OpenObserve side, ingested data goes first to a write-ahead log and an in-memory table. A background job writes it out as Parquet files and compacts them every `ZO_COMPACT_INTERVAL` seconds. With a CPU limit on the container, a load spike only makes ingestion slower. The apps do not notice.

Values for this plan:

| Variable | Value | Why |
|---|---|---|
| `OTEL_BSP_SCHEDULE_DELAY` | `15000` | Export every 15 s. Fewer, larger exports cost less CPU. |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` | `512` | One HTTP request per 512 spans. |
| `OTEL_BSP_MAX_QUEUE_SIZE` | `4096` | Upper bound on queued spans per worker. With 8 KB bodies, the worst case is about 70 MB per worker. In practice much less, because most bodies are small. |
| `OTEL_BSP_EXPORT_TIMEOUT` | `10000` | Give up on one export after 10 s. |
| `OTEL_EXPORTER_OTLP_TRACES_COMPRESSION` | `none` | The network is local. Gzip would only add CPU. |

What is lost on a restart: the spans still in the queue of a worker that stops. The SDK's TracerProvider flushes the queue at exit through `atexit` (its default `shutdown_on_exit`), so a clean stop (deploy, `--max-requests` recycle) loses almost nothing. A crash or `SIGKILL` loses up to one batch window. This is accepted in the ADR.

## Part 1: platform changes (this repository)

### 1.1 `deploy/compose.yml`

Two services and one network:

```yaml
  openobserve:
    image: public.ecr.aws/zinclabs/openobserve:${OPENOBSERVE_VERSION:-v1.0.4}
    user: "568:568"                        # the image runs as 568:568
    env_file: ../.envs/.openobserve
    environment:
      ZO_DATA_DIR: /data
      ZO_LOCAL_MODE: "true"                # single node, metadata in SQLite
      ZO_HTTP_PORT: "5080"
      ZO_COMPACT_DATA_RETENTION_DAYS: "30"
      ZO_COMPACT_INTERVAL: "60"
      ZO_TELEMETRY: "false"                # no anonymous telemetry to the vendor
      ZO_MEM_TABLE_MAX_SIZE: "128"         # MB. The default memtable limit is half of the host memory, far above this container's limit.
      ZO_MAX_FILE_SIZE_IN_MEMORY: "64"     # MB; default 256 is too large for a 512 MB limit
      ZO_MEMORY_CACHE_ENABLED: "false"     # query speed vs memory; turn on if queries are slow
    volumes:
      - ../data/openobserve:/data
    networks: [platform, telemetry]
    restart: unless-stopped
    logging: *logging
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 512m

  observe-tailscale:
    image: tailscale/tailscale:${TAILSCALE_VERSION:-v1.102.4}
    hostname: observe
    env_file: ../.envs/.tailscale-observe
    environment:
      TS_HOSTNAME: observe
      TS_STATE_DIR: /var/lib/tailscale
      TS_SERVE_CONFIG: /config/serve.json
      TS_USERSPACE: "true"
      TS_AUTH_ONCE: "true"
    volumes:
      - ../data/tailscale-observe:/var/lib/tailscale
      - ./tailscale/observe-serve.json:/config/serve.json:ro
    networks: [platform, egress]
    depends_on: [openobserve]
    restart: unless-stopped
    logging: *logging
```

There is no healthcheck on `openobserve`. The image is distroless (no `sh`, `wget` or `curl`), so a command healthcheck cannot run. Idle memory was about 128 MB with these limits.

And in `networks:`:

```yaml
  telemetry:
    name: platform-telemetry
    internal: true
```

Pin text: `OPENOBSERVE_VERSION=v1.0.4` in `deploy/.env.example`. Pin an exact tag, like the other images. Dependabot does not track it, so bump it by hand.

### 1.2 `deploy/tailscale/observe-serve.json`

Same shape as `secrets-serve.json`:

```json
{
  "TCP": {
    "443": { "HTTPS": true }
  },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": { "/": { "Proxy": "http://openobserve:5080" } }
    }
  }
}
```

### 1.3 `deploy/env/` examples

- `deploy/env/.openobserve.example`:
  ```
  ZO_ROOT_USER_EMAIL=you@example.com
  ZO_ROOT_USER_PASSWORD=
  ```
- `deploy/env/.tailscale-observe.example`:
  ```
  TS_AUTHKEY=
  TS_EXTRA_ARGS=--advertise-tags=tag:observe
  ```

### 1.4 `tailscale/policy.hujson`

- Add `"tag:observe": ["autogroup:admin"]` to `tagOwners`.
- Add no grant. Admins already reach every node. No one else needs the UI.
- Add `tag:observe:443` to the `deny` list of the existing `tag:ci` test, so that a CI runner can never read traces.

### 1.5 `scripts/nas-setup.sh`

The script follows its existing patterns (`write_file`, `log`, the "only if missing" checks). It now:

1. Creates `$P/data/openobserve` (owned by 568:568, the user OpenObserve runs as) and `$P/data/tailscale-observe`, next to the other data dirs.
2. Asks for a third Tailscale auth key, tagged `tag:observe`, only when `.envs/.tailscale-observe` does not exist, and writes that file.
3. Writes `.envs/.openobserve` with a generated `ZO_ROOT_USER_PASSWORD`. The root login email is the admin email from `deploy/apps.yml`, read with the reused `apps_admin_email` helper.
4. Adds `platform-telemetry` to `shared_networks` in `deploy/apps.yml`, on first write and on existing files (it appends the line when it is missing).
5. Restarts the deployer when `deploy/apps.yml` changed, so that it reads the new `shared_networks`.
6. Starts OpenObserve and its Tailscale node with the rest of the stack (`compose up -d --remove-orphans`).
7. Prints the UI URL (`https://observe.<tailnet>.ts.net`) and how to read the root password from `.envs/.openobserve`.

Without step 4, the deployer's compose policy rejects any app that joins `platform-telemetry` (`deployer/policy.py`, external networks must be in `shared_networks`).

### 1.6 Ingestion credential

OTLP/HTTP ingestion into OpenObserve uses HTTP Basic auth at `http://openobserve:5080/api/default/v1/traces`. Facts verified on v1.0.4:

- The open source edition has only the admin role. Creating a `member` user returns 400.
- Each user has an ingestion passcode. `GET /api/default/passcode` with that user's email and password returns it.
- `email:passcode` can ingest (200) but cannot search (401). So the credential in the apps cannot read traces.

Create it once, after the first `nas-setup` with OpenObserve. The four steps (add an `ingest@<your domain>` user in the UI, read its passcode, build the header value, set the two keys in each app's Infisical project) are in the [README section "Request observability"](../README.md#request-observability). Each app stores the header value in its own Infisical project, not in this repository.

### 1.7 Other platform touches

- `scripts/nas-stats.sh`: nothing to change. The `platform` project already includes the new containers.
- `deploy/backup/`: nothing. OpenObserve data is not backed up.
- `README.md`: add OpenObserve to the overview and the one-time setup, and link this plan.
- `scripts/nas-lockdown.sh`: check that it does not need to know about the new Tailscale node.

### 1.8 Retention and storage

- `ZO_COMPACT_DATA_RETENTION_DAYS=30` deletes data older than 30 days. The minimum OpenObserve accepts is 3. It can also be set per stream in the UI. Keep the global value as the source of truth.
- Storage estimate: `requests per day x average span size x 30 / compression ratio`.
  - Example: 10,000 requests per day, about 20 KB per span with bodies, about 10x compression on Parquet. That gives about 20 MB per day and about 600 MB for 30 days.
  - Measure the real size after one week with `du -sh /mnt/svd/platform/data/openobserve`.

## Part 2: app changes (pinguei as the example)

Pinguei facts this section relies on (repository `pinguei-br/pinguei`):

- Django 4.2 with DRF, psycopg 3, served by gunicorn with 3 sync workers and no `--preload`.
- WSGI entry point `pinguei_backend.wsgi:application`, settings `pinguei_backend.settings`.
- Health check at `/api/health/`.
- Attachments upload as multipart.

### 2.1 Dependencies (`requirements.txt`)

```
opentelemetry-sdk==1.45.1
opentelemetry-exporter-otlp-proto-http==1.45.1
opentelemetry-instrumentation-django==0.66b1
opentelemetry-instrumentation-psycopg==0.66b1
```

- Use the `proto-http` exporter, not the gRPC one. It avoids the `grpcio` wheel, which is large and uses more memory.
- Do not install `opentelemetry-distro` or run `opentelemetry-bootstrap`. They pull in every instrumentation they find. Only these two instrumentations are wanted.

### 2.2 The `observability` package

Package `pinguei_backend/observability/`, with three files:

- `body_capture.py`: pure functions `is_text_content` and `body_attributes`, and the constants `MAX_BODY_BYTES`, `REQUEST` and `RESPONSE`. Only text bodies are recorded, truncated to 8 KB.
- `tracing.py`: `tracing_enabled(environ)`, `setup_tracing(environ) -> bool` and `_install_tracing()`. The OpenTelemetry imports are lazy, inside `_install_tracing()`, so the package costs nothing when tracing is off.
- `middleware.py`: `RequestBodyTracingMiddleware`. It skips the request body when it exceeds `DATA_UPLOAD_MAX_MEMORY_SIZE`, by catching `RequestDataTooBig`. It skips multipart uploads, streaming responses and binary content before touching the body, so files are never read into memory just for tracing.

Read the code in those files. It is not repeated here.

### 2.3 Starting the SDK in `wsgi.py`

There is no `gunicorn.conf.py`. `pinguei_backend/wsgi.py` calls `setup_tracing(os.environ)` before `get_wsgi_application()`:

```python
setup_tracing(os.environ)
application = get_wsgi_application()
```

Gunicorn runs without `--preload`, so each worker imports `wsgi.py` after the fork. The export thread therefore lives in the worker. A thread created in the master would not survive the fork. The SDK's TracerProvider flushes the queue at exit through `atexit`. If `--preload` is ever added, this breaks. Revisit then.

Do not use `opentelemetry-instrument gunicorn ...`. It starts the SDK in the master, before the fork.

### 2.4 The middleware

`RequestBodyTracingMiddleware` goes first in `MIDDLEWARE` in `pinguei_backend/settings.py`. `DjangoInstrumentor` inserts its own middleware at index 0 when it runs, so the span already exists when this middleware runs. Being first, this middleware also sees the final response after every other middleware has changed it.

It writes two custom attributes, `http.request.body` and `http.response.body`, plus a `.size` attribute for each. They are not OpenTelemetry semantic convention names. Keep the same names in every app so queries work across apps.

### 2.5 Environment: compose and Infisical

The non-secret settings live in the `environment:` of the `api` service in the app's `deploy/compose.yml`:

```
OTEL_SERVICE_NAME=pinguei-api
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=none
OTEL_LOGS_EXPORTER=none
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_TRACES_COMPRESSION=none
OTEL_BSP_SCHEDULE_DELAY=15000
OTEL_BSP_MAX_EXPORT_BATCH_SIZE=512
OTEL_BSP_MAX_QUEUE_SIZE=4096
OTEL_BSP_EXPORT_TIMEOUT=10000
OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST=.*
OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE=.*
OTEL_PYTHON_DJANGO_EXCLUDED_URLS=api/health/
```

- Headers become span attributes such as `http.request.header.user_agent`.
- The health check is excluded, or it adds thousands of spans per day per app with no value.

Only two keys go in `.envs/.production/.django.example` and in Infisical (environment `prod`, folder `django`):

```
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://openobserve:5080/api/default/v1/traces
OTEL_EXPORTER_OTLP_TRACES_HEADERS=Authorization=Basic%20<base64 of ingest-email:passcode>
```

- The space after `Basic` must be URL-encoded as `%20`.
- An empty endpoint turns tracing off. **Rollback without a code change:** delete the endpoint in Infisical and redeploy. `setup_tracing()` then does nothing and the middleware skips every request.

### 2.6 `deploy/compose.yml` (pinguei)

The `platform-telemetry` network goes on the `api` service only, as an external network:

```yaml
  platform-telemetry:
    external: true
    name: platform-telemetry
```

Only `api` calls `setup_tracing()` (through `wsgi.py`). `worker`, `beat` and `migrate` never call it, so they stay uninstrumented.

Known noise: the health check is excluded from the Django spans, but the psycopg instrumentation still records its database queries (about 4 short spans per check, every 30 s) as root spans with no parent request. That is roughly 11,000 small spans per day per app. Filter them out in queries with `span_kind = '2'`, or drop `PsycopgInstrumentor` if they cost too much.

### 2.7 Tests

Tests are in `support/tests/test_observability_*.py`, because CI runs `manage.py test support`. They cover the pure functions, `setup_tracing` doing nothing when the endpoint is empty, and the middleware (JSON bodies, truncation, multipart skipped, binary request skipped, streaming skipped, oversized request body, no active span). The non-UTF-8 case is tested on `body_attributes`. The existing suite must pass with no OpenTelemetry variables set.

### 2.8 Checklist for another Django app

1. Add the four pinned packages to `requirements.txt`.
2. Add the `observability` package and the tests.
3. Call `setup_tracing(os.environ)` in `wsgi.py` before `get_wsgi_application()`.
4. Put `RequestBodyTracingMiddleware` first in `MIDDLEWARE`.
5. Add the `OTEL_*` settings to the `api` service's `environment:` and the `platform-telemetry` network to `api` and `networks:`.
6. Add the endpoint and headers keys to the env template, then run `make secrets APP=<app>` here and fill in the values in Infisical.
7. Release. The deployer runs it as usual.
8. Open `https://observe.<tailnet>.ts.net`, choose Traces, and filter by `service_name = 'pinguei-api'`.

### 2.9 Porting to financas and games

Same files and steps as pinguei.

- **financas**: `config.wsgi`, settings `config.settings.settings`, `--max-requests 1000`, and statement PDF uploads (multipart, skipped). Use `OTEL_SERVICE_NAME=financas-api`.
- **games**: Django, `OTEL_SERVICE_NAME=games-api`. Check its upload routes.
- Check each app's gunicorn command for `--preload` first.

## Part 3: using the data

OpenObserve flattens span attributes into columns. Dots become underscores, for example `http.route` becomes `http_route`. The exact names depend on the semantic convention version of the instrumentation, so check the column names in the UI once. `span_kind` is numeric: server spans are `'2'`. `duration` is in microseconds. Example queries on the traces stream:

Requests per hour per app and route:

```sql
SELECT histogram(_timestamp, '1 hour') AS hour, service_name, http_route, count(*) AS requests
FROM "default"
WHERE span_kind = '2'
GROUP BY hour, service_name, http_route
ORDER BY hour
```

Slowest routes in the last day (p95, in milliseconds):

```sql
SELECT service_name, http_route, approx_percentile_cont(duration, 0.95) / 1000 AS p95_ms, count(*) AS requests
FROM "default"
WHERE span_kind = '2'
GROUP BY service_name, http_route
ORDER BY p95_ms DESC
LIMIT 20
```

Server errors with their bodies:

```sql
SELECT _timestamp, service_name, http_method, http_target, http_status_code, http_request_body, http_response_body
FROM "default"
WHERE span_kind = '2' AND http_status_code >= 500
ORDER BY _timestamp DESC
```

Build one dashboard with: requests per minute per app, error rate per app, p95 latency per route, and a table of the latest 5xx responses. OpenObserve alerts can post to the existing ntfy topic later. That is out of scope for this plan.

## Security

**Nothing is masked.** OpenObserve will hold, in plain text, for 30 days:

- `Authorization` headers with JWT access tokens, and `Cookie` and `Set-Cookie` headers;
- passwords from `/api/auth/login/`, `/api/auth/register/`, `/api/me/password/` and the backoffice login;
- access and refresh tokens in the login and refresh responses;
- every financial value that financas returns.

Controls that stay in place:

- The UI is reachable only through the `observe` Tailscale node, and only admins reach it.
- `platform-telemetry` is internal. Only the API containers and OpenObserve join it.
- The root and ingestion credentials live in `.envs/.openobserve` and Infisical, never in git.
- Deleting the data dir removes everything at once.

Cheap improvements, if wanted later, in order of cost:

1. Skip body capture for the auth routes. This is one prefix check per request, with no body parsing:
   ```python
   SKIP_BODY_PATHS = ("/api/auth/", "/api/me/password/")
   if request.path.startswith(SKIP_BODY_PATHS): ...
   ```
2. Drop the `Authorization` and `Cookie` headers with `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS=authorization,cookie,set-cookie`. The instrumentation replaces their values with `[REDACTED]` and keeps the header names.
3. Mask JSON fields by key (`password`, `token`, `access`, `refresh`). This parses each JSON body, so it costs the most.

## Rollout order

1. Platform PR in this repository: sections 1.1 to 1.7, plus the README.
2. Tailscale admin console: add `tag:observe`, create its auth key, merge the policy fragment.
3. `make nas-setup NAS_HOST=user@host`. Check the UI opens.
4. Create the ingestion passcode and set the two Infisical keys in pinguei (see the README).
5. Pinguei release.
6. After one week: run `make nas-stats` and `du -sh` on the data dir, and compare with the targets. Adjust the memory limits, `OTEL_BSP_*` and the OpenObserve memory settings.
7. Financas and games.

## Open questions

- Should the Celery worker in financas be traced later? It would need `opentelemetry-instrumentation-celery` and a `worker_process_init` signal handler. It is out of scope for now ("backends only, HTTP requests").
