import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import {
    AlertTriangle,
    Bot,
    CheckCircle2,
    FileDown,
    FileUp,
    Loader2,
    Paperclip,
    RefreshCw,
    SendHorizonal,
    Sparkles,
    UserRound,
    XCircle,
} from "lucide-react";
import { H2 } from "@nous-research/ui";
import {
    api,
    buildWorkbenchRunPayload,
    downloadWorkbenchFile,
    getWorkbenchFileDownloadUrl,
} from "@/lib/api";
import type {
    PaginatedSessions,
    SessionMessage,
    WorkbenchBootstrapResponse,
    WorkbenchFileMetadata,
    WorkbenchRunEvent,
    WorkbenchRunOutputFile,
} from "@/lib/api";
import { MessageList } from "@/pages/SessionsPage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { timeAgo } from "@/lib/utils";
import { useToast } from "@/hooks/useToast";
import { Toast } from "@/components/Toast";

type ActivityKind = "run" | "reasoning" | "tool" | "file" | "stream";
type ActivityStatus = "info" | "success" | "error";

interface WorkspaceActivityEntry {
    id: string;
    kind: ActivityKind;
    status: ActivityStatus;
    message: string;
    timestamp: number;
    requestId?: string;
}

interface WorkspaceGeneratedFile {
    id: string;
    filename: string;
    sizeBytes?: number;
    mimeType?: string;
    downloadUrl: string;
}

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

function formatBytes(value?: number): string {
    if (typeof value !== "number" || Number.isNaN(value) || value < 0) {
        return "-";
    }
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatClock(timestamp: number): string {
    return new Date(timestamp).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    });
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

function normalizeGeneratedFiles(
    payload: WorkbenchRunOutputFile[] | undefined,
): WorkspaceGeneratedFile[] {
    if (!Array.isArray(payload)) return [];

    const normalized: WorkspaceGeneratedFile[] = [];
    for (const item of payload) {
        if (!item || typeof item.file_id !== "string" || !item.file_id.trim()) {
            continue;
        }

        const id = item.file_id.trim();
        normalized.push({
            id,
            filename:
                item.filename?.trim() || item.file?.filename?.trim() || "generated.bin",
            sizeBytes:
                typeof item.size_bytes === "number"
                    ? item.size_bytes
                    : typeof item.file?.bytes === "number"
                        ? item.file.bytes
                        : undefined,
            mimeType: item.mime_type || item.file?.mime_type,
            downloadUrl: getWorkbenchFileDownloadUrl(id),
        });
    }

    return normalized;
}

function mergeGeneratedFiles(
    existing: WorkspaceGeneratedFile[],
    incoming: WorkspaceGeneratedFile[],
): WorkspaceGeneratedFile[] {
    if (incoming.length === 0) return existing;

    const byId = new Map(existing.map((file) => [file.id, file]));
    for (const file of incoming) {
        byId.set(file.id, file);
    }
    return Array.from(byId.values()).sort((a, b) => a.filename.localeCompare(b.filename));
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

    const [retainedFiles, setRetainedFiles] = useState<WorkbenchFileMetadata[]>([]);
    const [filesLoading, setFilesLoading] = useState(false);
    const [filesError, setFilesError] = useState<string | null>(null);
    const [filesRequestId, setFilesRequestId] = useState<string | null>(null);
    const [selectedFileIds, setSelectedFileIds] = useState<string[]>([]);

    const [uploadPending, setUploadPending] = useState(false);
    const [uploadError, setUploadError] = useState<string | null>(null);
    const [uploadRequestId, setUploadRequestId] = useState<string | null>(null);

    const [composer, setComposer] = useState("");
    const [runPending, setRunPending] = useState(false);
    const [runError, setRunError] = useState<string | null>(null);
    const [runRequestId, setRunRequestId] = useState<string | null>(null);
    const [streamRequestId, setStreamRequestId] = useState<string | null>(null);

    const [generatedFiles, setGeneratedFiles] = useState<WorkspaceGeneratedFile[]>([]);
    const [activityEntries, setActivityEntries] = useState<WorkspaceActivityEntry[]>([]);

    const activityIdRef = useRef(0);
    const fileInputRef = useRef<HTMLInputElement | null>(null);
    const { toast, showToast } = useToast();

    const selectedSession = useMemo(
        () => sessions.find((session) => session.id === selectedSessionId) ?? null,
        [sessions, selectedSessionId],
    );

    const filesById = useMemo(() => {
        const map = new Map<string, WorkbenchFileMetadata>();
        for (const file of retainedFiles) {
            map.set(file.id, file);
        }
        return map;
    }, [retainedFiles]);

    const selectedFiles = useMemo(
        () => selectedFileIds.map((id) => filesById.get(id)).filter(Boolean) as WorkbenchFileMetadata[],
        [filesById, selectedFileIds],
    );

    const appendActivity = useCallback(
        (
            kind: ActivityKind,
            status: ActivityStatus,
            message: string,
            requestId?: string,
        ) => {
            activityIdRef.current += 1;
            const entry: WorkspaceActivityEntry = {
                id: `activity-${activityIdRef.current}`,
                kind,
                status,
                message,
                timestamp: Date.now(),
                ...(requestId ? { requestId } : {}),
            };
            setActivityEntries((prev) => [entry, ...prev].slice(0, 200));
        },
        [],
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
            const detail = formatApiError(error);
            setSessionsError(detail);
            setSessions([]);
            setSelectedSessionId(null);
            appendActivity("stream", "error", `Session reload failed: ${detail}`);
        } finally {
            setSessionsLoading(false);
        }
    }, [appendActivity]);

    const loadMessages = useCallback(async (sessionId: string) => {
        setMessagesLoading(true);
        setMessagesError(null);

        try {
            const payload = await api.getWorkbenchSessionMessages(sessionId);
            setMessages(payload.messages);
        } catch (error) {
            const detail = formatApiError(error);
            setMessagesError(detail);
            setMessages([]);
            appendActivity("stream", "error", `Message reload failed: ${detail}`);
        } finally {
            setMessagesLoading(false);
        }
    }, [appendActivity]);

    const loadFiles = useCallback(async () => {
        setFilesLoading(true);
        setFilesError(null);

        try {
            const payload = await api.getWorkbenchFiles();
            setFilesRequestId(payload.requestId ?? null);
            setRetainedFiles(payload.files);
            setSelectedFileIds((current) => {
                const next = current.filter((fileId) =>
                    payload.files.some((file) => file.id === fileId),
                );
                return next;
            });
        } catch (error) {
            const detail = formatApiError(error);
            setFilesError(detail);
            appendActivity("file", "error", `File list failed: ${detail}`);
            showToast(`File list failed: ${detail}`, "error");
        } finally {
            setFilesLoading(false);
        }
    }, [appendActivity, showToast]);

    useEffect(() => {
        void loadBootstrap();
    }, [loadBootstrap]);

    useEffect(() => {
        if (!bootstrap) return;
        void loadSessions();
        void loadFiles();
    }, [bootstrap, loadFiles, loadSessions]);

    useEffect(() => {
        if (!selectedSessionId) {
            setMessages([]);
            setMessagesError(null);
            return;
        }
        void loadMessages(selectedSessionId);
    }, [selectedSessionId, loadMessages]);

    useEffect(() => {
        if (selectedFileIds.length === 0) return;
        const knownIds = new Set(retainedFiles.map((file) => file.id));
        const missingIds = selectedFileIds.filter((fileId) => !knownIds.has(fileId));
        if (missingIds.length === 0) return;

        const detail = `Detached unavailable file id(s): ${missingIds.join(", ")}`;
        setSelectedFileIds((current) => current.filter((fileId) => knownIds.has(fileId)));
        setFilesError(detail);
        appendActivity("file", "error", detail);
    }, [appendActivity, retainedFiles, selectedFileIds]);

    useEffect(() => {
        const derived = retainedFiles
            .filter((file) => file.source === "assistant_output")
            .map((file) => ({
                id: file.id,
                filename: file.filename,
                sizeBytes: file.bytes,
                mimeType: file.mime_type,
                downloadUrl: getWorkbenchFileDownloadUrl(file.id),
            } satisfies WorkspaceGeneratedFile));

        if (derived.length === 0) return;
        setGeneratedFiles((current) => mergeGeneratedFiles(current, derived));
    }, [retainedFiles]);

    const toggleFileSelection = (fileId: string) => {
        setSelectedFileIds((current) => {
            if (current.includes(fileId)) {
                return current.filter((existing) => existing !== fileId);
            }
            return [...current, fileId];
        });
    };

    const handleUploadInputChange = async (event: ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        event.target.value = "";
        if (!file || runPending) return;

        setUploadPending(true);
        setUploadError(null);

        try {
            const uploaded = await api.uploadWorkbenchFile(file, {
                filename: file.name,
                purpose: "uploads",
                source: "upload",
            });

            setUploadRequestId(uploaded.requestId ?? null);
            setRetainedFiles((current) => {
                const deduped = current.filter((existing) => existing.id !== uploaded.file.id);
                return [uploaded.file, ...deduped];
            });
            setSelectedFileIds((current) =>
                current.includes(uploaded.file.id)
                    ? current
                    : [...current, uploaded.file.id],
            );

            appendActivity(
                "file",
                "success",
                `Uploaded ${uploaded.file.filename} (${formatBytes(uploaded.file.bytes)})`,
                uploaded.requestId,
            );
            showToast(`Uploaded ${uploaded.file.filename}`, "success");
        } catch (error) {
            const detail = formatApiError(error);
            setUploadError(detail);
            appendActivity("file", "error", `Upload failed: ${detail}`);
            showToast(`Upload failed: ${detail}`, "error");
        } finally {
            setUploadPending(false);
        }
    };

    const handleDownloadFile = useCallback(async (
        file: { id: string; filename: string },
    ) => {
        try {
            const downloaded = await downloadWorkbenchFile(file.id);
            const objectUrl = window.URL.createObjectURL(downloaded.blob);
            const link = document.createElement("a");
            link.href = objectUrl;
            link.download = file.filename;
            link.rel = "noopener";
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 1000);
            appendActivity("file", "success", `Downloaded ${file.filename}`, downloaded.requestId);
            showToast(`Downloaded ${file.filename}`, "success");
        } catch (error) {
            const detail = formatApiError(error);
            appendActivity("file", "error", `Download failed: ${detail}`);
            showToast(`Download failed: ${detail}`, "error");
        }
    }, [appendActivity, showToast]);

    const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();

        const prompt = composer.trim();
        if (!prompt) {
            setRunError("Prompt cannot be empty.");
            showToast("Prompt cannot be empty.", "error");
            return;
        }

        if (runPending || bootstrapLoading || !bootstrap) return;

        setComposer("");
        setRunPending(true);
        setRunError(null);
        setRunRequestId(null);
        setStreamRequestId(null);

        const baseSessionId = selectedSessionId;

        setMessages((prev) => [
            ...prev,
            { role: "user", content: prompt, timestamp: Date.now() / 1000 },
            { role: "assistant", content: "" },
        ]);

        appendActivity(
            "run",
            "info",
            `Run started${selectedFileIds.length > 0 ? ` with ${selectedFileIds.length} attached file(s)` : ""}.`,
        );

        try {
            const payload = buildWorkbenchRunPayload({
                prompt,
                sessionId: baseSessionId,
                selectedFileIds,
            });

            const runStart = await api.createWorkbenchRun(payload);
            setRunRequestId(runStart.requestId ?? null);

            appendActivity(
                "run",
                "info",
                `Upstream run started: ${runStart.runId}`,
                runStart.requestId,
            );

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
                                appendActivity("reasoning", "info", evt.text);
                            }
                            break;
                        case "tool.started":
                            appendActivity(
                                "tool",
                                "info",
                                `Tool started: ${evt.tool ?? "unknown"}`,
                            );
                            break;
                        case "tool.completed": {
                            const hadError = evt.error === true;
                            const durationSuffix =
                                typeof evt.duration === "number" && Number.isFinite(evt.duration)
                                    ? ` (${evt.duration.toFixed(2)}s)`
                                    : "";
                            appendActivity(
                                "tool",
                                hadError ? "error" : "success",
                                `Tool ${hadError ? "failed" : "completed"}: ${evt.tool ?? "unknown"}${durationSuffix}`,
                            );
                            break;
                        }
                        case "run.completed": {
                            if (typeof evt.output === "string" && evt.output.trim()) {
                                setMessages((prev) => ensureAssistantOutput(prev, evt.output ?? ""));
                            }
                            const outputFiles = normalizeGeneratedFiles(evt.files);
                            if (outputFiles.length > 0) {
                                setGeneratedFiles((prev) => mergeGeneratedFiles(prev, outputFiles));
                            }
                            appendActivity(
                                "run",
                                "success",
                                `Run completed${outputFiles.length > 0 ? ` with ${outputFiles.length} generated file(s)` : ""}.`,
                            );
                            break;
                        }
                        case "run.failed": {
                            const detail =
                                typeof evt.error === "string" && evt.error.trim()
                                    ? evt.error
                                    : "Run failed.";
                            setRunError(`Run failed: ${detail}`);
                            appendActivity("run", "error", `Run failed: ${detail}`);
                            break;
                        }
                        default:
                            // Unknown stream events are ignored to keep parser + UI resilient.
                            break;
                    }
                },
            );

            setStreamRequestId(streamMeta.requestId ?? null);

            const nextSessionId =
                streamMeta.sessionId ?? runStart.sessionId ?? baseSessionId ?? null;

            await Promise.all([loadSessions(), loadFiles()]);
            if (nextSessionId) {
                setSelectedSessionId(nextSessionId);
                await loadMessages(nextSessionId);
            }
        } catch (error) {
            const detail = formatApiError(error);
            setRunError(detail);
            appendActivity("run", "error", `Run error: ${detail}`);

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
            showToast(`Run failed: ${detail}`, "error");
        } finally {
            setRunPending(false);
        }
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
        <>
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
                            Browser user identifiers were ignored from:{" "}
                            {bootstrap.workbench.ignored_browser_user_id_sources.join(", ") || "unknown"}
                        </div>
                    )}

                    {runError && (
                        <div className="mt-3 border border-destructive/40 bg-destructive/[0.08] p-3 text-xs text-destructive">
                            <strong>Proxy/stream error:</strong> {runError}
                        </div>
                    )}
                </section>

                <section className="grid grid-cols-1 gap-4 xl:grid-cols-[290px_minmax(0,1fr)_340px]">
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
                                onClick={() => void loadSessions()}
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
                                        className={`w-full border p-2 text-left transition-colors ${active
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

                        {selectedFiles.length > 0 && (
                            <div className="flex flex-wrap gap-2 border border-border/70 bg-background/60 p-2 text-[11px]">
                                {selectedFiles.map((file) => (
                                    <button
                                        key={file.id}
                                        type="button"
                                        className="inline-flex items-center gap-1 border border-primary/40 bg-primary/[0.08] px-2 py-1 text-primary"
                                        onClick={() => toggleFileSelection(file.id)}
                                        title="Remove attachment"
                                    >
                                        <Paperclip className="h-3 w-3" />
                                        {file.filename}
                                    </button>
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
                            <div className="flex items-center justify-between gap-2">
                                <span className="text-[11px] text-muted-foreground">
                                    {selectedFileIds.length > 0
                                        ? `${selectedFileIds.length} retained file(s) attached`
                                        : "Text-only run"}
                                </span>
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

                    <aside className="flex min-h-[65vh] flex-col gap-3 border border-border bg-background/30 p-3">
                        <div className="flex items-center justify-between">
                            <div className="text-sm font-medium">Workspace Files</div>
                            <div className="flex items-center gap-1">
                                <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-7 w-7"
                                    onClick={() => void loadFiles()}
                                    disabled={filesLoading || runPending}
                                    aria-label="Refresh files"
                                >
                                    <RefreshCw className={`h-3.5 w-3.5 ${filesLoading ? "animate-spin" : ""}`} />
                                </Button>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    className="h-7 gap-1 text-[11px]"
                                    disabled={uploadPending || runPending}
                                    onClick={() => fileInputRef.current?.click()}
                                >
                                    {uploadPending ? (
                                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                    ) : (
                                        <FileUp className="h-3.5 w-3.5" />
                                    )}
                                    Upload
                                </Button>
                                <input
                                    ref={fileInputRef}
                                    type="file"
                                    className="hidden"
                                    onChange={handleUploadInputChange}
                                />
                            </div>
                        </div>

                        <div className="text-[11px] text-muted-foreground">
                            {filesRequestId ? `files_request_id=${filesRequestId}` : ""}
                            {uploadRequestId ? ` · upload_request_id=${uploadRequestId}` : ""}
                        </div>

                        {filesError && (
                            <p className="border border-destructive/30 bg-destructive/[0.08] p-2 text-xs text-destructive">
                                {filesError}
                            </p>
                        )}

                        {uploadError && (
                            <p className="border border-destructive/30 bg-destructive/[0.08] p-2 text-xs text-destructive">
                                {uploadError}
                            </p>
                        )}

                        <div className="max-h-52 space-y-2 overflow-y-auto pr-1">
                            {retainedFiles.map((file) => {
                                const checked = selectedFileIds.includes(file.id);
                                return (
                                    <label
                                        key={file.id}
                                        className="block cursor-pointer border border-border/80 bg-background/50 p-2 transition-colors hover:bg-secondary/25"
                                    >
                                        <div className="flex items-start gap-2">
                                            <input
                                                type="checkbox"
                                                className="mt-0.5"
                                                checked={checked}
                                                onChange={() => toggleFileSelection(file.id)}
                                                disabled={runPending}
                                            />
                                            <div className="min-w-0 flex-1">
                                                <p className="truncate text-xs font-medium">{file.filename}</p>
                                                <p className="mt-1 text-[10px] text-muted-foreground">
                                                    {file.id} · {formatBytes(file.bytes)}
                                                    {file.source ? ` · ${file.source}` : ""}
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => void handleDownloadFile(file)}
                                                className="inline-flex h-6 w-6 items-center justify-center border border-border/80 text-muted-foreground hover:text-foreground"
                                                title="Download file"
                                            >
                                                <FileDown className="h-3.5 w-3.5" />
                                            </button>
                                        </div>
                                    </label>
                                );
                            })}

                            {!filesLoading && retainedFiles.length === 0 && (
                                <div className="border border-border/60 bg-background/40 p-3 text-xs text-muted-foreground">
                                    No retained files yet for this tenant.
                                </div>
                            )}
                        </div>

                        <div className="border border-border/70 bg-background/50 p-2">
                            <div className="mb-2 flex items-center gap-2 text-xs font-medium">
                                <Paperclip className="h-3.5 w-3.5 text-muted-foreground" />
                                Attached to next run ({selectedFileIds.length})
                            </div>
                            {selectedFiles.length === 0 ? (
                                <p className="text-[11px] text-muted-foreground">
                                    Select retained files to attach.
                                </p>
                            ) : (
                                <div className="flex flex-wrap gap-1.5">
                                    {selectedFiles.map((file) => (
                                        <button
                                            key={file.id}
                                            type="button"
                                            className="inline-flex items-center gap-1 border border-primary/40 bg-primary/[0.08] px-2 py-1 text-[10px] text-primary"
                                            onClick={() => toggleFileSelection(file.id)}
                                        >
                                            {file.filename}
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>

                        <div className="border border-border/70 bg-background/50 p-2">
                            <div className="mb-2 flex items-center gap-2 text-xs font-medium">
                                <CheckCircle2 className="h-3.5 w-3.5 text-muted-foreground" />
                                Generated outputs ({generatedFiles.length})
                            </div>
                            {generatedFiles.length === 0 ? (
                                <p className="text-[11px] text-muted-foreground">
                                    Generated files from <code>run.completed.files</code> will appear here.
                                </p>
                            ) : (
                                <div className="max-h-36 space-y-1.5 overflow-y-auto pr-1">
                                    {generatedFiles.map((file) => (
                                        <div
                                            key={file.id}
                                            className="flex items-center justify-between gap-2 border border-border/70 px-2 py-1 text-[11px]"
                                        >
                                            <div className="min-w-0">
                                                <p className="truncate font-medium">{file.filename}</p>
                                                <p className="truncate text-[10px] text-muted-foreground">
                                                    {file.id} · {formatBytes(file.sizeBytes)}
                                                    {file.mimeType ? ` · ${file.mimeType}` : ""}
                                                </p>
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => void handleDownloadFile({
                                                    id: file.id,
                                                    filename: file.filename,
                                                })}
                                                className="inline-flex h-6 w-6 items-center justify-center border border-border/80 text-muted-foreground hover:text-foreground"
                                                title="Download generated file"
                                            >
                                                <FileDown className="h-3.5 w-3.5" />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>

                        <div className="min-h-0 flex-1 border border-border/70 bg-background/50 p-2">
                            <div className="mb-2 flex items-center gap-2 text-xs font-medium">
                                <Sparkles className="h-3.5 w-3.5 text-muted-foreground" />
                                Activity ({activityEntries.length})
                            </div>
                            {activityEntries.length === 0 ? (
                                <p className="text-[11px] text-muted-foreground">
                                    Stream/tool activity will appear here.
                                </p>
                            ) : (
                                <div className="max-h-56 space-y-1.5 overflow-y-auto pr-1">
                                    {activityEntries.map((entry) => {
                                        const icon =
                                            entry.status === "error" ? (
                                                <XCircle className="h-3.5 w-3.5 text-destructive" />
                                            ) : entry.status === "success" ? (
                                                <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                                            ) : (
                                                <Sparkles className="h-3.5 w-3.5 text-muted-foreground" />
                                            );

                                        return (
                                            <div
                                                key={entry.id}
                                                className="border border-border/70 bg-background/70 p-2 text-[11px]"
                                            >
                                                <div className="mb-1 flex items-center justify-between gap-2">
                                                    <span className="inline-flex items-center gap-1 font-medium">
                                                        {icon}
                                                        {entry.kind}
                                                    </span>
                                                    <span className="text-[10px] text-muted-foreground">
                                                        {formatClock(entry.timestamp)}
                                                    </span>
                                                </div>
                                                <p className="whitespace-pre-wrap leading-snug">{entry.message}</p>
                                                {entry.requestId && (
                                                    <p className="mt-1 truncate text-[10px] text-muted-foreground">
                                                        request_id={entry.requestId}
                                                    </p>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    </aside>
                </section>
            </div>

            <Toast toast={toast} />
        </>
    );
}
