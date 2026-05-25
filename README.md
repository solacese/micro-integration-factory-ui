# Micro Integration Factory

By Joe Schulze and Raphael Caillon.

Solace-inspired AI workbench for building micro-integrations from live event payloads.

The workbench captures a Solace event, lets you describe the target behavior in chat,
asks Claude through LiteLLM for an implementation-ready plan, previews a sample output,
then generates a Solace MDK Java/Spring project and queues a local VS Code plus Docker build job.

![Micro Integration Factory preview](docs/workbench-preview.svg)

## What It Builds

- Solace MDK micro-integration projects using `micro-integration-build-parent`
- Java SDK runtime code by default
- Optional Groovy transform drafts
- Optional DataWeave `.dwl` drafts with equivalent Java runtime output
- Docker-ready generated projects with Solace workflow configuration
- Event Portal-ready canonical topics, schemas, applications, and event metadata

## Quickstart

```bash
cp .env.example .env
make bootstrap
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) and use the default `demo://sample`
broker to create the first generated project without a real broker. The app is public by
default for demos; set `DISABLE_AUTH=false` only if you want to restore the admin password gate.

For a real Solace broker, set:

```bash
SOLACE_BROKER_URL=tcp://your-broker:55555
SOLACE_VPN=default
SOLACE_USERNAME=...
SOLACE_PASSWORD=...
SOLACE_WEB_MESSAGING_URL=...
```

## LiteLLM

The API calls any OpenAI-compatible LiteLLM endpoint. Store values in `.env`, the settings UI, or AWS Secrets Manager:

```bash
LITELLM_BASE_URL=https://your-litellm.example
LITELLM_API_KEY=replace-me
LITELLM_MODEL=claude-sonnet-4-6
```

The builder uses Claude Sonnet through LiteLLM to ask qualifying questions, produce a sample
output, and generate only the transformation artifact. The Solace MDK scaffold comes from the
project template. The generation flow stores the Claude/LiteLLM draft in
`ai/claude-code-plan.json` inside the generated project. LiteLLM is required for live design,
preview, and project generation; there is no local draft fallback.

## Architecture

```mermaid
flowchart LR
  A["Solace topic capture"] --> B["CapturedEvent"]
  B --> C["Builder session"]
  C --> D["Claude Code via LiteLLM"]
  D --> E["MDK project renderer"]
  E --> F["Generated Java/Spring project"]
  F --> G["Local worker"]
  G --> H["VS Code + Docker image"]
  E --> I["Event Portal metadata"]
```

The imported foundation comes from [`solacese/agentic-integration-factory`](https://github.com/solacese/agentic-integration-factory) and includes FastAPI orchestration, Next.js UI, Solace live bridge, MDK templates, Docker/ECR build adapters, and Event Portal sync.

## AWS SaaS Target

The hosted architecture is designed for:

- Web/API on ECS Fargate behind an ALB
- RDS Postgres for workbench state
- ElastiCache Redis for queued jobs
- S3 for generated artifacts
- ECR for generated images
- AWS Secrets Manager for broker, LiteLLM, registry, and Event Portal credentials

The local worker remains optional in hosted mode and is responsible for local VS Code open plus local Docker builds.

Deploy the control plane when AWS credentials and Docker are available:

```bash
# Optional: restrict hosted HTTP to a known CIDR. If unset, deploy resolves
# the current public IP and uses a /32 rule.
CONTROL_PLANE_ALLOWED_CIDR=203.0.113.10/32
make deploy-aws
make aws-status
make aws-down
```

## Security

- Never commit `.env`, cloud credentials, broker credentials, or LiteLLM keys.
- Rotate any credential pasted into chat, logs, issues, or screenshots.
- Store production secrets in AWS Secrets Manager or the encrypted settings store.
- Generated projects should be reviewed before production deployment.

## Development

```bash
make test
make lint
npm run build:web
```

Main folders:

- `apps/web` - Next.js workbench
- `apps/api` - FastAPI control plane and generator
- `apps/api/resources/templates` - Solace MDK project templates
- `apps/api/resources/mdk-reference` - Solace MDK reference project
- `infra/docker` - local Postgres, Redis, Solace, and API compose files

## License

Apache 2.0.
