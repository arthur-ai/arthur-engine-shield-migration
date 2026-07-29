# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Arthur Engine is a Python-based AI/ML monitoring and governance platform with three main components:

- **GenAI Engine**: FastAPI-based REST API for LLM evaluation and guardrailing
- **ML Engine**: Job-based evaluation engine for ML model monitoring
- **Frontend UI**: React + TypeScript + Vite web application

## Technologies

**Backend:**

- Python 3.12 (GenAI Engine), Python 3.13 (ML Engine)
- FastAPI, SQLAlchemy, PostgreSQL with pgVector
- OpenAI/Azure LLMs, LangChain, LiteLLM
- ML Models: Transformers, Sentence Transformers, Spacy
- NER/PII: Presidio, GLiNER
- Alembic for database migrations

**Frontend:**

- React 19, TypeScript, Vite
- **MUI (Material UI) v7** - Primary component library (`@mui/material`, `@mui/icons-material`, `@mui/x-date-pickers`)
- Emotion (`@emotion/react`, `@emotion/styled`) - Styling engine for MUI
- Tailwind CSS - Utility classes for layout supplementing MUI
- TanStack Query/Table, Material React Table
- Zustand for state management

**Infrastructure:**

- Docker, Docker Compose, Helm, AWS ECS
- OpenTelemetry, NewRelic
- Pytest, Coverage, Locust

## Common Commands

### GenAI Engine

```bash
# Setup
cd genai-engine
uv sync --group dev --group linters

# Start PostgreSQL (required)
docker compose up

# Database setup
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=changeme_pg_password
export POSTGRES_URL=localhost
export POSTGRES_PORT=5432
export POSTGRES_DB=arthur_genai_engine
export GENAI_ENGINE_SECRET_STORE_KEY="some_test_key"
uv run alembic upgrade head

# Run development server
export PYTHONPATH="src:$PYTHONPATH"
uv run serve
# Access at http://localhost:3030/docs

# Testing
uv run pytest -m "unit_tests"
uv run pytest -m "unit_tests" --cov=src --cov-fail-under=79
./tests/test_remote.sh  # Integration tests

# Database migrations
uv run alembic revision --autogenerate -m "<message>"
uv run alembic upgrade head

# Code quality
uv run isort src --profile black
uv run autoflake --remove-all-unused-imports --in-place --recursive src
uv run black src
uv run routes_security_check

# Generate API changelog
uv run generate_changelog
```

### ML Engine

```bash
cd ml-engine

# Generate GenAI Engine client
cd scripts
./openapi_client_utils.sh generate python
./openapi_client_utils.sh install python
./install_db_dependencies.sh
cd ..

uv sync

# Run ML Engine
uv run python src/ml_engine/job_agent.py

# Testing
uv sync --group dev
uv run pytest tests/unit

# Code quality
uv run isort src/ml_engine --profile black --check
uv run black --check src/ml_engine
uv run mypy src/ml_engine
```

### Frontend UI

```bash
cd genai-engine/ui
yarn install
yarn dev              # Development at localhost:5173
yarn build           # Production build
yarn type-check      # TypeScript checking
yarn lint            # ESLint
yarn format          # Prettier (auto-fix)
yarn format:check    # Prettier (check only)
yarn generate-api    # Generate API client from OpenAPI spec

# Before committing (REQUIRED - CI enforced)
yarn check           # Runs type-check, lint, and format:check
```

### Docker Compose (Full Stack)

```bash
cd deployment/docker-compose/genai-engine
cp .env.template .env
# Edit .env with your configuration
docker compose up
# Access at http://localhost:3030/docs
```

## Architecture

### GenAI Engine Structure

```
src/
├── server.py              # FastAPI app initialization
├── dependencies.py        # Dependency injection (DB, auth, clients)
├── config/                # Configuration management
├── auth/                  # Authentication & OAuth (Keycloak, JWT)
├── db_models/             # SQLAlchemy models (19 entity types)
│   ├── task_models.py            # Task/use-case definitions
│   ├── rule_models.py            # Rule configurations
│   ├── rule_result_models.py     # Rule evaluation results
│   ├── inference_models.py       # Span/trace data storage
│   └── dataset_models.py         # Dataset management
├── repositories/          # Data access layer (24 repositories)
│   ├── tasks_repository.py
│   ├── rules_repository.py
│   ├── inference_repository.py
│   └── span_repository.py        # Trace data queries
├── routers/               # API route handlers
│   ├── v1/                # Legacy API endpoints
│   │   ├── trace_api_routes.py
│   │   ├── llm_eval_routes.py
│   │   └── rag_routes.py
│   └── v2/                # Current API version
│       ├── task_management_routes.py
│       ├── rule_management_routes.py
│       ├── validate_routes.py
│       └── feedback_routes.py
├── scorer/                # Evaluation engine
│   ├── scorer.py          # Main scorer orchestration
│   ├── llm_client.py      # OpenAI/Azure/LiteLLM integration
│   └── checks/            # Evaluation implementations
│       ├── hallucination/         # Claim-based LLM judge
│       ├── prompt_injection/      # DebertaV3 model
│       ├── toxicity/              # RoBERTa classifier
│       ├── pii/                   # Presidio + GLiNER
│       ├── sensitive_data/        # Few-shot LLM judge
│       └── regex/                 # Pattern-based checks
├── schemas/               # Pydantic request/response models
├── utils/                 # Utility modules
│   ├── model_load.py      # Download & cache models
│   └── classifiers.py     # GPU/device detection
└── validation/            # Input validation logic
```

### ML Engine Structure

```
src/ml_engine/
├── job_agent.py           # Main agent polling for jobs
├── job_runner.py          # Job execution orchestration
├── job_executor.py        # Individual job execution
├── dataset_loader.py      # Load data from various sources
├── connectors/            # Data source connectors
│   ├── bigquery/
│   ├── snowflake/
│   ├── postgres/
│   ├── mysql/
│   ├── s3/
│   └── gcs/
├── job_executors/         # Job type handlers
│   ├── backtest_executor.py
│   └── multi_model_eval_executor.py
└── metric_calculators/    # Metric computation
```

### Database Schema (Key Entities)

- **tasks** - Use cases/LLM applications
- **rules** - Evaluation rules configuration
- **rule_results** - Results of rule evaluations
- **spans/inferences** - Trace data (prompts, responses, metadata)
- **datasets** - User data for evaluations
- **feedback** - User feedback on evaluations
- **api_keys** - Authentication credentials
- **secrets** - Encrypted credential storage
- **metrics** - Calculated metrics per task

## Key Evaluation Types

The scorer system in [src/scorer/checks/](src/scorer/checks/) implements:

- **Hallucination Detection**: Claim-based LLM judge technique
- **Prompt Injection**: DebertaV3 model-based detection
- **Toxicity**: RoBERTa toxicity classifier
- **PII Detection**: Presidio + GLiNER for Named Entity Recognition
- **Sensitive Data**: Few-shot LLM judge
- **Regex Checks**: Pattern-based validation
- Custom rules support via extensible plugin system

## Frontend UI Guidelines (MANDATORY)

### Always Use MUI Components

**All frontend UI work MUST use Material UI (MUI) components.** Do NOT use plain HTML elements or custom-styled replacements when an MUI component exists. This applies to every new component, feature, and bugfix.

**Required:** Use MUI components from `@mui/material` for all UI elements:

| Instead of...            | Always use...                                       |
| ------------------------ | --------------------------------------------------- |
| `<button>`               | `<Button>` from `@mui/material`                     |
| `<input>`, `<textarea>`  | `<TextField>` from `@mui/material`                   |
| `<select>`               | `<Select>` or `<Autocomplete>` from `@mui/material` |
| `<table>`                | `<Table>` components or Material React Table         |
| `<div>` for layout       | `<Box>`, `<Stack>`, `<Paper>`, `<Card>`             |
| `<p>`, `<h1>`-`<h6>`     | `<Typography>` with appropriate `variant`           |
| `<a>`                    | `<Link>` from `@mui/material`                        |
| `<ul>/<li>` for menus    | `<List>`, `<ListItem>`, `<Menu>`, `<MenuItem>`      |
| `<dialog>`, custom modal | `<Dialog>` with `DialogTitle`, `DialogContent`, `DialogActions` |
| Custom alert/banner      | `<Alert>` from `@mui/material`                       |
| Custom tooltip           | `<Tooltip>` from `@mui/material`                     |
| Custom chip/badge        | `<Chip>`, `<Badge>` from `@mui/material`            |
| Custom icon              | Icons from `@mui/icons-material`                     |
| Custom date picker       | Components from `@mui/x-date-pickers`               |

### Styling Rules

1. **Use the MUI `sx` prop** as the primary styling method for MUI components. This is the established pattern across the codebase.
2. **Use MUI theme color tokens** — never use raw hex/rgb colors. Use semantic tokens:
   - `primary.main`, `primary.light`, `primary.dark`, `primary.50`
   - `secondary.main`, `secondary.light`, `secondary.dark`
   - `error.main`, `error.50`, `success.main`, `success.light`
   - `warning.main`, `info.main`
   - `text.primary`, `text.secondary`, `text.disabled`
   - `divider`, `action.hover`
3. **Tailwind CSS is only for supplementary layout utilities** (e.g., `min-h-screen`, `flex`, spacing). Never use Tailwind for colors, typography, or component styling that MUI handles.
4. **Use `styled()` from `@mui/material/styles`** only when creating reusable custom-styled components that need to extend MUI components.

### Component Patterns

- **Buttons**: Use `variant="contained"` for primary actions, `variant="outlined"` for secondary, `variant="text"` for tertiary.
- **Typography**: Use semantic variants — `h5`/`h6` for headings, `subtitle1`/`body1`/`body2` for body text, `caption` for helper text.
- **Text Fields**: Use `variant="filled"` as the default text field style.
- **Layout**: Use `<Stack>` for flex layouts, `<Box>` for general containers, `<Card>`/`<Paper>` for elevated surfaces.
- **Icons**: Always source from `@mui/icons-material`. Size with `sx={{ fontSize: N }}` and color with theme tokens.
- **Feedback**: Use `<Alert>` for inline messages, notistack's `enqueueSnackbar` for toast notifications, `<Tooltip>` for hover hints.

### What NOT to Do

- **Do NOT create custom-styled `<div>`, `<span>`, or `<button>` elements** when MUI provides an equivalent component.
- **Do NOT use inline CSS styles** (`style={{ }}`) — use the `sx` prop instead.
- **Do NOT use hardcoded color values** (`#ff0000`, `rgb(...)`) — use MUI theme tokens.
- **Do NOT use Tailwind for colors or typography** — those are handled by MUI's design system.
- **Do NOT introduce new UI libraries** that duplicate MUI functionality.

## Development Workflow

### GenAI Engine Development

```bash
# Initial setup
cd genai-engine
uv sync --group dev --group linters
uv run pre-commit install

# Start PostgreSQL
docker compose up

# Set environment variables (see README.md)
# Run development server
uv run serve

# Before committing
uv run pytest -m "unit_tests"
uv run black src
uv run isort src

# Database schema changes
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head

# API changes - generate changelog
uv run generate_changelog
```

### ML Engine Development

```bash
cd ml-engine

# Generate GenAI client
cd scripts
./openapi_client_utils.sh generate python
./openapi_client_utils.sh install python
cd ..

uv sync --group dev --group linters

# Set environment variables
export ARTHUR_API_HOST=https://platform.arthur.ai
export ARTHUR_CLIENT_SECRET=<secret>
export ARTHUR_CLIENT_ID=<id>

# Run
uv run python src/ml_engine/job_agent.py

# Before committing
uv run pytest tests/unit
uv run mypy src/ml_engine
uv run black --check src/ml_engine
```

### Frontend Development

```bash
cd genai-engine/ui
yarn install
yarn dev

# After OpenAPI spec changes
yarn generate-api

# Before committing (REQUIRED - CI enforced)
yarn check  # Runs type-check, lint, and format:check
```

## Testing

**GenAI Engine:**

- Unit tests: `uv run pytest -m "unit_tests"`
- Coverage requirement: >= 79%
- Integration tests: `./tests/test_remote.sh`
- Performance tests: Locust-based (see [locust/README.md](genai-engine/locust/README.md))

**ML Engine:**

- Unit tests: `uv run pytest tests/unit`
- Type checking: `uv run mypy src/ml_engine`

**Pre-commit Hooks:**

- Trailing whitespace & end-of-file fixes
- YAML validation
- isort (import sorting)
- autoflake (unused imports removal)
- black (code formatting)
- Routes security validation
- Unit tests execution

## Key Configuration

**Environment Variables (GenAI Engine):**

```bash
# Database
POSTGRES_USER=postgres
POSTGRES_PASSWORD=changeme_pg_password
POSTGRES_URL=localhost
POSTGRES_PORT=5432
POSTGRES_DB=arthur_genai_engine

# GenAI Engine
GENAI_ENGINE_ADMIN_KEY=<admin-key>
GENAI_ENGINE_SECRET_STORE_KEY=<encryption-key>
GENAI_ENGINE_ENVIRONMENT=local|staging|production
GENAI_ENGINE_ENABLE_PERSISTENCE=enabled|disabled
GENAI_ENGINE_OPENAI_PROVIDER=Azure|OpenAI
GENAI_ENGINE_OPENAI_GPT_NAMES_ENDPOINTS_KEYS=<json-config>

# Observability
NEWRELIC_LICENSE_KEY=<key>
OTEL_EXPORTER_OTLP_ENDPOINT=<endpoint>
```

**Environment Variables (ML Engine):**

```bash
ARTHUR_API_HOST=https://platform.arthur.ai
ARTHUR_CLIENT_ID=<client-id>
ARTHUR_CLIENT_SECRET=<client-secret>
GENAI_ENGINE_INTERNAL_API_KEY=<api-key>
```

## Deployment

- **Docker**: Multi-stage builds with CPU and GPU variants
- **Docker Compose**: Full stack deployment in [deployment/docker-compose/genai-engine/](deployment/docker-compose/genai-engine/)
- **Helm**: Kubernetes deployment charts
- **CloudFormation**: AWS ECS deployment templates
- **CI/CD**: GitHub Actions ([.github/workflows/arthur-engine-workflow.yml](.github/workflows/arthur-engine-workflow.yml))

## Key Branches

This is a **fork** of [`arthur-ai/arthur-engine`](https://github.com/arthur-ai/arthur-engine) and its
branch model differs from upstream's:

- `main` - the only long-lived branch in this fork. Feature branches are cut from `main` and PR back into it.
- **There is no `dev` branch here.** Upstream has one and its CI depends on it; see the
  "Syncing from upstream" section below for what that breaks.

## Syncing from upstream arthur-engine

Read this section before merging upstream — every item below has already cost a debugging cycle.

```bash
git fetch upstream --prune
git merge upstream/dev          # day-to-day sync source
```

`upstream/dev` is the usual source. At release time `upstream/main` is a **superset** of `dev` (each
release lands on `main` as a `Increment arthur-engine version (#N)` merge commit on top of the dev
history), so merging `upstream/main` brings the fork level with both — check with
`git merge-base --is-ancestor upstream/dev upstream/main` before choosing.

Then open a PR into `main`. CI only triggers on push to `main`/`dev` and PRs targeting them, so a
feature-branch push alone runs **nothing** — without a PR the sync sits unvalidated.

> **Merge the sync PR with a merge commit. Never squash, never rebase.** See the release-pipeline
> guard below for why: a squash or rebase can put an upstream `Increment arthur-engine version`
> message at the head of `main`, which is the trigger the upstream release pipeline keys on.

### Recurring conflicts and their standing resolutions

These reappear on most syncs. The resolutions are deliberate; do not "fix" them back to upstream.

1. **`.github/workflows/meticulous.yaml` — modify/delete. Always keep the deletion.**
   `d518aca0` removed the Meticulous workflow because it cannot run in this fork: it needs
   `METICULOUS_API_TOKEN`/`METICULOUS_RECORDING_TOKEN`, which do not exist here, so it failed on
   every UI PR. Upstream still maintains the file, so every upstream change to it resurfaces as a
   modify/delete conflict. Resolve with `git rm .github/workflows/meticulous.yaml`.
   Upstream's companion changes to [genai-engine/ui/vite.config.ts](genai-engine/ui/vite.config.ts)
   (the `injectMeticulousRecordingScript` and sourcemap-chaining plugins) **do** merge in and should
   be kept — they are gated on `GENERATE_SOURCEMAPS`/`METICULOUS_RECORDING_TOKEN` and stay inert
   when unset. The same commit also dropped the `METICULOUS_*` build-args from the GPU build.

2. **`security/vex/openvex.json` — re-pair the migration image.**
   Trivy matches VEX statements by product purl. Upstream only knows `pkg:oci/genai-engine-gpu`, so
   any statement it adds must also list `pkg:oci/genai-engine-shield-migration-gpu` or the finding
   silently reappears as untriaged backlog on this fork's image. See the fork note in
   [security/README.md](security/README.md). Edit line-level to preserve the file's compact product
   formatting — reserializing the JSON churns ~150 lines and conflicts on every future sync.
   Check for gaps after merging:

   ```bash
   python3 -c "
   import json; d=json.load(open('security/vex/openvex.json'))
   print([s['vulnerability']['name'] for s in d['statements']
          if 'pkg:oci/genai-engine-gpu' in [p['@id'] for p in s.get('products',[])]
          and 'pkg:oci/genai-engine-shield-migration-gpu' not in [p['@id'] for p in s.get('products',[])]])"
   ```

   A correct sync leaves this file **purely additive** over `upstream/dev` — the only difference
   should be the added `genai-engine-shield-migration-gpu` product lines, zero deletions.

3. **`genai-engine/staging.openapi.json` — generated; keep both sides.**
   FastAPI emits `components.schemas` in **strictly alphabetical** key order, and both forks add
   schemas, so conflicts are usually two insertions at the same point. Keep both and re-sort. CI's
   changelog job regenerates the spec and diffs it, and the ml-engine client is generated from it,
   so a misordered resolution fails CI.

4. **The release-pipeline guard in `.github/workflows/arthur-engine-workflow.yml` — keep it.**
   Thirteen jobs in that workflow publish or deploy to Arthur's **official** channels: the
   `arthurplatform/genai-engine-cpu|gpu` and `ml-engine` images, the `genai-engine-models-*` images,
   CloudFormation templates, SBOMs, Helm charts, git tags, and an ECS deploy. Upstream gates them on
   `contains(github.event.head_commit.message, 'Increment arthur-engine version') && github.ref ==
   refs/heads/main|dev`. This fork's default branch **is** `refs/heads/main`, so that message check
   was the only thing standing between a fork commit and upstream's released artifacts — and the
   fork's Docker Hub credentials can push to that namespace. Each of those 13 gates therefore also
   requires `github.repository == 'arthur-ai/arthur-engine'`. Upstream does not have this line, so
   it conflicts whenever upstream edits a gate; **always keep the guard.** Verify after a sync:

   ```bash
   python3 -c "
   import yaml; d=yaml.safe_load(open('.github/workflows/arthur-engine-workflow.yml'))
   bad=[k for k,v in d['jobs'].items()
        if 'Increment arthur-engine version' in str(v.get('if','')) 
        and \"github.repository == 'arthur-ai/arthur-engine'\" not in str(v.get('if',''))]
   print('UNGUARDED RELEASE JOBS:', bad or 'none')"
   ```

   Related quirk, harmless but confusing: upstream's model-upload rebuild check resolves its baseline
   with `git log --first-parent --grep='Increment arthur-engine version' --skip=1 -n 1`. That assumes
   upstream's topology, where releases sit on `main`'s first-parent line. Here version bumps arrive
   *inside* sync merge commits, so `--first-parent` never sees them and the baseline resolves to an
   ancient commit, making that check's diff always non-empty. The guard above makes it moot.

5. **`./version` — always take upstream's.** Upstream owns it via its "Increment arthur-engine
   version" commits, and it is what tags the published image (below).

### Verifying a sync actually landed

Commit ancestry alone does not prove content survived a conflict resolution. After merging:

```bash
git merge-base --is-ancestor upstream/dev HEAD && echo "all upstream commits reachable"
BASE=$(git merge-base HEAD^1 HEAD^2)
# Files upstream touched that are NOT byte-identical in HEAD — each needs a justification
comm -12 <(git diff --name-only $BASE upstream/dev | sort) \
         <(git diff --name-only upstream/dev HEAD | sort)
```

Every file that lists should be one both sides changed. For those, confirm upstream's added lines
are all present. `.github/workflows/meticulous.yaml` is the one intentional exception.

## Publishing the shield-migration GPU image

This fork publishes its GenAI Engine GPU build to a migration-scoped Docker Hub repo,
[`arthurplatform/genai-engine-shield-migration-gpu`](https://hub.docker.com/repository/docker/arthurplatform/genai-engine-shield-migration-gpu/general),
deliberately separate from the official `arthurplatform/genai-engine-gpu` so migration builds never
clobber released images.

- **Workflow:** [.github/workflows/build-shield-migration-gpu.yml](.github/workflows/build-shield-migration-gpu.yml)
- **Trigger:** push to `main` touching `genai-engine/**`, `version`, the workflow itself, or the
  `vuln-report` composite action. A merge of an upstream sync into `main` always touches
  `genai-engine/**` and `version`, so **every sync merge republishes the image.**
- **Tags:** `<contents of ./version>` and `latest`. The version comes from
  [composite-actions/set-version](.github/workflows/composite-actions/set-version), which reads
  `./version` verbatim and appends `-dev` when the ref is not `main`. The "Resolve image tags" step
  asserts the tag matches `./version` and fails the build on an empty or mismatched value, so a
  botched merge of `./version` cannot publish a stale or untagged image.
- **Why this workflow exists:** upstream's main CI only builds images on "Increment arthur-engine
  version" commits, which `version-workflow.yml` produces on `dev` — a branch this fork does not
  have. That job therefore never fires here.

**Required secrets**, in the `shared-protected-branch-secrets` environment (Settings → Environments):

| Secret | Purpose |
| --- | --- |
| `DOCKERHUB_OSS_USERNAME` / `DOCKERHUB_OSS_TOKEN` | Push to the migration repo; also used for the authenticated pull in the advisory Trivy scan. Must be a **user account** that is a member of the `arthurplatform` org — the org name is not a login identity — with a **Read & Write** personal access token, not an account password (password login fails when 2FA is on). |
| `GITLAB_UNIFY_FRONTEND_TOKEN` | Required — the UI build stage exits 1 without it (private `@arthur/*` packages). |

Optional (`AMPLITUDE_*`, `RECAPTCHA_ENTERPRISE_SITE_KEY`): when blank the corresponding UI feature
stays disabled. `ENABLE_TELEMETRY` stays `false` — this is a migration fork, not a released build.
Do **not** add `provenance: mode=max` to the build: the build-args carry secrets and `mode=max`
records their values into the attestation on a public image.

The scan step passes `DOCKERHUB_OSS_*`, not upstream's `DOCKERHUB_ENTERPRISE_*` (which do not exist
in this fork) — the account that pushed the image is the one that should pull it. Do not restore the
upstream names when resolving a conflict here.

A "Preflight — required secrets" step fails fast and names anything missing, rather than dying later
inside `docker/login-action` with the opaque `Username and password required`.

## Important Notes

- GenAI Engine uses Python 3.12, ML Engine uses Python 3.13
- PostgreSQL with pgVector extension required for vector similarity
- Pre-commit hooks enforce code quality and run tests
- API changes require changelog generation via `uv run generate_changelog`
- Model files are downloaded and cached on first use
- GPU support optional but improves performance for model-based checks
- **Frontend: Always use MUI components** — never use plain HTML elements when MUI provides an equivalent. See "Frontend UI Guidelines" section above for full details.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
