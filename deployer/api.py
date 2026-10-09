from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from deployer.auth import AuthError, GitHubOidcVerifier, authorize_deploy, require_admin
from deployer.breakglass import BreakGlassError, BreakGlassService
from deployer.config import PlatformConfig
from deployer.service import (
    ConflictError,
    DeploymentService,
    DeployRequest,
    RequestError,
)


class DeployBody(BaseModel):
    app: str
    version: str
    images: dict[str, str]
    bundle: str


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not header[len("Bearer "):]:
        raise AuthError(401, "bearer token required")
    return header[len("Bearer "):]


def create_app(
    config: PlatformConfig,
    verifier: GitHubOidcVerifier,
    deployments: DeploymentService,
    break_glass: BreakGlassService,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/deployments", status_code=202)
    def create_deployment(body: DeployBody, request: Request):
        identity = verifier.verify(_bearer(request))
        app_config = config.apps.get(body.app)
        if app_config is None:
            raise AuthError(403, "app not allowed")
        authorize_deploy(identity, app_config, config.allowed_workflow_refs)
        try:
            deployment = deployments.submit(
                DeployRequest(body.app, body.version, body.images, body.bundle)
            )
        except RequestError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except ConflictError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return {"id": deployment.id}

    @app.get("/v1/deployments/{deployment_id}")
    def get_deployment(deployment_id: str, request: Request):
        identity = verifier.verify(_bearer(request))
        deployment = deployments.get(deployment_id)
        if deployment is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        authorize_deploy(
            identity, config.apps[deployment.app], config.allowed_workflow_refs
        )
        return {
            "id": deployment.id,
            "app": deployment.app,
            "version": deployment.version,
            "status": deployment.status.value,
            "detail": deployment.detail,
        }

    @app.post("/v1/apps/{app_name}/break-glass")
    def open_break_glass(app_name: str, request: Request):
        user = require_admin(request.headers.get("Tailscale-User-Login"), config.admins)
        try:
            expires = break_glass.open(app_name, user)
        except BreakGlassError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return {"expires_at": expires.isoformat()}

    return app
