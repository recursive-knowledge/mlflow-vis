SHELL := /bin/bash
.DEFAULT_GOAL := help

# env/server.env is the single source of configuration for this box. It holds
# database passwords and the tunnel token and is NEVER shared — teammates and
# nodes get the far smaller env/nodes/<node>.env instead (make node-provision).
SERVER_ENV := env/server.env

ifneq (,$(wildcard $(SERVER_ENV)))
include $(SERVER_ENV)
export
endif

REPO := $(shell pwd)
UV   := $(shell command -v uv 2>/dev/null || echo $$HOME/.local/bin/uv)

COMPOSE   := docker compose --env-file $(SERVER_ENV)
DC_ALL    := docker compose --env-file $(SERVER_ENV) --profile studio --profile tunnel
# One-off container on the compose network, for scripts that must reach
# storage-api or the database by service name.
DC_RUN    := docker compose --env-file $(SERVER_ENV) run --rm --no-deps \
               -v "$(REPO)/scripts:/opt/scripts:ro" \
               -v "$(REPO)/$(SERVER_ENV):/opt/server.env:ro" \
               -e RK_ENV_FILE=/opt/server.env

# ---------------------------------------------------------------------------
##@ Setup

.PHONY: help
help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nrk-mlflow — RL training coordination stack\n\n"} \
	  /^##@/{printf "\n\033[1m%s\033[0m\n", substr($$0,5)} \
	  /^[a-zA-Z_-]+:.*?##/{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

.PHONY: install
install: ## One command to reproduce the whole server setup
	@command -v uv >/dev/null 2>&1 || test -x "$(UV)" || { \
	  echo "installing uv…"; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	@test -f $(SERVER_ENV) || { cp env/server.env.example $(SERVER_ENV); \
	  echo "created $(SERVER_ENV) from the template"; }
	@chmod 600 $(SERVER_ENV)
	@$(UV) sync --frozen --group ops
	@$(UV) run python scripts/gen-secrets.py
	@bash scripts/fetch-supabase.sh
	@$(MAKE) --no-print-directory dirs
	@$(COMPOSE) build
	@echo ""
	@echo "installed. next:  make up  (then: make bucket && make smoke)"

.PHONY: dirs
dirs: ## Create the data directories under RK_DATA_ROOT
	@test -n "$(RK_DATA_ROOT)" || { echo "RK_DATA_ROOT unset — run: make install"; exit 1; }
	@mkdir -p "$(RK_DATA_ROOT)"/{supabase/db,supabase/storage,checkpoints,backups/daily,backups/weekly,backups/logs}
	@# /overflow is a shared pool and inherits world-readable default ACLs, so
	@# lock the root down here. Children are owned by container UIDs after
	@# first start and can no longer be chmod'ed from this account.
	@chmod 750 "$(RK_DATA_ROOT)" 2>/dev/null || true
	@echo "data root ready: $(RK_DATA_ROOT)"

.PHONY: check-env
check-env: ## Verify env/server.env is present and complete
	@test -f $(SERVER_ENV) || { echo "no $(SERVER_ENV) — run: make install"; exit 1; }
	@for v in RK_DATA_ROOT POSTGRES_PASSWORD MLFLOW_DB_PASSWORD JWT_SECRET \
	          ANON_KEY SERVICE_ROLE_KEY S3_PROTOCOL_ACCESS_KEY_ID \
	          S3_PROTOCOL_ACCESS_KEY_SECRET; do \
	  test -n "$${!v}" || { echo "$$v is empty — run: make install"; exit 1; }; \
	done
	@test -f supabase/upstream/db/roles.sql || { \
	  echo "supabase assets not vendored — run: bash scripts/fetch-supabase.sh"; exit 1; }
	@echo "env OK"

# ---------------------------------------------------------------------------
##@ Stack

.PHONY: up
up: check-env dirs ## Start the core stack (db, storage, mlflow)
	$(COMPOSE) up -d --build --wait
	@$(MAKE) --no-print-directory health

.PHONY: up-tunnel
up-tunnel: check-env dirs ## Start core + the public Cloudflare tunnel
	@test -n "$(CLOUDFLARE_TUNNEL_TOKEN)" || { \
	  echo "CLOUDFLARE_TUNNEL_TOKEN is empty — see cloudflare/README.md"; exit 1; }
	$(COMPOSE) --profile tunnel up -d --build --wait
	@$(MAKE) --no-print-directory health

.PHONY: studio
studio: check-env ## Start the Supabase admin console (loopback only)
	$(COMPOSE) --profile studio up -d
	@echo "studio: http://$(STUDIO_BIND_ADDR):$(STUDIO_PORT)  (ssh -L $(STUDIO_PORT):localhost:$(STUDIO_PORT) $$(hostname))"
	@echo "login:  $(DASHBOARD_USERNAME) / see DASHBOARD_PASSWORD in $(SERVER_ENV)"

.PHONY: down
down: ## Stop everything (data is preserved)
	$(DC_ALL) down

.PHONY: restart
restart: ## Restart services
	$(DC_ALL) restart

.PHONY: ps
ps: ## Show service status
	$(DC_ALL) ps

.PHONY: logs
logs: ## Tail logs (make logs S=mlflow for one service)
	$(DC_ALL) logs -f --tail=100 $(S)

.PHONY: bucket
bucket: ## Create the MLflow artifact bucket in Supabase Storage
	@$(DC_RUN) -e S3_ENDPOINT=http://storage:5000/s3 \
	  mlflow python /opt/scripts/bootstrap-bucket.py

# ---------------------------------------------------------------------------
##@ Operations

.PHONY: health
health: ## Check tracking server, database, storage, tunnel, disk headroom
	@bash scripts/health.sh

.PHONY: psql
psql: ## psql shell on the MLflow database (make psql DB=postgres for Supabase's)
	$(COMPOSE) exec db psql -U postgres -d $(or $(DB),mlflow)

.PHONY: usage
usage: ## Show what the data root is consuming
	@du -sh "$(RK_DATA_ROOT)"/* 2>/dev/null | sort -h
	@echo "---"
	@df -h "$(RK_DATA_ROOT)" | tail -1

.PHONY: prune
prune: ## Apply checkpoint retention (dry run unless APPLY=1)
	@bash scripts/prune-checkpoints.sh

# ---------------------------------------------------------------------------
##@ Backups

.PHONY: backup
backup: ## Dump now: make backup TIER=daily|weekly
	@bash scripts/backup-postgres.sh $(or $(TIER),daily)

.PHONY: backup-list
backup-list: ## List retained dumps in both tiers
	@for t in daily weekly; do \
	  echo "$$t:"; \
	  ls -lh "$(RK_DATA_ROOT)/backups/$$t"/cluster-*.sql.gz 2>/dev/null \
	    | awk '{print "  " $$5 "  " $$9}' || echo "  (none)"; \
	done

.PHONY: backup-cron-enable
backup-cron-enable: ## Turn on scheduled daily + weekly backups
	@bash scripts/backup-cron.sh enable

.PHONY: backup-cron-disable
backup-cron-disable: ## Turn off scheduled backups (dumps are kept)
	@bash scripts/backup-cron.sh disable

.PHONY: backup-cron-status
backup-cron-status: ## Show whether scheduled backups are active
	@bash scripts/backup-cron.sh status

.PHONY: restore
restore: ## Restore from a dump: make restore F=/path/to/cluster-*.sql.gz
	@test -n "$(F)" || { echo "usage: make restore F=<cluster-*.sql.gz>"; exit 1; }
	@bash scripts/restore-postgres.sh "$(F)"

# ---------------------------------------------------------------------------
##@ Nodes & teammates

.PHONY: node-provision
node-provision: ## Build a shareable credential bundle: make node-provision NODE=julius
	@test -n "$(NODE)" || { echo "usage: make node-provision NODE=<name>"; exit 1; }
	@bash scripts/node-provision.sh "$(NODE)"

.PHONY: node-authorize
node-authorize: ## Grant a provisioned node rsync access over SSH (asks first)
	@test -n "$(NODE)" || { echo "usage: make node-authorize NODE=<name>"; exit 1; }
	@bash scripts/node-authorize.sh "$(NODE)"

.PHONY: node-list
node-list: ## List provisioned node bundles
	@ls -1 env/nodes/*.env 2>/dev/null | sed 's|env/nodes/|  |;s|\.env$$||' || echo "  (none — make node-provision NODE=<name>)"

# ---------------------------------------------------------------------------
##@ Tests

.PHONY: smoke
smoke: ## Log a run over loopback — proves db + storage are wired up
	@$(UV) run python scripts/smoke-test.py

.PHONY: test-tunnel
test-tunnel: ## Log a run through the Cloudflare tunnel as a node would
	@test -n "$(NODE)" || { echo "usage: make test-tunnel NODE=<name>"; exit 1; }
	@$(UV) run python scripts/test-tunnel.py "$(NODE)"

.PHONY: test
test: smoke ## Run every check that does not need the tunnel
	@bash scripts/health.sh
