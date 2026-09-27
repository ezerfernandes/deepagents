######################
# FORK-OWNED TARGETS
######################
# The upstream Makefile includes this file through one marked line. Fork
# targets live here, so merges of upstream Makefile changes stay clean.

.PHONY: complexity complexity-report

# McCabe cyclomatic complexity per function, measured with ruff's C901 rule.
# The repository lint config ignores C90, so these targets select it
# explicitly. 10 is McCabe's recommended ceiling and ruff's default.
MAX_COMPLEXITY ?= 10
COMPLEXITY_FILES ?= deepagents_code

complexity: ## Fail if a function's cyclomatic complexity exceeds MAX_COMPLEXITY (default 10)
	uv run --all-groups ruff check --select C901 --output-format concise \
		--config "lint.mccabe.max-complexity = $(MAX_COMPLEXITY)" $(COMPLEXITY_FILES)

complexity-report: ## List each function's cyclomatic complexity, highest first
	@uv run --all-groups ruff check --select C901 --output-format concise \
		--config "lint.mccabe.max-complexity = 0" $(COMPLEXITY_FILES) \
		| sed -nE 's/^([^:]+:[0-9]+):[0-9]+: [^:]+: `([^`]+)` is too complex \(([0-9]+) > 0\)$$/\3  \2  \1/p' \
		| sort -k1,1nr -k3,3
