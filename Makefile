.PHONY: test nas-setup lockdown unlock nas-stats nas-provision nas-migrate

test:
	docker compose -f compose.dev.yml run --rm --build test

NAS_HOST ?=
PLATFORM_DIR ?= /mnt/svd/platform
REPOS ?= my-games-hub-br/my-games-hub pinguei-br/pinguei my-personal-finances-br/accounting_administrator
DEPLOYER_VERSION ?= $(shell git describe --tags --abbrev=0 2>/dev/null)
# name,repository,app dir,image prefix (the repository id is looked up with gh).
APPS ?= games,my-games-hub-br/my-games-hub,/mnt/svd/games,ghcr.io/my-games-hub-br/ pinguei,pinguei-br/pinguei,/mnt/svd/pinguei,ghcr.io/pinguei-br/ financas,my-personal-finances-br/accounting_administrator,/mnt/svd/financas,ghcr.io/my-personal-finances-br/
GENERATE ?= games:mysql=MYSQL_READONLY_PASSWORD,MYSQL_BREAKGLASS_PASSWORD pinguei:mysql=MYSQL_READONLY_PASSWORD,MYSQL_BREAKGLASS_PASSWORD financas:db-users=POSTGRES_READONLY_PASSWORD,POSTGRES_BREAKGLASS_PASSWORD

# Installs or updates the platform on the NAS: secrets, Infisical import, deployer. Safe to re-run.
nas-setup:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(if $(DEPLOYER_VERSION),,$(error set DEPLOYER_VERSION=vX.Y.Z))
	tar czf - --exclude=deploy/apps.yml --exclude=deploy/.env deploy scripts | ssh $(NAS_HOST) "rm -rf /tmp/platform-setup && mkdir -p /tmp/platform-setup && tar xzf - -C /tmp/platform-setup"
	specs=""; for app in $(APPS); do repo=$$(echo "$$app" | cut -d, -f2); id=$$(gh api "repos/$$repo" --jq .id); specs="$$specs $$app,$$id"; done; \
	ssh -t $(NAS_HOST) "sudo DEPLOYER_VERSION=$(DEPLOYER_VERSION) GENERATE='$(GENERATE)' sh /tmp/platform-setup/scripts/nas-setup.sh /tmp/platform-setup $(PLATFORM_DIR)$$specs; rm -rf /tmp/platform-setup"

# Emergency: cuts the deploy path. NAS first (stops the deployer and the Tailscale nodes of the
# deployer and Infisical; apps keep running), then GitHub (disables the deploy workflow and deletes
# the Tailscale credential secret). Revoke the Tailscale trust credential in the admin console too.
lockdown:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	ssh $(NAS_HOST) "cat > /tmp/nas-lockdown.sh" < scripts/nas-lockdown.sh
	ssh -t $(NAS_HOST) "sudo sh /tmp/nas-lockdown.sh $(PLATFORM_DIR) on; rm -f /tmp/nas-lockdown.sh"
	for repo in $(REPOS); do \
		gh workflow disable deploy.yml --repo "$$repo" || true; \
		gh secret delete TS_OAUTH_CLIENT_ID --repo "$$repo" || true; \
	done
	@echo "==> now revoke the credential: https://login.tailscale.com/admin/settings/trust-credentials"

# Lifts a lockdown: NAS back on, workflows enabled, Tailscale credential asked again.
unlock:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	ssh $(NAS_HOST) "cat > /tmp/nas-lockdown.sh" < scripts/nas-lockdown.sh
	ssh -t $(NAS_HOST) "sudo sh /tmp/nas-lockdown.sh $(PLATFORM_DIR) off; rm -f /tmp/nas-lockdown.sh"
	for repo in $(REPOS); do gh workflow enable deploy.yml --repo "$$repo"; done
	scripts/github-setup.sh $(REPOS)

# Read-only snapshot of memory and CPU per container, plus Redis and database memory. Changes nothing.
nas-stats:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	ssh $(NAS_HOST) "cat > /tmp/nas-stats.sh" < scripts/nas-stats.sh
	ssh -t $(NAS_HOST) "sudo sh /tmp/nas-stats.sh; rm -f /tmp/nas-stats.sh"

# Creates one app's database, roles, credentials and Redis user on the shared data services. Safe to re-run.
nas-provision:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(if $(APP),,$(error set APP=games|pinguei|financas))
	ssh $(NAS_HOST) "cat > /tmp/nas-provision-app-data.sh" < scripts/nas-provision-app-data.sh
	ssh -t $(NAS_HOST) "sudo PLATFORM_DIR=$(PLATFORM_DIR) sh /tmp/nas-provision-app-data.sh $(APP); rm -f /tmp/nas-provision-app-data.sh"

# One step of an app's move to the shared database: prepare, load, verify or finish (see docs/shared-data-runbook.md).
nas-migrate:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(if $(APP),,$(error set APP=games|pinguei|financas))
	$(if $(STEP),,$(error set STEP=prepare|load|verify|finish))
	ssh $(NAS_HOST) "cat > /tmp/nas-migrate-app-db.sh" < scripts/nas-migrate-app-db.sh
	ssh -t $(NAS_HOST) "sudo PLATFORM_DIR=$(PLATFORM_DIR) sh /tmp/nas-migrate-app-db.sh $(APP) $(STEP); rm -f /tmp/nas-migrate-app-db.sh"
