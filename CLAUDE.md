# CLAUDE.md

Arthur Engine is an AI/ML monitoring and governance platform. Each component has its own CLAUDE.md with commands and gotchas:

- [genai-engine/](genai-engine/CLAUDE.md) — FastAPI REST API for LLM evaluation and guardrailing (Python 3.12, PostgreSQL + pgVector)
- [genai-engine/ui/](genai-engine/ui/CLAUDE.md) — React 19 + TypeScript + Vite frontend
- [ml-engine/](ml-engine/CLAUDE.md) — job-based evaluation engine for ML model monitoring (Python 3.13)
- [arthur-observability-sdk/](arthur-observability-sdk/CLAUDE.md) — Python SDK for tracing and prompt management

## Workflow

- In this fork, `main` is the only long-lived branch — feature branches come from `main` and PR back into it. See "Key Branches" below. (Upstream's default branch is `dev`.)
- Pre-commit hooks format code and run the unit test suites — a slow or failing commit is usually them, not git.
- GenAI Engine API changes require a changelog entry: `uv run generate_changelog` from `genai-engine/`.
- Repo skills cover environment setup and running the stack: `setup-genai-dev`, `start-genai-backend`, `start-genai-frontend`.
- Full-stack local deployment: [deployment/docker-compose/genai-engine/](deployment/docker-compose/genai-engine/) (`cp .env.template .env`, then `docker compose up`).

## Dependencies

Every Python project here is `pyproject.toml` + `uv.lock`, and the two must never disagree. CI installs
with `uv sync --frozen`, which replays the lockfile and never looks at the manifest — so a manifest-only
change is invisible at runtime while still looking merged. Change a pin, then `uv lock --directory <project>`
and commit both files together. Never hand-edit `uv.lock`.

`check-dependency-automation` in [the CI workflow](.github/workflows/arthur-engine-workflow.yml) enforces
this across all four uv projects via
[`.github/scripts/check-lockfile-drift.sh`](.github/scripts/check-lockfile-drift.sh), which you can run
locally from the repo root. It reports one of two states:

- **STALE** — the manifest resolves but the lock was not regenerated. Run `uv lock` and commit.
- **UNRESOLVABLE** — nothing can satisfy the manifest, usually because a *transitive* constraint of
  another pinned dependency caps the package being bumped (`litellm` caps `openai<3.0.0`; `gliner` caps
  `transformers`; `presidio-anonymizer` caps `cryptography`). Move the lagging dependency forward — never
  cap or downgrade the one being updated. If nothing upstream lifts the ceiling yet, add an
  `allowedVersions` rule in [renovate.json](renovate.json) so Renovate stops re-proposing it, naming the
  blocker and the condition for removing the cap.

Renovate opens PRs that edit the manifest without the lock whenever its lockfile step fails, and offers no
way to fail closed — that gate is the only thing that catches it. See the `description` at the top of
[renovate.json](renovate.json).

## Code style

Write code that reads like the surrounding code: match its comment density, naming, and idiom.

## Frontend

All UI work uses MUI components styled via `sx` with theme tokens — see [genai-engine/ui/CLAUDE.md](genai-engine/ui/CLAUDE.md) for the rules.

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
