import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  AlertTriangle,
  Bot,
  Loader2,
  RefreshCw,
  SendHorizonal,
  Sparkles,
  UserRound,
} from "lucide-react";
import { H2 } from "@nous-research/ui";
import { api } from "@/lib/api";
import type {
  PaginatedSessions,
  SessionMessage,
  WorkbenchBootstrapResponse,
  WorkbenchRunEvent,
} from "@/lib/api";
import { MessageList } from "@/pages/SessionsPage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { timeAgo } from "@/lib/utils";

function formatApiError(error: unknown): string {
  if (!(error instanceof Error)) return String(error);

  const message = error.message || "Unknown error";
  const jsonStart = message.indexOf("{");
  if (jsonStart < 0) return message;

  const jsonPayload = message.slice(jsonStart);
  try {
    const parsed = JSON.parse(jsonPayload) as {
      detail?: { message?: string; request_id?: string };
      error?: { message?: string };
      message?: string;
    };

    const detail =
      parsed.detail?.message ?? parsed.error?.message ?? parsed.message;
    if (!detail) return message;

    const requestId = parsed.detail?.request_id;
    return requestId ? `${detail} (request: ${requestId})` : detail;
  } catch {
    return message;
  }
}

function appendAssistantDelta(
  messages: SessionMessage[],
  delta: string,
): SessionMessage[] {
  const next = [...messages];
  for (let i = next.length - 1; i >= 0; i--) {
    if (next[i].role === "assistant") {
      next[i] = {
        ...next[i],
        content: `${next[i].content ?? ""}${delta}`,
      };
      return next;
    }
  }
  next.push({ role: "assistant", content: delta });
  return next;
}

function ensureAssistantOutput(
  messages: SessionMessage[],
  output: string,
): SessionMessage[] {
  const next = [...messages];
  for (let i = next.length - 1; i >= 0; i--) {
    if (next[i].role === "assistant") {
      if (!next[i].content?.trim()) {
        next[i] = { ...next[i], content: output };
      }
      return next;
    }
  }
  next.push({ role: "assistant", content: output });
  return next;
}

export default function WorkbenchPage() {
  const [bootstrap, setBootstrap] =
    useState<WorkbenchBootstrapResponse | null>(null);
  const [bootstrapLoading, setBootstrapLoading] = useState(true);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);

  const [sessions, setSessions] = useState<PaginatedSessions["sessions"]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [messagesError, setMessagesError] = useState<string | null>(null);

  const [composer, setComposer] = useState("");
  const [runPending, setRunPending] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runRequestId, setRunRequestId] = useState<string | null>(null);
  const [streamRequestId, setStreamRequestId] = useState<string | null>(null);
  const [streamActivity, setStreamActivity] = useState<string[]>([]);

  const selectedSession = useMemo(
    () => sessions.find((session) => session.id === selectedSessionId) ?? null,
    [sessions, selectedSessionId],
  );

  const loadBootstrap = useCallback(async () => {
    setBootstrapLoading(true);
    setBootstrapError(null);

    try {
      const payload = await api.getWorkbenchBootstrap();
      setBootstrap(payload);
    } catch (error) {
      setBootstrapError(formatApiError(error));
    } finally {
      setBootstrapLoading(false);
    }
  }, []);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    setSessionsError(null);

    try {
      const payload = await api.getWorkbenchSessions(50, 0);
      setSessions(payload.sessions);
      setSelectedSessionId((current) => {
        if (current && payload.sessions.some((session) => session.id === current)) {
          return current;
        }
        return payload.sessions[0]?.id ?? null;
      });
    } catch (error) {
      setSessionsError(formatApiError(error));
      setSessions([]);
      setSelectedSessionId(null);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const loadMessages = useCallback(async (sessionId: string) => {
    setMessagesLoading(true);
    setMessagesError(null);

    try {
      const payload = await api.getWorkbenchSessionMessages(sessionId);
      setMessages(payload.messages);
    } catch (error) {
      setMessagesError(formatApiError(error));
      setMessages([]);
    } finally {
      setMessagesLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadBootstrap();
  }, [loadBootstrap]);

  useEffect(() => {
    if (!bootstrap) return;
    void loadSessions();
  }, [bootstrap, loadSessions]);

  useEffect(() => {
    if (!selectedSessionId) {
      setMessages([]);
      setMessagesError(null);
      return;
    }
    void loadMessages(selectedSessionId);
  }, [selectedSessionId, loadMessages]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const prompt = composer.trim();
    if (!prompt || runPending || bootstrapLoading || !bootstrap) return;

    setComposer("");
    setRunPending(true);
    setRunError(null);
    setRunRequestId(null);
    setStreamRequestId(null);
    setStreamActivity([]);

    const baseSessionId = selectedSessionId;

    setMessages((prev) => [
      ...prev,
      { role: "user", content: prompt, timestamp: Date.now() / 1000 },
      { role: "assistant", content: "" },
    ]);

    try {
      const runStart = await api.createWorkbenchRun({
        input: prompt,
        ...(baseSessionId ? { session_id: baseSessionId } : {}),
      });

      if (!runStart.runId) {
        throw new Error("Workbench run start returned an empty run_id");
      }

      setRunRequestId(runStart.requestId ?? null);

      if (runStart.sessionId && runStart.sessionId !== selectedSessionId) {
        setSelectedSessionId(runStart.sessionId);
      }

      const streamMeta = await api.streamWorkbenchRunEvents(
        runStart.runId,
        (evt: WorkbenchRunEvent) => {
          switch (evt.event) {
            case "message.delta":
              if (typeof evt.delta === "string" && evt.delta.length > 0) {
                setMessages((prev) => appendAssistantDelta(prev, evt.delta ?? ""));
              }
              break;
            case "reasoning.available":
              if (evt.text) {
                setStreamActivity((prev) => [
                  ...prev,
                  `Reasoning: ${evt.text}`,
                ]);
              }
              break;
            case "tool.started":
              setStreamActivity((prev) => [
                ...prev,
                `Tool started: ${evt.tool ?? "unknown"}`,
              ]);
              break;
            case "tool.completed":
              setStreamActivity((prev) => [
                ...prev,
                `Tool completed: ${evt.tool ?? "unknown"}${evt.error ? " (error)" : ""}`,
              ]);
              break;
            case "run.completed":
              if (typeof evt.output === "string" && evt.output.trim()) {
                setMessages((prev) => ensureAssistantOutput(prev, evt.output ?? ""));
              }
              break;
            case "run.failed":
              setRunError(evt.error ? `Run failed: ${evt.error}` : "Run failed.");
              break;
            default:
              break;
          }
        },
      );

      setStreamRequestId(streamMeta.requestId ?? null);

      const nextSessionId =
        streamMeta.sessionId ?? runStart.sessionId ?? baseSessionId ?? null;

      await loadSessions();
      if (nextSessionId) {
        setSelectedSessionId(nextSessionId);
        await loadMessages(nextSessionId);
      }
    } catch (error) {
      const detail = formatApiError(error);
      setRunError(detail);
      setMessages((prev) => {
        const next = [...prev];
        for (let i = next.length - 1; i >= 0; i--) {
          if (next[i].role === "assistant" && !next[i].content?.trim()) {
            next[i] = {
              ...next[i],
              role: "system",
              content: `⚠️ ${detail}`,
            };
            return next;
          }
        }
        return [...next, { role: "system", content: `⚠️ ${detail}` }];
      });
    } finally {
      setRunPending(false);
    }
  };

  const onRefreshSessions = () => {
    void loadSessions();
  };

  if (bootstrapLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
      </div>
    );
  }

  if (!bootstrap || bootstrapError) {
    return (
      <div className="flex flex-col gap-4">
        <div className="border border-destructive/40 bg-destructive/[0.08] p-4 text-destructive">
          <div className="flex items-center gap-2 text-sm font-medium">
            <AlertTriangle className="h-4 w-4" />
            Failed to load workbench bootstrap.
          </div>
          <p className="mt-2 text-xs text-destructive/80">
            {bootstrapError ?? "Unknown bootstrap error."}
          </p>
        </div>
        <Button variant="outline" onClick={() => void loadBootstrap()}>
          Retry bootstrap
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <section className="border border-border bg-background/40 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          <H2 variant="sm">Workbench</H2>
          <Badge variant="outline">tenant: {bootstrap.tenant.label}</Badge>
          <Badge variant={bootstrap.tenant.fallback ? "warning" : "success"}>
            {bootstrap.tenant.fallback ? "fallback" : "resolved"}
          </Badge>
          <Badge variant="secondary">source: {bootstrap.tenant.source}</Badge>
        </div>

        <p className="mt-2 text-xs text-muted-foreground">
          Tenant ID <span className="font-mono-ui">{bootstrap.tenant.id}</span>
          {bootstrap.tenant.fallback_reason
            ? ` · fallback_reason=${bootstrap.tenant.fallback_reason}`
            : ""}
          {bootstrap.workbench.request_id
            ? ` · bootstrap_request_id=${bootstrap.workbench.request_id}`
            : ""}
        </p>

        {bootstrap.workbench.ignored_browser_user_id && (
          <div className="mt-3 border border-warning/40 bg-warning/[0.08] p-3 text-xs text-warning">
            Browser user identifiers were ignored from: {" "}
            {bootstrap.workbench.ignored_browser_user_id_sources.join(", ") || "unknown"}
          </div>
        )}

        {runError && (
          <div className="mt-3 border border-destructive/40 bg-destructive/[0.08] p-3 text-xs text-destructive">
            <strong>Proxy/stream error:</strong> {runError}
          </div>
        )}
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-[290px_minmax(0,1fr)]">
        <aside className="border border-border bg-background/30 p-3">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Bot className="h-4 w-4 text-muted-foreground" />
              Sessions
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={onRefreshSessions}
              disabled={sessionsLoading}
              aria-label="Refresh sessions"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${sessionsLoading ? "animate-spin" : ""}`}
              />
            </Button>
          </div>

          {sessionsError && (
            <p className="mb-2 border border-destructive/30 bg-destructive/[0.08] p-2 text-xs text-destructive">
              {sessionsError}
            </p>
          )}

          <div className="flex max-h-[60vh] flex-col gap-2 overflow-y-auto pr-1">
            {sessions.map((session) => {
              const active = session.id === selectedSessionId;
              const title = session.title || session.preview || "Untitled session";
              return (
                <button
                  key={session.id}
                  type="button"
                  onClick={() => setSelectedSessionId(session.id)}
                  className={`w-full border p-2 text-left transition-colors ${
                    active
                      ? "border-primary/40 bg-primary/[0.08]"
                      : "border-border hover:bg-secondary/30"
                  }`}
                >
                  <p className="truncate text-xs font-medium">{title}</p>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {session.message_count} msgs · {timeAgo(session.last_active)}
                  </p>
                </button>
              );
            })}

            {sessions.length === 0 && !sessionsLoading && (
              <p className="py-8 text-center text-xs text-muted-foreground">
                No sessions yet for this tenant.
              </p>
            )}
          </div>
        </aside>

        <section className="flex min-h-[65vh] flex-col gap-3 border border-border bg-background/30 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium">
              {selectedSession
                ? `${selectedSession.title || "Untitled"} · ${selectedSession.id}`
                : "New tenant-scoped conversation"}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {runRequestId ? `run_request_id=${runRequestId}` : ""}
              {streamRequestId ? ` · stream_request_id=${streamRequestId}` : ""}
            </div>
          </div>

          <div className="min-h-[360px] flex-1 border border-border bg-background/50 p-3">
            {messagesLoading ? (
              <div className="flex h-full items-center justify-center">
                <Loader2 className="h-5 w-5 animate-spin text-primary" />
              </div>
            ) : messagesError ? (
              <div className="border border-destructive/40 bg-destructive/[0.08] p-3 text-xs text-destructive">
                {messagesError}
              </div>
            ) : messages.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
                <UserRound className="h-5 w-5" />
                <p className="text-sm">Send a prompt to start this tenant session.</p>
              </div>
            ) : (
              <MessageList messages={messages} autoScrollToBottom />
            )}
          </div>

          {streamActivity.length > 0 && (
            <div className="max-h-28 overflow-y-auto border border-warning/30 bg-warning/[0.06] p-2 text-[11px] text-warning">
              {streamActivity.map((line, index) => (
                <p key={`${line}-${index}`}>{line}</p>
              ))}
            </div>
          )}

          <form onSubmit={handleSubmit} className="flex flex-col gap-2">
            <textarea
              value={composer}
              onChange={(e) => setComposer(e.target.value)}
              disabled={runPending}
              rows={4}
              placeholder="Type your prompt for this tenant-scoped workbench..."
              className="flex min-h-[100px] w-full border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
            <div className="flex items-center justify-end">
              <Button
                type="submit"
                disabled={runPending || !composer.trim()}
                className="gap-2"
              >
                {runPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <SendHorizonal className="h-4 w-4" />
                )}
                {runPending ? "Streaming..." : "Send"}
              </Button>
            </div>
          </form>
        </section>
      </section>
    </div>
  );
}
