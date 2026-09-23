SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help

VENV ?= $(shell pwd)/.venv
UV ?= uv
UVX ?= uvx
UV_RUN ?= VIRTUAL_ENV=$(VENV) PATH=$(VENV)/bin:$$PATH

DATA_DIR ?= data
DETAILS_DIR ?= $(DATA_DIR)/details
LOG_CSV ?= $(DATA_DIR)/log.csv
OUT_DIR ?= out
SAT ?= PROVES_Electra
FIT ?= dM,dn

# Unattended pipeline data root (external USB NVMe; see README "Continuous operation").
DATA_ROOT ?= /Volumes/nvme-1tb-m4/proves/tinygs
OPS_ENV = TINYGS_DATA_ROOT=$(DATA_ROOT)

##@ Setup

.PHONY: setup
setup: $(VENV)/.stamp ## Create the venv and install deps + Playwright Chromium

$(VENV)/.stamp: requirements.txt
	@$(UV) venv $(VENV) --allow-existing
	@VIRTUAL_ENV=$(VENV) $(UV) pip install --requirement requirements.txt
	@$(UV_RUN) playwright install chromium
	@touch $@

.PHONY: clean
clean: ## Remove venv, pipeline data, and fit output (refuses paths outside the repo)
	@repo=$$(pwd -P); \
	for d in "$(DATA_DIR)" "$(OUT_DIR)"; do \
	  case "$$d" in /*) p="$$d" ;; *) p="$$repo/$$d" ;; esac; \
	  parent=$$(cd "$$(dirname "$$p")" 2>/dev/null && pwd -P) || parent=""; \
	  if [ -z "$$parent" ]; then real=""; elif [ -d "$$p" ]; then real=$$(cd "$$p" && pwd -P); else real="$$parent/$$(basename "$$p")"; fi; \
	  case "$$real" in \
	    "$$repo"/?*) ;; \
	    "") [ ! -e "$$p" ] || { echo "clean: cannot resolve $$d" >&2; exit 1; } ;; \
	    *) echo "clean: refusing to delete $$d -> $$real (outside $$repo)" >&2; exit 1 ;; \
	  esac; \
	done
	rm -rf $(VENV) $(DATA_DIR) $(OUT_DIR)

##@ Development

.PHONY: pre-commit-install
pre-commit-install: ## Install pre-commit hooks
	@$(UVX) pre-commit install > /dev/null

.PHONY: test
test: ## Run the unit tests
	$(VENV)/bin/python -m unittest discover -s tests -v

.PHONY: fmt
fmt: pre-commit-install ## Lint and format files
	@$(UVX) pre-commit run --all-files

##@ Pipeline

.PHONY: fetch
fetch: setup ## Fetch one packet window from TinyGS and append new frames to the CSV log
	@mkdir -p $(DATA_DIR)
	$(UV_RUN) python3 tinygs_tle/tinygs_fetch.py --sat $(SAT) --out $(DATA_DIR)/tinygs_packets_latest.json
	$(UV_RUN) python3 tinygs_tle/proves_track.py $(DATA_DIR)/tinygs_packets_latest.json $(LOG_CSV)

.PHONY: details
details: setup ## Rate-limited fetch of per-packet detail JSON (1 page/min, max 25/run)
	$(UV_RUN) python3 tinygs_tle/tinygs_details_batch.py --log $(LOG_CSV) --details-dir $(DETAILS_DIR)

.PHONY: tle
tle: setup ## Fit a TLE from the collected detail JSON (override DETAILS_DIR to point elsewhere)
	@mkdir -p $(OUT_DIR)
	$(UV_RUN) python3 tinygs_tle/fit_tle.py --details-dir $(DETAILS_DIR) --out $(OUT_DIR) --fit $(FIT)

.PHONY: all
all: setup fetch details tle ## Full pipeline: setup -> fetch -> details -> tle

##@ Continuous operation (see README; launchd runs these same scripts)

.PHONY: cycle
cycle: ## One unattended cycle: fetch + archive + track every enabled satellite into DATA_ROOT
	$(OPS_ENV) scripts/cycle.sh

.PHONY: details-all
details-all: ## One rate-limited detail batch for all enabled satellites in DATA_ROOT
	$(OPS_ENV) scripts/details.sh

.PHONY: daily
daily: ## CelesTrak TLE refresh + TLE fit (when enough new details) in DATA_ROOT
	$(OPS_ENV) scripts/daily.sh
