"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Braces,
  ChevronLeft,
  ChevronRight,
  CheckCircle2,
  EyeOff,
  Layers3,
  Play,
  Radio,
  Send,
  ShieldCheck,
  Shuffle,
} from "lucide-react";
import type {
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function byteSize(value: unknown) {
  return new TextEncoder().encode(JSON.stringify(value ?? {})).length;
}

function formatBytes(value: unknown) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "pending";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatThroughput(value: unknown) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "pending";
  return `${Math.round(value).toLocaleString()} events/s`;
}

function resultNumber(result: Record<string, unknown>, key: string) {
  const value = result[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
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
  const [working, setWorking] = useState(false);
  const [liveDesignStatus, setLiveDesignStatus] = useState("Select a source event");
  const [liveDesigning, setLiveDesigning] = useState(false);
  const [questionChecklist, setQuestionChecklist] = useState<Record<string, boolean>>({});
  const eventSourceRef = useRef<EventSource | null>(null);
  const builderPaneRef = useRef<HTMLElement | null>(null);
  const outputPaneRef = useRef<HTMLElement | null>(null);

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
  const chatMessages = builderSession?.messages ?? [];
  const hasAssistantMessage = chatMessages.some((chat) => chat.role === "assistant");
  const showStatusMessage =
    working || liveDesigning || liveDesignStatus !== "Select a source event";

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
      setLiveDesignStatus("Capture stream reconnecting");
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

  const topicDestination =
    builderSession?.draft?.topicMapping?.outputTopic ??
    generatedRun?.canonicalModel?.topics?.[0] ??
    "Waiting for transform";
  const workerResult = isRecord(workerJob?.result) ? workerJob.result : {};
  const sampleInputBytes =
    resultNumber(workerResult, "sampleInputBytes") ?? byteSize(selectedEvent?.payload);
  const sampleOutputBytes =
    resultNumber(workerResult, "sampleOutputBytes") ?? byteSize(selectedOutput);
  const estimatedThroughput =
    resultNumber(workerResult, "eventThroughputPerSecond") ??
    Math.max(1000, Math.min(50000, Math.round(120_000_000 / Math.max(sampleInputBytes, 512))));
  const p95Latency = resultNumber(workerResult, "p95LatencyMs");
  const imageSize = resultNumber(workerResult, "imageSizeBytes");
  const templatesVisible = !selectedTemplate && !builderSession?.draft && !chatInput.trim();

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
    setLiveDesignStatus("Starting capture");
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
      setLiveDesignStatus(nextSubscription.message);
    } catch (error) {
      setLiveDesignStatus(error instanceof Error ? error.message : "Capture failed");
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
    setLiveDesignStatus(status);
    try {
      const session = await ensureSession();
      const updated = await sendBuilderMessage(session.id, {
        content: prompt,
        transformLanguage: language
      });
      setBuilderSession(updated);
      setChatInput("");
      setLiveDesignStatus("Draft updated. Answer the checklist, then generate the project.");
      window.requestAnimationFrame(() => {
        outputPaneRef.current?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
          inline: "center"
        });
      });
    } catch (error) {
      setLiveDesignStatus(error instanceof Error ? error.message : "Unable to draft");
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
    setLiveDesignStatus("Rendering the template project around the current transform");
    try {
      const session = await ensureSession();
      const result = await generateBuilderSession(session.id);
      setBuilderSession(result.session);
      setGeneratedRun(result.run);
      setWorkerJob(result.workerJob);
      setLiveDesignStatus(
        "Project generated. Security, performance, Maven, and Docker checks are running."
      );
      window.requestAnimationFrame(() => {
        outputPaneRef.current?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
          inline: "center"
        });
      });
    } catch (error) {
      setLiveDesignStatus(error instanceof Error ? error.message : "Generation failed");
    } finally {
      setWorking(false);
    }
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

        <div className="chat-box">
          <div className="messages">
            {!chatMessages.length && !builderSession?.draft ? (
              <article className="message system-message" data-role="assistant">
                <span>builder</span>
                <p>
                  {selectedEvent
                    ? "Pick a template or describe a small transform. I will ask two or three questions, then generate a focused micro integration."
                    : "Select a live event first. Then choose a template or describe the transform."}
                </p>
              </article>
            ) : null}
            {templatesVisible ? (
              <article className="message template-message" data-role="assistant">
                <span>start with</span>
                <div className="template-grid">
                  {templateOptions.map((template) => {
                    const Icon = template.icon;
                    return (
                      <button
                        type="button"
                        className="template-button"
                        key={template.key}
                        disabled={working || liveDesigning || !selectedEvent}
                        onClick={() => void applyTemplate(template.key)}
                      >
                        <Icon size={16} />
                        <span>{template.label}</span>
                        <small>{template.note}</small>
                      </button>
                    );
                  })}
                </div>
              </article>
            ) : null}
            {chatMessages.map((chat) => (
              <article key={chat.id} className="message" data-role={chat.role}>
                <span>{chat.role}</span>
                <p>{chat.content}</p>
              </article>
            ))}
            {builderSession?.draft?.assistantMessage && !hasAssistantMessage ? (
              <article className="message" data-role="assistant">
                <span>builder</span>
                <p>{builderSession.draft.assistantMessage}</p>
              </article>
            ) : null}
            {showStatusMessage ? (
              <article className="message status-message" data-role="assistant" aria-live="polite">
                <span>working</span>
                <p>{liveDesignStatus}</p>
              </article>
            ) : null}
            {questions.length ? (
              <article className="message checklist-message" data-role="assistant">
                <span>{readyToGenerate ? "ready" : "question checklist"}</span>
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
                {readyToGenerate ? (
                  <div className="ready-strip">
                    <CheckCircle2 size={14} />
                    <span>Ready to generate</span>
                  </div>
                ) : null}
              </article>
            ) : null}
          </div>

          <div className="composer">
            <textarea
              placeholder={
                selectedEvent
                  ? `Describe the ${languageLabel(language)} transform.`
                  : "Select a source event first."
              }
              value={chatInput}
              disabled={!selectedEvent}
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
            <button
              type="button"
              className="send-button"
              aria-label="Send"
              onClick={sendMessage}
              disabled={working || !chatInput.trim() || !selectedEvent}
            >
              <Send size={18} />
            </button>
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

        <div className="output-metadata">
          <div className="topic-destination">
            <span>Topic destination</span>
            <strong>{topicDestination}</strong>
          </div>
          <div className="benchmark-grid" aria-label="Generation benchmarks">
            <div>
              <span>Event throughput</span>
              <strong>{formatThroughput(estimatedThroughput)}</strong>
            </div>
            <div>
              <span>Image size</span>
              <strong>{formatBytes(imageSize)}</strong>
            </div>
            <div>
              <span>Payload</span>
              <strong>{formatBytes(sampleInputBytes)}</strong>
            </div>
            <div>
              <span>Sample output</span>
              <strong>{formatBytes(sampleOutputBytes)}</strong>
            </div>
            <div>
              <span>p95 latency</span>
              <strong>{p95Latency ? `${p95Latency.toFixed(2)} ms` : "pending"}</strong>
            </div>
            <div>
              <span>Build</span>
              <strong>{workerJob?.status ?? "idle"}</strong>
            </div>
          </div>
          {generatedRun ? (
            <a className="project-download" href={workspaceArchiveUrl(generatedRun.id)}>
              VS Code project
            </a>
          ) : null}
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
    </main>
  );
}
