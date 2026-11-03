# Common tasks for Ella. `make` alone lists them.

EXTENSION := editors/vscode
FILE ?= example_syntax.ella

# Where VS Code looks for extensions: the server's directory under WSL or SSH, else the desktop's.
VSCODE_EXTENSIONS ?= $(if $(wildcard $(HOME)/.vscode-server),$(HOME)/.vscode-server/extensions,$(HOME)/.vscode/extensions)

.DEFAULT_GOAL := help
.PHONY: help sync test lint format format-check typecheck check run tree native asm ir \
	lsp extension install-extension uninstall-extension package clean

help: ## List the recipes
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

# Python

sync: ## Install Python dependencies, the language server's too
	uv sync --all-extras

test: ## Run the tests
	uv run pytest

lint: ## Lint
	uv run ruff check

format: ## Format the code
	uv run ruff format

format-check: ## Check the formatting without changing anything
	uv run ruff format --check

typecheck: ## Strict type checking of src/ and test/
	uv run mypy

check: lint format-check typecheck test ## Everything CI would run

# Running programs (FILE=path/to/program.ella, default the syntax tour)

run: ## Run FILE through the IR
	uv run ella $(FILE)

tree: ## Run FILE with the tree-walking interpreter
	uv run ella --tree $(FILE)

native: ## Compile FILE to native code and run it (needs gcc)
	uv run ella --native -O $(FILE)

asm: ## Print FILE's x64 assembly
	uv run ella --asm $(FILE)

ir: ## Print FILE's optimized IR
	uv run ella --ir -O $(FILE)

# Editor support

lsp: sync extension ## Build the language server and the VS Code extension that starts it

extension: $(EXTENSION)/node_modules ## Install the VS Code extension's packages

$(EXTENSION)/node_modules: $(EXTENSION)/package.json $(EXTENSION)/package-lock.json
	cd $(EXTENSION) && npm install --no-fund --no-audit
	touch $@

install-extension: lsp ## Link the extension into VS Code (then run Developer: Reload Window)
	mkdir -p $(VSCODE_EXTENSIONS)
	ln -sfn $(abspath $(EXTENSION)) $(VSCODE_EXTENSIONS)/ella

uninstall-extension: ## Remove the link to the extension
	rm -f $(VSCODE_EXTENSIONS)/ella

package: extension ## Package the extension as dist/ella.vsix, for `code --install-extension`
	mkdir -p dist
	cd $(EXTENSION) && npx --yes @vscode/vsce package --allow-missing-repository --skip-license --no-rewrite-relative-links --out ../../dist/ella.vsix

clean: ## Remove caches, build output and the extension's packages
	rm -rf dist .pytest_cache .ruff_cache .mypy_cache $(EXTENSION)/node_modules
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
