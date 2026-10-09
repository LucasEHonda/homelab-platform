import logging
import os
from pathlib import Path

import uvicorn

from deployer.api import create_app
from deployer.auth import GitHubOidcVerifier
from deployer.breakglass import BreakGlassService
from deployer.config import load_config
from deployer.notify import NtfyNotifier
from deployer.runner import Docker, SubprocessRunner
from deployer.secrets import InfisicalClient
from deployer.service import DeploymentService, InfisicalSecretsSource


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(Path(os.environ.get("DEPLOYER_CONFIG", "/config/apps.yml")))
    history = Path(os.environ.get("DEPLOYER_HISTORY", "/data/deployments.jsonl"))
    docker = Docker(SubprocessRunner())
    notifier = NtfyNotifier(os.environ.get(config.ntfy_url_env))
    secrets = InfisicalSecretsSource(InfisicalClient(config.infisical_url), os.environ)
    deployments = DeploymentService(config, docker, secrets, notifier, history)
    break_glass = BreakGlassService(config, docker, notifier)
    break_glass.lock_all()
    uvicorn.run(create_app(config, GitHubOidcVerifier(), deployments, break_glass), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), proxy_headers=False, access_log=True)


if __name__ == "__main__":
    main()
