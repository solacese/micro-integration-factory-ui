"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Braces,
  ChevronLeft,
  ChevronRight,
  Code2,
  Container,
  EyeOff,
  Layers3,
  Play,
  Radio,
  RefreshCw,
  Send,
  ShieldCheck,
  Shuffle,
  Terminal
} from "lucide-react";
import type {
  ArtifactDetail,
  ArtifactSummary,
  BuilderSession,
  CapturedEvent,
  GenerationRunSummary,
  SolaceSubscriptionSummary,
  TransformLanguage,
  WorkerJob
} from "@spec2event/shared";
import {
  capturedEventsStreamUrl,
  createBuilderSession,
  createSolaceSubscription,
  generateBuilderSession,
  getArtifact,
  getArtifacts,
  getWorkerJob,
  listCapturedEvents,
  sendBuilderMessage,
  workspaceArchiveUrl
} from "@/lib/api";

function prettyJson(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}

function languageLabel(language: TransformLanguage) {
  return {
    java_sdk: "Java SDK",
    groovy: "Groovy",
    dataweave: "DataWeave"
  }[language];
}

const languageOptions: Array<{ value: TransformLanguage; label: string; note: string }> = [
  { value: "java_sdk", label: "Java SDK", note: "production default" },
  { value: "groovy", label: "Groovy", note: "script draft" },
  { value: "dataweave", label: "DataWeave", note: "dwl plus Java runtime" }
];

const templateOptions = [
  {
    key: "schema-validation",
    label: "Schema validation",
    note: "validate shape",
    icon: ShieldCheck,
    prompt:
      "Validate this payload against a clear JSON schema before publishing. Reject missing required fields, type mismatches, and malformed nested objects. Return a canonical valid event and describe the validation errors path for invalid payloads."
  },
  {
    key: "anonymization",
    label: "Anonymization",
    note: "mask sensitive data",
    icon: EyeOff,
    prompt:
      "Strip out sensitive data from ORDER and only return customer, items, total, and orderId when those fields exist. Mask or hash remaining direct identifiers, remove secrets and tokens, and explain which fields were anonymized."
  },
  {
    key: "aggregation",
    label: "Aggregation",
    note: "group events",
    icon: Layers3,
    prompt:
      "Aggregate related payloads into a summary event. Group by the strongest business key in the payload, compute useful counts and totals, preserve correlation metadata, and publish a compact aggregate event."
  },
  {
    key: "transformation",
    label: "Transformation",
    note: "map fields",
    icon: Shuffle,
    prompt:
      "Transform this payload into a canonical event model. Rename fields for clarity, normalize timestamps and money values, preserve source identifiers, and publish only the fields consumers need."
  }
];

export default function HomePage() {
  const [brokerUrl, setBrokerUrl] = useState("demo://sample");
  const [vpn, setVpn] = useState("default");
  const [username, setUsername] = useState("demo");
  const [password, setPassword] = useState("demo");
  const [topicFilter, setTopicFilter] = useState("orders/>");
  const [credentialsHidden, setCredentialsHidden] = useState(false);
  const [sourceCollapsed, setSourceCollapsed] = useState(false);
  const [subscription, setSubscription] = useState<SolaceSubscriptionSummary | null>(null);
  const [capturedEvents, setCapturedEvents] = useState<CapturedEvent[]>([]);
  const [selectedEvent, setSelectedEvent] = useState<CapturedEvent | null>(null);
  const [language, setLanguage] = useState<TransformLanguage>("java_sdk");
  const [builderSession, setBuilderSession] = useState<BuilderSession | null>(null);
  const [selectedTemplate, setSelectedTemplate] = useState<string | null>(null);
  const [chatInput, setChatInput] = useState("");
  const [generatedRun, setGeneratedRun] = useState<GenerationRunSummary | null>(null);
  const [workerJob, setWorkerJob] = useState<WorkerJob | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [activeArtifact, setActiveArtifact] = useState<ArtifactDetail | null>(null);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("Ready");
  const [liveDesignStatus, setLiveDesignStatus] = useState("Select a source event");
  const [liveDesigning, setLiveDesigning] = useState(false);
  const [questionChecklist, setQuestionChecklist] = useState<Record<string, boolean>>({});
  const eventSourceRef = useRef<EventSource | null>(null);
  const builderPaneRef = useRef<HTMLElement | null>(null);
  const outputPaneRef = useRef<HTMLElement | null>(null);
  const projectPaneRef = useRef<HTMLElement | null>(null);

  const questions = useMemo(
    () => builderSession?.draft?.qualifyingQuestions ?? [],
    [builderSession?.draft?.qualifyingQuestions]
  );
  const generationInProgress = Boolean(
    workerJob && !["completed", "failed", "partial"].includes(workerJob.status)
  );
  const checklistComplete =
    questions.length >= 2 && questions.every((question) => questionChecklist[question]);
  const readyToGenerate = Boolean(builderSession?.draft && checklistComplete);
  const generateDisabled = !readyToGenerate || working || generationInProgress;
  const generationFinished = Boolean(
    workerJob && ["completed", "failed", "partial"].includes(workerJob.status)
  );

  useEffect(() => {
    if (!subscription) return;
    eventSourceRef.current?.close();
    const source = new EventSource(capturedEventsStreamUrl(subscription.id));
    eventSourceRef.current = source;
    source.onmessage = (event) => {
      const captured = JSON.parse(event.data) as CapturedEvent;
      setCapturedEvents((current) => {
        if (current.some((item) => item.id === captured.id)) return current;
        return [...current, captured];
      });
      setSelectedEvent((current) => current ?? captured);
    };
    source.onerror = () => {
      setMessage("Capture stream reconnecting");
    };
    listCapturedEvents(subscription.id)
      .then((events) => {
        setCapturedEvents(events);
        setSelectedEvent((current) => current ?? events[0] ?? null);
      })
      .catch(() => {});
    return () => source.close();
  }, [subscription]);

  useEffect(() => {
    if (selectedEvent) {
      setSourceCollapsed(true);
      window.requestAnimationFrame(() => {
        builderPaneRef.current?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
          inline: "center"
        });
      });
    }
  }, [selectedEvent]);

  useEffect(() => {
    setQuestionChecklist((current) => {
      const next: Record<string, boolean> = {};
      for (const question of questions) {
        next[question] = current[question] ?? false;
      }
      return next;
    });
  }, [questions]);

  useEffect(() => {
    if (!workerJob || ["completed", "failed", "partial"].includes(workerJob.status)) return;
    const interval = window.setInterval(() => {
      getWorkerJob(workerJob.id)
        .then(setWorkerJob)
        .catch(() => {});
    }, 2000);
    return () => window.clearInterval(interval);
  }, [workerJob]);

  const selectedOutput = useMemo(() => {
    return builderSession?.preview ?? builderSession?.draft?.sampleOutput ?? {};
  }, [builderSession]);

  const draftFiles = builderSession?.draft?.files ?? [];

  async function applyTemplate(templateKey: string) {
    const template = templateOptions.find((option) => option.key === templateKey);
    if (!template) return;
    setSelectedTemplate(template.key);
    setChatInput(template.prompt);
    setQuestionChecklist({});
    await submitPrompt(
      template.prompt,
      `Preparing a ${template.label.toLowerCase()} micro integration draft`
    );
  }

  async function startCapture() {
    setWorking(true);
    setMessage("Starting capture");
    try {
      const nextSubscription = await createSolaceSubscription({
        brokerUrl,
        vpn,
        username,
        password,
        topicFilter
      });
      setSubscription(nextSubscription);
      setCapturedEvents([]);
      setSelectedEvent(null);
      setCredentialsHidden(true);
      setSourceCollapsed(false);
      setBuilderSession(null);
      setQuestionChecklist({});
      setSelectedTemplate(null);
      setChatInput("");
      setMessage(nextSubscription.message);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Capture failed");
    } finally {
      setWorking(false);
    }
  }

  async function ensureSession() {
    if (builderSession) return builderSession;
    if (!selectedEvent) throw new Error("Select an event first");
    const session = await createBuilderSession({
      capturedEventId: selectedEvent.id,
      transformLanguage: language
    });
    setBuilderSession(session);
    return session;
  }

  async function submitPrompt(content: string, status: string) {
    const prompt = content.trim();
    if (!prompt || working || liveDesigning) return;
    setWorking(true);
    setLiveDesigning(true);
    setMessage("Drafting integration");
    setLiveDesignStatus(status);
    try {
      const session = await ensureSession();
      const updated = await sendBuilderMessage(session.id, {
        content: prompt,
        transformLanguage: language
      });
      setBuilderSession(updated);
      setChatInput("");
      setMessage("Draft updated");
      setLiveDesignStatus("Draft updated. Answer the checklist, then generate the project.");
      window.requestAnimationFrame(() => {
        outputPaneRef.current?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
          inline: "center"
        });
      });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to draft");
    } finally {
      setWorking(false);
      setLiveDesigning(false);
    }
  }

  async function sendMessage() {
    await submitPrompt(chatInput, "Asking qualifying questions and drafting");
  }

  async function generate() {
    if (generateDisabled) return;
    setWorking(true);
    setMessage("Generating project");
    setLiveDesignStatus("Rendering the template project around the current transform");
    try {
      const session = await ensureSession();
      const result = await generateBuilderSession(session.id);
      setBuilderSession(result.session);
      setGeneratedRun(result.run);
      setWorkerJob(result.workerJob);
      setMessage("Project generated");
      setLiveDesignStatus(
        "Project generated. Security, performance, Maven, and Docker checks are running."
      );
      const nextArtifacts = await getArtifacts(result.run.id);
      setArtifacts(nextArtifacts);
      if (nextArtifacts[0]) {
        setActiveArtifact(await getArtifact(result.run.id, nextArtifacts[0].id));
      }
      window.requestAnimationFrame(() => {
        projectPaneRef.current?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
          inline: "center"
        });
      });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Generation failed");
    } finally {
      setWorking(false);
    }
  }

  async function openArtifact(artifact: ArtifactSummary) {
    if (!generatedRun) return;
    setActiveArtifact(await getArtifact(generatedRun.id, artifact.id));
  }

  return (
    <main className="workbench" data-source-collapsed={sourceCollapsed}>
      <section
        className="pane left-pane"
        data-collapsed={sourceCollapsed}
        aria-label="Solace capture"
      >
        {sourceCollapsed ? (
          <button
            type="button"
            className="source-reopen-button"
            aria-label="Show source"
            onClick={() => setSourceCollapsed(false)}
          >
            <ChevronRight size={18} />
          </button>
        ) : (
          <>
            <div className="pane-head">
              <div>
                <span className="step-kicker">1 Source</span>
                <h1>Live Payload</h1>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="Hide source"
                onClick={() => setSourceCollapsed(true)}
              >
                <ChevronLeft size={17} />
              </button>
            </div>
            {subscription && credentialsHidden ? (
              <div className="connection-card">
                <div>
                  <span>Connected</span>
                  <strong>{subscription.topicFilter}</strong>
                  <small>{brokerUrl}</small>
                </div>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => setCredentialsHidden(false)}
                >
                  <EyeOff size={16} />
                  <span>Show credentials</span>
                </button>
              </div>
            ) : (
              <>
                <div className="form-grid compact-form">
                  <label className="wide">
                    <span>Broker URL</span>
                    <input value={brokerUrl} onChange={(event) => setBrokerUrl(event.target.value)} />
                  </label>
                  <label>
                    <span>VPN</span>
                    <input value={vpn} onChange={(event) => setVpn(event.target.value)} />
                  </label>
                  <label>
                    <span>Topic</span>
                    <input
                      value={topicFilter}
                      onChange={(event) => setTopicFilter(event.target.value)}
                    />
                  </label>
                  <label>
                    <span>Username</span>
                    <input value={username} onChange={(event) => setUsername(event.target.value)} />
                  </label>
                  <label>
                    <span>Password</span>
                    <input
                      type="password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                    />
                  </label>
                </div>

                <button
                  type="button"
                  className="action-button"
                  disabled={working}
                  onClick={startCapture}
                >
                  <Radio size={18} />
                  <span>{subscription ? "Reconnect" : "Listen"}</span>
                </button>
              </>
            )}

            <div className="event-list">
              <div className="section-row">
                <strong>Events</strong>
                <span>{capturedEvents.length}</span>
              </div>
              {capturedEvents.map((event) => (
                <button
                  type="button"
                  className="event-row"
                  data-active={selectedEvent?.id === event.id}
                  key={event.id}
                  onClick={() => {
                    setSelectedEvent(event);
                    setSourceCollapsed(true);
                    setBuilderSession(null);
                    setQuestionChecklist({});
                    setGeneratedRun(null);
                    setWorkerJob(null);
                    setArtifacts([]);
                    setActiveArtifact(null);
                  }}
                >
                  <Activity size={16} />
                  <span>{event.topicName}</span>
                  <ChevronRight size={16} />
                </button>
              ))}
            </div>

            <div className="payload-panel">
              <div className="section-row">
                <strong>Payload</strong>
                <Braces size={16} />
              </div>
              <pre>{prettyJson(selectedEvent?.payload)}</pre>
            </div>
          </>
        )}
      </section>

      <section className="pane chat-pane" aria-label="AI builder" ref={builderPaneRef}>
        <div className="pane-head">
          <div>
            <span className="step-kicker">2 Builder</span>
            <h2>Builder</h2>
          </div>
          <div className="select-wrap">
            <select
              value={language}
              onChange={(event) => setLanguage(event.target.value as TransformLanguage)}
            >
              {languageOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {working || liveDesigning ? (
          <div className="builder-activity" aria-live="polite">
            <RefreshCw size={14} className="spin" />
            <span>{message}</span>
          </div>
        ) : null}

        <div className="messages">
          {builderSession?.draft?.assistantMessage ? (
            <article className="message" data-role="assistant">
              <span>assistant</span>
              <p>{builderSession.draft.assistantMessage}</p>
            </article>
          ) : null}
          {working || liveDesigning ? (
            <article className="message status-message" data-role="assistant" aria-live="polite">
              <span>working</span>
              <p>{liveDesignStatus}</p>
            </article>
          ) : null}
          {builderSession?.draft?.qualifyingQuestions?.length ? (
            <article className="message" data-role="assistant">
              <span>questions</span>
              <ul className="question-list">
                {builderSession.draft.qualifyingQuestions.map((question) => (
                  <li key={question}>{question}</li>
                ))}
              </ul>
            </article>
          ) : null}
          {(builderSession?.messages ?? []).map((chat) => (
            <article key={chat.id} className="message" data-role={chat.role}>
              <span>{chat.role}</span>
              <p>{chat.content}</p>
            </article>
          ))}
        </div>

        {questions.length ? (
          <div className="question-checklist">
            <div className="section-row">
              <strong>Question checklist</strong>
              <span>{readyToGenerate ? "Ready to generate" : "Answer and check"}</span>
            </div>
            <div className="question-items">
              {questions.map((question) => (
                <label key={question} className="question-item">
                  <input
                    type="checkbox"
                    checked={Boolean(questionChecklist[question])}
                    onChange={(event) =>
                      setQuestionChecklist((current) => ({
                        ...current,
                        [question]: event.target.checked
                      }))
                    }
                  />
                  <span>{question}</span>
                </label>
              ))}
            </div>
          </div>
        ) : null}

        {readyToGenerate ? <div className="ready-strip">Ready to generate</div> : null}

        <div className="composer">
          <textarea
            placeholder={`Describe the ${languageLabel(language)} transform.`}
            value={chatInput}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                if (!working && chatInput.trim()) {
                  void sendMessage();
                }
              }
            }}
            onChange={(event) => {
              setSelectedTemplate(null);
              setChatInput(event.target.value);
            }}
          />
          <div className="composer-actions">
            <button
              type="button"
              className="icon-button primary"
              onClick={sendMessage}
              disabled={working || !chatInput.trim()}
            >
              <Send size={17} />
            </button>
          </div>
        </div>

        <div className="template-block">
          <div className="section-row template-head">
            <strong>Templates</strong>
            <span>start here</span>
          </div>
          <div className="template-grid">
            {templateOptions.map((template) => {
              const Icon = template.icon;
              return (
                <button
                  type="button"
                  className="template-button"
                  data-active={selectedTemplate === template.key}
                  key={template.key}
                  disabled={working || liveDesigning}
                  onClick={() => void applyTemplate(template.key)}
                >
                  <Icon size={16} />
                  <span>{template.label}</span>
                  <small>{template.note}</small>
                </button>
              );
            })}
          </div>
        </div>
      </section>

      <section className="pane right-pane" aria-label="Output" ref={outputPaneRef}>
        <div className="pane-head">
          <div>
            <span className="step-kicker">3 Output</span>
            <h2>Sample Result</h2>
          </div>
        </div>

        <div className="output-preview">
          <pre>{prettyJson(selectedOutput)}</pre>
        </div>

        <button
          type="button"
          className="generate-button"
          disabled={generateDisabled}
          data-ready={readyToGenerate && !generationInProgress}
          onClick={generate}
        >
          <Play size={18} />
          <span>
            {generationInProgress
              ? "Generating Micro Integration"
              : generationFinished
                ? "Generated Micro Integration"
                : readyToGenerate
                  ? "Generate Micro Integration"
                  : "Complete Checklist"}
          </span>
        </button>
      </section>

      <section className="pane project-pane" aria-label="Project" ref={projectPaneRef}>
        <div className="project-sidebar">
          <div className="section-row">
            <strong>Project</strong>
            {generatedRun ? (
              <a className="project-download" href={workspaceArchiveUrl(generatedRun.id)}>
                VS Code project
              </a>
            ) : (
              <Code2 size={16} />
            )}
          </div>
          <div className="file-tree">
            {artifacts.length
              ? artifacts.map((artifact) => (
                  <button
                    type="button"
                    key={artifact.id}
                    data-active={activeArtifact?.id === artifact.id}
                    onClick={() => void openArtifact(artifact)}
                  >
                    {artifact.path}
                  </button>
                ))
              : draftFiles.map((file) => (
                  <div className="file-placeholder" key={file.path}>
                    <span>{file.path}</span>
                    <small>{file.purpose}</small>
                  </div>
                ))}
          </div>
        </div>

        <div className="code-surface">
          <div className="section-row">
            <strong>{activeArtifact?.path ?? "Generated code"}</strong>
            <Terminal size={16} />
          </div>
          <pre>{activeArtifact?.content ?? prettyJson(builderSession?.draft)}</pre>
        </div>

        <div className="worker-panel">
          <div className="section-row">
            <strong>Tests & Build</strong>
            <Container size={16} />
          </div>
          <div className="worker-status">
            <span>{workerJob?.status ?? "idle"}</span>
            {workerJob && !["completed", "failed", "partial"].includes(workerJob.status) ? (
              <RefreshCw size={15} className="spin" />
            ) : null}
          </div>
          <pre>
            {workerJob?.logs ||
              "Security, performance, Maven, VS Code, and Docker logs appear here after generation."}
          </pre>
        </div>
      </section>
    </main>
  );
}
