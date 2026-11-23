.PHONY: test nas-setup lockdown unlock lockdown-github unlock-github panic unpanic public-off public-on public-status nas-stats nas-provision nas-dozzle secrets

test:
	docker compose -f compose.dev.yml run --rm --build test

NAS_HOST ?=
PLATFORM_DIR ?= /mnt/svd/platform
REPOS ?= my-games-hub-br/my-games-hub pinguei-br/pinguei my-personal-finances-br/accounting_administrator
DEPLOYER_VERSION ?= $(shell git describe --tags --abbrev=0 2>/dev/null)
# name,repository,app dir,image prefix (the repository id is looked up with gh).
APPS ?= games,my-games-hub-br/my-games-hub,/mnt/svd/games,ghcr.io/my-games-hub-br/ pinguei,pinguei-br/pinguei,/mnt/svd/pinguei,ghcr.io/pinguei-br/ financas,my-personal-finances-br/accounting_administrator,/mnt/svd/financas,ghcr.io/my-personal-finances-br/
GENERATE ?=
INFISICAL_URL ?=
REF ?= main
# Every ssh call of a target reuses one connection, so the SSH password is asked once.
SSH = ssh -o ControlMaster=auto -o ControlPath=$(HOME)/.ssh/cm-%r@%h:%p -o ControlPersist=10m
# Domains served to the internet through a Cloudflare Tunnel; public-* and panic act on all of them.
PUBLIC_ZONES ?= meutill.com
# Kill-switch API token: CLOUDFLARE_API_TOKEN, or else this macOS Keychain item.
CF_KEYCHAIN_SERVICE ?= homelab-cloudflare-killswitch
# Host python3 (stdlib only), so the kill switch works without Docker Desktop.
KILLSWITCH = CLOUDFLARE_API_TOKEN="$${CLOUDFLARE_API_TOKEN:-$$(security find-generic-password -s $(CF_KEYCHAIN_SERVICE) -w 2>/dev/null)}" python3 -m deployer.tools.cloudflare_killswitch

# Installs or updates the platform on the NAS: secrets, Infisical import, deployer. Safe to re-run.
nas-setup:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(if $(DEPLOYER_VERSION),,$(error set DEPLOYER_VERSION=vX.Y.Z))
	tar czf - --exclude=deploy/apps.yml --exclude=deploy/.env deploy scripts | $(SSH) $(NAS_HOST) "rm -rf /tmp/platform-setup && mkdir -p /tmp/platform-setup && tar xzf - -C /tmp/platform-setup"
	specs=""; for app in $(APPS); do repo=$$(echo "$$app" | cut -d, -f2); id=$$(gh api "repos/$$repo" --jq .id); specs="$$specs $$app,$$id"; done; \
	$(SSH) -t $(NAS_HOST) "sudo DEPLOYER_VERSION=$(DEPLOYER_VERSION) GENERATE='$(GENERATE)' sh /tmp/platform-setup/scripts/nas-setup.sh /tmp/platform-setup $(PLATFORM_DIR)$$specs; rm -rf /tmp/platform-setup"

# Emergency: cuts the deploy path. NAS first (stops the deployer and the Tailscale nodes of the
# deployer and Infisical; apps keep running), then GitHub (disables the deploy workflow and deletes
# the Tailscale credential secret). Revoke the Tailscale trust credential in the admin console too.
lockdown:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(SSH) $(NAS_HOST) "cat > /tmp/nas-lockdown.sh" < scripts/nas-lockdown.sh
	$(SSH) -t $(NAS_HOST) "sudo sh /tmp/nas-lockdown.sh $(PLATFORM_DIR) on; rm -f /tmp/nas-lockdown.sh"
	$(MAKE) --no-print-directory lockdown-github

# GitHub half of a lockdown: deploy workflows off, Tailscale credential secret deleted.
lockdown-github:
	for repo in $(REPOS); do \
		gh workflow disable deploy.yml --repo "$$repo" || true; \
		gh secret delete TS_OAUTH_CLIENT_ID --repo "$$repo" || true; \
	done
	@echo "==> now revoke the credential: https://login.tailscale.com/admin/settings/trust-credentials"

# Lifts a lockdown: NAS back on, workflows enabled, Tailscale credential asked again.
unlock:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(SSH) $(NAS_HOST) "cat > /tmp/nas-lockdown.sh" < scripts/nas-lockdown.sh
	$(SSH) -t $(NAS_HOST) "sudo sh /tmp/nas-lockdown.sh $(PLATFORM_DIR) off; rc=\$$?; rm -f /tmp/nas-lockdown.sh; exit \$$rc"
	$(MAKE) --no-print-directory unlock-github

# GitHub half of an unlock: deploy workflows on, Tailscale credential asked again.
unlock-github:
	for repo in $(REPOS); do gh workflow enable deploy.yml --repo "$$repo"; done
	scripts/github-setup.sh $(REPOS)

# Panic button: cuts every path from the internet into the NAS, keeps the apps on Tailscale.
# 1. Cloudflare blocks every PUBLIC_ZONES host at the edge (WAF rule; needs no SSH and no Docker).
# 2. NAS: every cloudflared container stopped and kept stopped, then the lockdown (deployer off).
# 3. GitHub: deploy workflows off, Tailscale credential deleted.
# Each step runs even if the one before failed; the summary lists what failed. Lift with `make unpanic`.
panic:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	@failed=""; \
	for zone in $(PUBLIC_ZONES); do $(KILLSWITCH) block --zone "$$zone" || failed="$$failed cloudflare:$$zone"; done; \
	{ $(SSH) $(NAS_HOST) "cat > /tmp/nas-lockdown.sh" < scripts/nas-lockdown.sh && \
	  $(SSH) -t $(NAS_HOST) "sudo sh /tmp/nas-lockdown.sh $(PLATFORM_DIR) panic; rc=\$$?; rm -f /tmp/nas-lockdown.sh; exit \$$rc"; } || failed="$$failed nas"; \
	$(MAKE) --no-print-directory lockdown-github || failed="$$failed github"; \
	if [ -n "$$failed" ]; then echo "==> PANIC INCOMPLETE, failed:$$failed (re-run make panic; last resort: unplug the NAS)"; exit 1; fi; \
	echo "==> PANIC ON: internet blocked at Cloudflare, tunnels stopped, deploy path cut. Apps still on Tailscale."

# Lifts a panic after a typed confirmation: NAS tunnels and deployer back, GitHub deploys back,
# Cloudflare block lifted last.
unpanic:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	@printf 'Open %s to the internet again? Type "unpanic": ' "$(PUBLIC_ZONES)"; read answer; [ "$$answer" = unpanic ] || { echo "aborted"; exit 1; }
	$(SSH) $(NAS_HOST) "cat > /tmp/nas-lockdown.sh" < scripts/nas-lockdown.sh
	$(SSH) -t $(NAS_HOST) "sudo sh /tmp/nas-lockdown.sh $(PLATFORM_DIR) unpanic; rc=\$$?; rm -f /tmp/nas-lockdown.sh; exit \$$rc"
	$(MAKE) --no-print-directory unlock-github
	@for zone in $(PUBLIC_ZONES); do $(KILLSWITCH) unblock --zone "$$zone" || exit 1; done

# Kill switch for the public sites only: blocks or reopens every PUBLIC_ZONES host at Cloudflare's
# edge. Deploys cannot undo it and the Tailscale path is unaffected. The token needs
# `Zone > Zone > Read` and `Zone > Zone WAF > Edit` on those zones only; keep it in the Keychain:
#   security add-generic-password -s homelab-cloudflare-killswitch -a "$USER" -w
public-off:
	@for zone in $(PUBLIC_ZONES); do $(KILLSWITCH) block --zone "$$zone" || exit 1; done

public-on:
	@for zone in $(PUBLIC_ZONES); do $(KILLSWITCH) unblock --zone "$$zone" || exit 1; done

public-status:
	@for zone in $(PUBLIC_ZONES); do $(KILLSWITCH) status --zone "$$zone" || exit 1; done

# Read-only snapshot of memory and CPU per container, plus Redis and database memory. Changes nothing.
nas-stats:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(SSH) $(NAS_HOST) "cat > /tmp/nas-stats.sh" < scripts/nas-stats.sh
	$(SSH) -t $(NAS_HOST) "sudo sh /tmp/nas-stats.sh; rm -f /tmp/nas-stats.sh"

# Installs or updates Dozzle (read-only logs and stats of every container) as a TrueNAS app. Asks for its password.
nas-dozzle:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	sh scripts/dozzle-users.sh $(or $(DOZZLE_USER),admin) > .dozzle-users.yml
	$(SSH) $(NAS_HOST) "umask 077; cat > /tmp/dozzle-users.yml" < .dozzle-users.yml; rc=$$?; rm -f .dozzle-users.yml; exit $$rc
	$(SSH) $(NAS_HOST) "cat > /tmp/nas-dozzle.sh" < scripts/nas-dozzle.sh
	$(SSH) -t $(NAS_HOST) "sudo PLATFORM_DIR=$(PLATFORM_DIR) DOZZLE_PORT=$(or $(DOZZLE_PORT),8888) sh /tmp/nas-dozzle.sh /tmp/dozzle-users.yml; rm -f /tmp/nas-dozzle.sh /tmp/dozzle-users.yml"

# Creates one app's database, roles, credentials and Redis user on the shared data services. Safe to re-run.
nas-provision:
	$(if $(NAS_HOST),,$(error set NAS_HOST=user@host))
	$(if $(APP),,$(error set APP=games|pinguei|financas))
	$(SSH) $(NAS_HOST) "cat > /tmp/nas-provision-app-data.sh" < scripts/nas-provision-app-data.sh
	$(SSH) -t $(NAS_HOST) "sudo PLATFORM_DIR=$(PLATFORM_DIR) sh /tmp/nas-provision-app-data.sh $(APP); rm -f /tmp/nas-provision-app-data.sh"

# Creates the Infisical secrets an app's env templates need and the app's project lacks: generated
# where the template says "# generate: <kind>", asked otherwise. Never changes an existing secret.
# Runs on this machine over Tailscale (no SSH) and asks for an Infisical admin token. Reads the
# templates from REF (default main), so run it with the feature branch before merging.
# CHECK=1 only lists what is missing.
secrets:
	$(if $(INFISICAL_URL),,$(error set INFISICAL_URL=https://secrets.<tailnet>.ts.net))
	$(if $(APP),,$(error set APP=<app name from APPS>))
	@repo=""; for app in $(APPS); do [ "$$(echo "$$app" | cut -d, -f1)" = "$(APP)" ] && repo=$$(echo "$$app" | cut -d, -f2); done; \
	[ -n "$$repo" ] || { echo "unknown APP $(APP)"; exit 1; }; \
	bundle=$$(mktemp -d); trap 'rm -rf "$$bundle"' EXIT; mkdir "$$bundle/envs"; \
	gh api -H "Accept: application/vnd.github.raw" "repos/$$repo/contents/deploy/deploy.yml?ref=$(REF)" > "$$bundle/deploy.yml" || exit 1; \
	for name in $$(gh api "repos/$$repo/contents/.envs/.production?ref=$(REF)" --jq '.[].name | select(endswith(".example"))'); do \
		gh api -H "Accept: application/vnd.github.raw" "repos/$$repo/contents/.envs/.production/$$name?ref=$(REF)" > "$$bundle/envs/$$name" || exit 1; \
	done; \
	docker compose -f compose.dev.yml run --rm -v "$$bundle:/bundle:ro" test \
		python -m deployer.tools.infisical_sync --url $(INFISICAL_URL) --project $(APP) --bundle /bundle $(if $(CHECK),--check)
