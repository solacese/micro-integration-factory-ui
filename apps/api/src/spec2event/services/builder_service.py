from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from spec2event.config import get_settings
from spec2event.db import session_scope
from spec2event.models import (
    BuilderSession,
    CapturedEvent,
    ChatMessage,
    GenerationRun,
    SourceUpload,
    WorkerJob,
)
from spec2event.services.generator_service import generator_service
from spec2event.services.run_service import (
    create_run,
    iso,
    snapshot_workspace,
    update_run,
)
from spec2event.services.settings_service import get_secret

TRANSFORM_LANGUAGES = {"java_sdk", "groovy", "dataweave"}
AI_TIMEOUT_SECONDS = 90.0
TRANSFORM_ARTIFACT_PREFIX = "src/main/resources/transforms/"
MAX_SAMPLE_OUTPUT_BYTES = 256 * 1024
MAX_TRANSFORM_ARTIFACT_BYTES = 128 * 1024
SECRET_PATTERNS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"aws_secret_access_key", re.IGNORECASE),
    re.compile(r"aws_session_token", re.IGNORECASE),
    re.compile(r"litellm_api_key", re.IGNORECASE),
    re.compile(r"['\"]?password['\"]?\s*[:=]\s*['\"][^'\"]{8,}['\"]", re.IGNORECASE),
]
TEXT_SUFFIXES = {
    ".java",
    ".groovy",
    ".dwl",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".xml",
    ".properties",
    ".txt",
    ".example",
}


def create_builder_session(
    db: Session, *, captured_event_id: str, transform_language: str
) -> BuilderSession:
    if transform_language not in TRANSFORM_LANGUAGES:
        raise HTTPException(status_code=400, detail="Unsupported transform language")
    captured_event = db.get(CapturedEvent, captured_event_id)
    if captured_event is None:
        raise HTTPException(status_code=404, detail="Captured event not found")
    session = BuilderSession(
        captured_event_id=captured_event.id,
        transform_language=transform_language,
        status="awaiting_design",
        draft_json=None,
    )
    db.add(session)
    db.flush()
    return session


def redesign_session(
    db: Session,
    *,
    builder_session_id: str,
    content: str,
    transform_language: str | None = None,
    schema_context: str | None = None,
) -> BuilderSession:
    session = get_builder_session(db, builder_session_id)
    if transform_language:
        if transform_language not in TRANSFORM_LANGUAGES:
            raise HTTPException(status_code=400, detail="Unsupported transform language")
        session.transform_language = transform_language

    draft = generate_draft(
        db, session, content, intent="live_design", schema_context=schema_context
    )
    draft["workbenchPrompt"] = content
    draft["schemaContext"] = schema_context
    session.draft_json = draft
    session.intent_summary = draft.get("intentSummary")
    session.status = "designed"
    db.flush()
    return session


def add_chat_turn(
    db: Session,
    *,
    builder_session_id: str,
    content: str,
    transform_language: str | None = None,
    schema_context: str | None = None,
) -> BuilderSession:
    session = get_builder_session(db, builder_session_id)
    if transform_language:
        if transform_language not in TRANSFORM_LANGUAGES:
            raise HTTPException(status_code=400, detail="Unsupported transform language")
        session.transform_language = transform_language

    user_message = ChatMessage(
        builder_session_id=session.id,
        role="user",
        content=content,
    )
    db.add(user_message)
    db.flush()

    draft = generate_draft(
        db, session, content, intent="chat_turn", schema_context=schema_context
    )
    draft["workbenchPrompt"] = content
    draft["schemaContext"] = schema_context
    session.draft_json = draft
    session.intent_summary = draft.get("intentSummary")
    session.status = "drafted"

    assistant_message = ChatMessage(
        builder_session_id=session.id,
        role="assistant",
        content=draft.get("assistantMessage") or "Draft updated.",
        draft_json=draft,
    )
    db.add(assistant_message)
    db.flush()
    return session


def create_preview(db: Session, *, builder_session_id: str) -> dict[str, Any]:
    session = get_builder_session(db, builder_session_id)
    existing_draft = _current_draft(session)
    if existing_draft is not None and isinstance(existing_draft.get("sampleOutput"), dict):
        sample_output = existing_draft["sampleOutput"]
        session.preview_json = sample_output
        session.status = "previewed"
        db.flush()
        return {
            "sessionId": session.id,
            "transformLanguage": session.transform_language,
            "sampleOutput": sample_output,
            "draft": existing_draft,
        }
    user_prompt = (
        _latest_user_prompt(session)
        or _draft_workbench_prompt(session)
        or "Create a sample preview output for the selected payload transformation."
    )
    schema_context = _draft_schema_context(session)
    draft = generate_draft(
        db,
        session,
        user_prompt,
        intent="preview",
        schema_context=schema_context,
    )
    draft["workbenchPrompt"] = user_prompt
    draft["schemaContext"] = schema_context
    session.draft_json = draft
    sample_output = draft.get("sampleOutput")
    if not isinstance(sample_output, dict):
        raise HTTPException(status_code=502, detail="LiteLLM response omitted sampleOutput")
    session.preview_json = sample_output
    session.status = "previewed"
    db.flush()
    return {
        "sessionId": session.id,
        "transformLanguage": session.transform_language,
        "sampleOutput": sample_output,
        "draft": draft,
    }


def generate_micro_integration(
    db: Session, *, builder_session_id: str
) -> tuple[BuilderSession, Any, WorkerJob]:
    session = get_builder_session(db, builder_session_id)
    captured_event = session.captured_event
    draft = _current_draft(session)
    if draft is None:
        user_prompt = (
            _latest_user_prompt(session)
            or _draft_workbench_prompt(session)
            or "Create the final implementation plan for this Solace micro-integration."
        )
        schema_context = _draft_schema_context(session)
        draft = generate_draft(
            db,
            session,
            user_prompt,
            intent="generate_project",
            schema_context=schema_context,
        )
        draft["workbenchPrompt"] = user_prompt
        draft["schemaContext"] = schema_context
    draft["generationMode"] = "fast_template_single_transform_file"
    session.draft_json = draft
    session.intent_summary = draft.get("intentSummary")
    service_name = _service_name(draft, captured_event)
    raw_source = json.dumps(
        {
            "capturedEvent": serialize_captured_event(captured_event),
            "draft": draft,
        },
        indent=2,
    )
    summary = {
        "serviceName": service_name,
        "sourceType": "solace_live_event",
        "topicFilter": captured_event.topic_filter,
        "topicName": captured_event.topic_name,
        "transformLanguage": session.transform_language,
    }
    upload = SourceUpload(
        source_type="custom",
        filename=f"{service_name}-captured-event.json",
        content_type="application/json",
        raw_content=raw_source,
        summary_json=summary,
    )
    db.add(upload)
    db.flush()
    run = create_run(db, upload, "local_docker")
    canonical_model = canonical_model_for_event(
        service_name=service_name,
        captured_event=captured_event,
        draft=draft,
        transform_language=session.transform_language,
    )
    workspace = generator_service.generate(run.id, canonical_model, summary, raw_source)
    _write_transform_artifacts(workspace, session.transform_language, draft, captured_event)
    update_run(
        db,
        run,
        status="completed",
        service_name=service_name,
        workspace_path=str(workspace),
        canonical_model_json=canonical_model,
        last_message="Generated Micro Integration Factory project",
    )
    snapshot_workspace(db, run, workspace)
    session.generated_run_id = run.id
    session.status = "generated"
    job = WorkerJob(
        builder_session_id=session.id,
        status="pending",
        project_path=str(workspace),
        logs="Local worker queued. Security and performance checks will run before Docker build.\n",
    )
    db.add(job)
    db.flush()
    if get_settings().enable_local_worker:
        _start_local_worker(job.id, Path(workspace), service_name)
    return session, run, job


def get_builder_session(db: Session, builder_session_id: str) -> BuilderSession:
    session = db.get(BuilderSession, builder_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Builder session not found")
    return session


def update_worker_job_result(
    db: Session,
    *,
    worker_job_id: str,
    status: str,
    logs: str | None = None,
    image_tag: str | None = None,
    project_path: str | None = None,
    result: dict[str, Any] | None = None,
) -> WorkerJob:
    job = db.get(WorkerJob, worker_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Worker job not found")
    job.status = status
    if logs is not None:
        job.logs = logs
    if image_tag is not None:
        job.image_tag = image_tag
    if project_path is not None:
        job.project_path = project_path
    if result is not None:
        job.result_json = result
    db.flush()
    return job


def serialize_captured_event(event: CapturedEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "subscription_id": event.subscription_id,
        "broker_url": _redact_url(event.broker_url),
        "topic_filter": event.topic_filter,
        "topic_name": event.topic_name,
        "headers": event.headers_json or {},
        "payload": event.payload_json or {},
        "created_at": iso(event.created_at),
    }


def serialize_chat_message(message: ChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "draft": message.draft_json,
        "created_at": iso(message.created_at),
    }


def serialize_builder_session(session: BuilderSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "transform_language": session.transform_language,
        "status": session.status,
        "intent_summary": session.intent_summary,
        "draft": session.draft_json,
        "preview": session.preview_json,
        "generated_run_id": session.generated_run_id,
        "captured_event": serialize_captured_event(session.captured_event),
        "messages": [
            serialize_chat_message(message)
            for message in sorted(session.messages, key=lambda item: item.created_at)
        ],
    }


def serialize_worker_job(job: WorkerJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "builder_session_id": job.builder_session_id,
        "job_type": job.job_type,
        "status": job.status,
        "project_path": job.project_path,
        "image_tag": job.image_tag,
        "logs": job.logs,
        "result": job.result_json,
        "created_at": iso(job.created_at),
        "updated_at": iso(job.updated_at),
    }


def generate_draft(
    db: Session,
    session: BuilderSession,
    user_prompt: str,
    *,
    intent: str,
    schema_context: str | None = None,
) -> dict[str, Any]:
    base_url = get_secret(db, "litellm_base_url")
    api_key = get_secret(db, "litellm_api_key")
    model = get_secret(db, "litellm_model") or get_settings().litellm_model
    if not base_url or not api_key or not model:
        raise HTTPException(
            status_code=400,
            detail=(
                "LiteLLM is required. Configure LITELLM_BASE_URL, "
                "LITELLM_API_KEY, and LITELLM_MODEL."
            ),
        )

    prompt = _draft_prompt(session, user_prompt, intent=intent, schema_context=schema_context)
    try:
        response = httpx.post(
            _chat_completion_url(base_url),
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are Claude Code running through LiteLLM as a focused "
                            "Solace micro-integration transform engineer. Only build "
                            "Solace micro integrations. Refuse unrelated scope inside "
                            "the JSON assistantMessage. Return strict JSON only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.6,
            },
            timeout=httpx.Timeout(AI_TIMEOUT_SECONDS, connect=5.0),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        draft = normalize_draft(parsed, session.captured_event, session.transform_language)
        draft["llmProvider"] = {
            "provider": "litellm",
            "model": model,
            "mode": "claude-code",
            "status": "completed",
            "intent": intent,
        }
        return draft
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LiteLLM design failed: {exc}") from exc


def normalize_draft(
    draft: dict[str, Any],
    captured_event: CapturedEvent,
    transform_language: str,
) -> dict[str, Any]:
    required = {
        "intentSummary": str,
        "assistantMessage": str,
        "topicMapping": dict,
        "sampleOutput": dict,
        "files": list,
        "validationNotes": list,
    }
    missing_or_invalid = [
        key
        for key, expected_type in required.items()
        if not isinstance(draft.get(key), expected_type)
    ]
    if missing_or_invalid:
        raise ValueError(f"LiteLLM draft missing required fields: {missing_or_invalid}")
    topic_mapping = draft["topicMapping"]
    if not isinstance(topic_mapping.get("inputTopic"), str):
        topic_mapping["inputTopic"] = captured_event.topic_name
    if not isinstance(topic_mapping.get("outputTopic"), str):
        raise ValueError("LiteLLM draft must include topicMapping.outputTopic")
    draft["files"] = _normalize_draft_files(draft["files"])
    draft["files"] = _single_transform_file(draft["files"], transform_language)
    draft["qualifyingQuestions"] = _normalize_question_list(
        draft.get("qualifyingQuestions"),
        captured_event,
        transform_language,
    )
    return {
        **draft,
        "transformLanguage": transform_language,
    }


def _normalize_draft_files(files: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("LiteLLM draft files must contain objects")
        path = item.get("path")
        purpose = item.get("purpose")
        content = item.get("content")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("LiteLLM draft file entries require path")
        if path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"LiteLLM draft file path is not safe: {path}")
        if not path.startswith(TRANSFORM_ARTIFACT_PREFIX):
            raise ValueError(
                "LiteLLM may only generate transform artifacts under "
                f"{TRANSFORM_ARTIFACT_PREFIX}: {path}"
            )
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError(f"LiteLLM draft file requires purpose: {path}")
        file_entry = {"path": path, "purpose": purpose}
        if content is not None:
            if not isinstance(content, str):
                raise ValueError(f"LiteLLM draft file content must be a string: {path}")
            file_entry["content"] = content
        normalized.append(file_entry)
    return normalized


def _single_transform_file(
    files: list[dict[str, str]], transform_language: str
) -> list[dict[str, str]]:
    expected_path = _expected_transform_path(transform_language)
    for item in files:
        path = item.get("path")
        content = item.get("content")
        if path == expected_path and isinstance(content, str) and content.strip():
            return [item]
    expected_name = Path(expected_path).name
    for item in files:
        path = item.get("path")
        content = item.get("content")
        if Path(path or "").name == expected_name and isinstance(content, str) and content.strip():
            return [{**item, "path": expected_path}]
    raise ValueError(f"LiteLLM draft must include exactly one transform file: {expected_path}")


def _expected_transform_path(transform_language: str) -> str:
    if transform_language == "groovy":
        return "src/main/resources/transforms/transform.groovy"
    if transform_language == "dataweave":
        return "src/main/resources/transforms/transform.dwl"
    return "src/main/resources/transforms/transform.md"


def _normalize_question_list(
    questions: Any,
    captured_event: CapturedEvent,
    transform_language: str,
) -> list[str]:
    defaults = [
        "What output topic should this micro integration publish to?",
        "How should invalid or schema-mismatched payloads be handled?",
        "Which fields are sensitive and must be removed, masked, or hashed?",
    ]
    if transform_language == "aggregation":
        defaults[1] = "What aggregation key and time window should be used?"
    normalized: list[str] = []
    topic = captured_event.topic_name or captured_event.topic_filter
    if topic:
        defaults[0] = f"What output topic should events from {topic} publish to?"
    if not isinstance(questions, list):
        questions = []
    for question in questions:
        if isinstance(question, str) and question.strip() and question not in normalized:
            normalized.append(question)
    for question in defaults:
        if len(normalized) >= 3:
            break
        if question not in normalized:
            normalized.append(question)
    return normalized[:3]


def _draft_file_content(draft: dict[str, Any], expected_path: str) -> str | None:
    expected_name = Path(expected_path).name
    for item in draft.get("files", []):
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if path == expected_path or Path(path).name == expected_name:
            return content
    return None


def canonical_model_for_event(
    *,
    service_name: str,
    captured_event: CapturedEvent,
    draft: dict[str, Any],
    transform_language: str,
) -> dict[str, Any]:
    entity = _entity_name(captured_event.topic_name or service_name)
    event_name = f"{_pascal(entity)}Processed"
    schema_name = f"{event_name}Payload"
    raw_topic_mapping = draft.get("topicMapping")
    topic_mapping = raw_topic_mapping if isinstance(raw_topic_mapping, dict) else {}
    raw_output_topic = topic_mapping.get("outputTopic")
    output_topic = (
        raw_output_topic
        if isinstance(raw_output_topic, str)
        else f"micro/integration/{entity}/processed/v1"
    )
    operation_id = f"process{_pascal(entity)}"
    return {
        "title": f"{_title(service_name)} Micro Integration",
        "serviceName": service_name,
        "serviceVersion": "1.0.0",
        "servers": [],
        "authSchemes": [],
        "ingressType": "event_subscriber",
        "stripeEnabled": False,
        "operations": [
            {
                "operationId": operation_id,
                "method": "SUBSCRIBE",
                "path": f"/topics/{_slug(captured_event.topic_filter)}",
                "summary": draft.get("intentSummary")
                or "Process a captured Solace event payload",
                "requestSchema": {"type": "object"},
                "responseSchema": {"type": "object"},
                "emitsEvent": True,
                "eventCandidates": [
                    {
                        "canonicalEventName": event_name,
                        "topicName": output_topic,
                        "schemaName": schema_name,
                        "applicationName": f"{service_name}-integration",
                        "operationId": operation_id,
                        "emitsEvent": True,
                    }
                ],
            }
        ],
        "topics": [output_topic],
        "schemaNames": [schema_name],
        "applicationNames": [f"{service_name}-integration"],
        "testFixtures": [
            {
                "operationId": operation_id,
                "label": f"Captured event from {captured_event.topic_name}",
                "method": "POST",
                "path": "/test-events",
                "payload": captured_event.payload_json,
            }
        ],
        "builderDraft": draft,
        "transformLanguage": transform_language,
    }


def _write_transform_artifacts(
    workspace: Path, transform_language: str, draft: dict[str, Any], captured_event: CapturedEvent
) -> None:
    transform_dir = workspace / "src/main/resources/transforms"
    transform_dir.mkdir(parents=True, exist_ok=True)
    sample_input = json.dumps(captured_event.payload_json or {}, indent=2)
    sample_output = json.dumps(draft.get("sampleOutput") or {}, indent=2)
    if transform_language == "groovy":
        groovy_content = _draft_file_content(
            draft, "src/main/resources/transforms/transform.groovy"
        )
        if groovy_content is None:
            raise HTTPException(
                status_code=502,
                detail=(
                    "LiteLLM draft omitted "
                    "src/main/resources/transforms/transform.groovy content"
                ),
            )
        (transform_dir / "transform.groovy").write_text(
            groovy_content,
            encoding="utf-8",
        )
    elif transform_language == "dataweave":
        dataweave_content = _draft_file_content(
            draft, "src/main/resources/transforms/transform.dwl"
        )
        if dataweave_content is None:
            raise HTTPException(
                status_code=502,
                detail="LiteLLM draft omitted src/main/resources/transforms/transform.dwl content",
            )
        (transform_dir / "transform.dwl").write_text(
            dataweave_content,
            encoding="utf-8",
        )
    else:
        (transform_dir / "transform.md").write_text(
            _draft_file_content(draft, "src/main/resources/transforms/transform.md")
            or draft.get(
                "assistantMessage",
                "Java SDK transform plan generated for this micro integration.",
            ),
            encoding="utf-8",
        )
    (transform_dir / "sample-input.json").write_text(sample_input, encoding="utf-8")
    (transform_dir / "sample-output.json").write_text(sample_output, encoding="utf-8")
    ai_dir = workspace / "ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    (ai_dir / "claude-code-plan.json").write_text(
        json.dumps(draft, indent=2),
        encoding="utf-8",
    )


def _start_local_worker(job_id: str, workspace: Path, service_name: str) -> None:
    thread = threading.Thread(
        target=_run_local_worker,
        args=(job_id, workspace, service_name),
        daemon=True,
        name=f"local-worker-{job_id}",
    )
    thread.start()


def _run_local_worker(job_id: str, workspace: Path, service_name: str) -> None:
    logs: list[str] = []
    status = "running"
    local_image_tag = f"micro-integration-factory/{service_name}:local"
    image_tag: str | None = local_image_tag
    with session_scope() as db:
        update_worker_job_result(db, worker_job_id=job_id, status=status, logs="Starting.\n")

    code_path = shutil.which("code")
    if code_path:
        result = subprocess.run(
            [code_path, str(workspace)], capture_output=True, text=True, check=False, timeout=15
        )
        logs.append(_command_log("code", result.returncode, result.stdout, result.stderr))
    else:
        logs.append("VS Code CLI not found; project was generated but not opened.\n")

    try:
        quality_logs = _run_quality_checks(workspace)
        logs.extend(quality_logs)
    except RuntimeError as exc:
        logs.append(f"Quality gates failed.\n{exc}\n")
        with session_scope() as db:
            update_worker_job_result(
                db,
                worker_job_id=job_id,
                status="failed",
                logs="".join(logs),
                image_tag=None,
                project_path=str(workspace),
                result={
                    "workspace": str(workspace),
                    "securityPassed": False,
                    "performancePassed": False,
                    "dockerAvailable": bool(shutil.which("docker")),
                },
            )
        return

    docker_path = shutil.which("docker")
    if docker_path:
        result = subprocess.run(
            [docker_path, "build", "-t", local_image_tag, "."],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        logs.append(_command_log("docker build", result.returncode, result.stdout, result.stderr))
        status = "completed" if result.returncode == 0 else "failed"
    else:
        logs.append("Docker CLI not found; image build was skipped.\n")
        status = "partial"
        image_tag = None

    with session_scope() as db:
        job = db.get(WorkerJob, job_id)
        if status == "completed" and job is not None and job.builder_session.generated_run_id:
            run = db.get(GenerationRun, job.builder_session.generated_run_id)
            if run is not None and image_tag:
                update_run(db, run, image_tag=image_tag)
        update_worker_job_result(
            db,
            worker_job_id=job_id,
            status=status,
            logs="".join(logs),
            image_tag=image_tag,
            project_path=str(workspace),
            result={
                "workspace": str(workspace),
                "securityPassed": True,
                "performancePassed": True,
                "mavenTestsRun": bool(shutil.which("mvn")),
                "dockerAvailable": bool(docker_path),
            },
        )


def _run_quality_checks(workspace: Path) -> list[str]:
    logs = [
        _run_security_check(workspace),
        _run_performance_check(workspace),
    ]
    mvn_path = shutil.which("mvn")
    if mvn_path:
        result = subprocess.run(
            [mvn_path, "test"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        logs.append(_command_log("mvn test", result.returncode, result.stdout, result.stderr))
        if result.returncode != 0:
            raise RuntimeError("Maven tests failed before Docker build.")
    else:
        logs.append("Maven CLI not found; generated project tests were skipped.\n")
    return logs


def _run_security_check(workspace: Path) -> str:
    findings: list[str] = []
    for path in workspace.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if "target" in path.parts or ".git" in path.parts:
            continue
        if path.name in {"sample-input.json", "source-spec.json"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                findings.append(str(path.relative_to(workspace)))
                break
    if findings:
        raise RuntimeError(
            "Security scan found possible committed secrets in generated files: "
            + ", ".join(findings)
        )
    return "$ security scan\nexit=0\nNo committed secrets or unsafe transform paths detected.\n"


def _run_performance_check(workspace: Path) -> str:
    output_path = workspace / "src/main/resources/transforms/sample-output.json"
    transform_dir = workspace / "src/main/resources/transforms"
    if output_path.exists() and output_path.stat().st_size > MAX_SAMPLE_OUTPUT_BYTES:
        raise RuntimeError("Performance check failed: sample output exceeds 256 KB.")
    if transform_dir.exists():
        for path in transform_dir.iterdir():
            if path.is_file() and path.stat().st_size > MAX_TRANSFORM_ARTIFACT_BYTES:
                raise RuntimeError(
                    f"Performance check failed: {path.name} exceeds 128 KB transform budget."
                )
    return (
        "$ performance scan\n"
        "exit=0\n"
        "Transform artifacts are bounded and sample output is within the preview budget.\n"
    )


def _command_log(command: str, returncode: int, stdout: str, stderr: str) -> str:
    return f"$ {command}\nexit={returncode}\n{stdout}{stderr}\n"


def _draft_prompt(
    session: BuilderSession,
    user_prompt: str,
    *,
    intent: str,
    schema_context: str | None = None,
) -> str:
    template_notes = {
        "java_sdk": "Generate a concise Java SDK transform plan in transform.md only.",
        "groovy": "Generate only the Groovy transform script in transform.groovy.",
        "dataweave": "Generate only the DataWeave transform in transform.dwl.",
    }
    return json.dumps(
        {
            "task": (
                "You are Claude Code building a small Solace MDK micro-integration. "
                "Stay narrowly focused on this micro integration and nothing else. "
                "The application template owns the broker client, Spring/MDK scaffold, "
                "Dockerfile, tests, and project structure. You only generate the true "
                "payload transformation artifact and a sample transformed output. "
                "Generate exactly one mutable transform file. The rest of the project "
                "is deterministic template code. "
                "Use customer JSON/XML schemas, keep/drop rules, and Event Portal schema "
                "context as validation and mapping constraints. If the schema context "
                "contains a keep list, return only those fields unless the user explicitly "
                "asks otherwise. "
                "Return JSON only with exactly these top-level keys: transformLanguage, "
                "intentSummary, assistantMessage, qualifyingQuestions, topicMapping, "
                "sampleOutput, files, validationNotes. Do not return markdown. Ask "
                "exactly two or three qualifyingQuestions before generation so the user "
                "can confirm the output topic, error behavior, and sensitive fields. "
                "Ask those questions even while providing your best draft. Prioritize "
                "output topic, invalid payload handling, anonymization fields, aggregation "
                "window, or required canonical fields. Do not propose macro integrations, external "
                "systems, broad architectures, auth flows, data lakes, orchestration, or "
                "non-transform files. Include generated file contents in files[].content. "
                "Do not mention Claude Code, LiteLLM, model names, or internal prompting "
                "inside assistantMessage."
            ),
            "intent": intent,
            "modelBehavior": (
                "Use Claude Sonnet 4.6 style: concise, careful questions, smooth "
                "incremental design, and code-focused output."
            ),
            "transformLanguage": session.transform_language,
            "transformArtifactRule": template_notes.get(session.transform_language),
            "userPrompt": user_prompt,
            "schemaContext": schema_context,
            "capturedEvent": serialize_captured_event(session.captured_event),
            "previousDraft": session.draft_json,
            "requiredContract": {
                "transformLanguage": session.transform_language,
                "intentSummary": "string",
                "assistantMessage": "string",
                "qualifyingQuestions": ["string"],
                "topicMapping": {
                    "inputTopic": session.captured_event.topic_name,
                    "outputTopic": "string Solace topic",
                },
                "sampleOutput": "object transformed from capturedEvent.payload",
                "files": [
                    {"path": "string", "purpose": "string", "content": "string"},
                ],
                "validationNotes": ["string"],
            },
            "schemaBehavior": {
                "acceptedInputs": ["JSON schema", "XML schema", "simple keep/drop rules"],
                "keepRuleExample": {
                    "userPrompt": (
                        "Strip out sensitive data from ORDER and only return customer, "
                        "items and total"
                    ),
                    "schemaContext": {"keep": ["customer", "items", "total", "orderId"]},
                    "dataWeaveShape": (
                        "Return a %dw 2.0 mapping that selects payload.orderId, "
                        "payload.customer, payload.items, and payload.total."
                    ),
                },
                "eventPortal": (
                    "When Event Portal schema check is requested but no schema is present, "
                    "ask one qualifying question for the schema name or subject."
                ),
            },
            "fileRequirements": {
                "groovy": (
                    "Include only src/main/resources/transforms/transform.groovy with content."
                ),
                "dataweave": (
                    "Include only src/main/resources/transforms/transform.dwl with content."
                ),
                "java_sdk": (
                    "Include only src/main/resources/transforms/transform.md with the Java SDK "
                    "implementation approach and any important generated snippets."
                ),
            },
        },
        indent=2,
    )


def _latest_user_prompt(session: BuilderSession) -> str | None:
    for message in sorted(session.messages, key=lambda item: item.created_at, reverse=True):
        if message.role == "user":
            return message.content
    return None


def _draft_workbench_prompt(session: BuilderSession) -> str | None:
    if isinstance(session.draft_json, dict):
        prompt = session.draft_json.get("workbenchPrompt")
        if isinstance(prompt, str):
            return prompt
    return None


def _draft_schema_context(session: BuilderSession) -> str | None:
    if isinstance(session.draft_json, dict):
        schema_context = session.draft_json.get("schemaContext")
        if isinstance(schema_context, str) and schema_context.strip():
            return schema_context
    return None


def _current_draft(session: BuilderSession) -> dict[str, Any] | None:
    if not isinstance(session.draft_json, dict):
        return None
    try:
        return normalize_draft(
            dict(session.draft_json),
            session.captured_event,
            session.transform_language,
        )
    except ValueError:
        return None


def _extract_json(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("Draft response must be a JSON object")
    return parsed


def _chat_completion_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _language_files(transform_language: str) -> list[dict[str, str]]:
    if transform_language == "groovy":
        return [
            {"path": "src/main/resources/transforms/transform.groovy", "purpose": "Groovy draft"},
            {
                "path": "src/main/java/.../service/CanonicalEventService.java",
                "purpose": "MDK runtime",
            },
        ]
    if transform_language == "dataweave":
        return [
            {"path": "src/main/resources/transforms/transform.dwl", "purpose": "DataWeave draft"},
            {
                "path": "src/main/java/.../service/CanonicalEventService.java",
                "purpose": "Equivalent Java runtime",
            },
        ]
    return [
        {
            "path": "src/main/java/.../service/CanonicalEventService.java",
            "purpose": "Java SDK runtime",
        },
        {"path": "config/application-runtime.yml", "purpose": "Solace workflow routing"},
    ]


def _service_name(draft: dict[str, Any], captured_event: CapturedEvent) -> str:
    raw_topic_mapping = draft.get("topicMapping")
    topic_mapping = raw_topic_mapping if isinstance(raw_topic_mapping, dict) else {}
    basis = (
        draft.get("serviceName")
        or topic_mapping.get("outputTopic")
        or captured_event.topic_name
        or "micro-integration"
    )
    return _slug(str(basis))[:48] or "micro-integration"


def _entity_name(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    if not parts:
        return "event"
    if len(parts) > 1 and parts[-1].lower() in {"v1", "v2"}:
        return parts[-2].lower()
    return parts[-1].lower()


def _pascal(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Event"


def _title(value: str) -> str:
    return " ".join(part.capitalize() for part in _slug(value).split("-"))


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.lower()).strip("-")
    return slug or "micro-integration"


def _redact_url(value: str) -> str:
    return re.sub(r"//([^:@/]+):([^@/]+)@", "//***:***@", value)
