import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  WorkbenchProxyMeta,
  WorkbenchRunEvent,
  WorkbenchRunOutputFile,
} from "@/lib/api";
import { useToast } from "@/hooks/useToast";

export type WorkspaceActivityKind =
  | "run"
  | "reasoning"
  | "tool"
  | "file"
  | "stream"
  | "tenant";
export type WorkspaceActivityStatus = "info" | "success" | "error";

export interface TenantMismatchWarning {
  kind: "bootstrap_mismatch" | "live_mismatch" | "missing_meta";
  flow:
    | "bootstrap"
    | "sessions"
    | "messages"
    | "files"
    | "upload"
    | "run_start"
    | "run_stream";
  message: string;
  expectedTenantId?: string;
  observedTenantId?: string;
  storedTenantId?: string;
  storedTenantLabel?: string;
  requestId?: string;
}

export interface WorkspaceActivityEntry {
  id: string;
  kind: WorkspaceActivityKind;
  status: WorkspaceActivityStatus;
  message: string;
  timestamp: number;
  requestId?: string;
}

export interface WorkspaceGeneratedFile {
  id: string;
  filename: string;
  sizeBytes?: number;
  mimeType?: string;
  downloadUrl: string;
}

interface LastSeenTenantRecord {
  version: number;
  tenantId: string;
  tenantLabel: string;
  tenantSource: string;
  seenAt: number;
  requestId?: string;
}

const LAST_SEEN_TENANT_STORAGE_KEY = "hermes.workbench.lastSeenTenant.v1";
const LAST_SEEN_TENANT_STORAGE_VERSION = 1;
const REQUEST_TIMEOUT_MS = 20_000;
const STREAM_TIMEOUT_MS = 120_000;

class RuntimeTimeoutError extends Error {
  phase: string;
  timeoutMs: number;

  constructor(phase: string, timeoutMs: number) {
    super(`${phase} timed out after ${Math.round(timeoutMs / 1000)}s`);
    this.phase = phase;
    this.timeoutMs = timeoutMs;
    this.name = "RuntimeTimeoutError";
  }
}

function withTimeout<T>(
  operation: Promise<T>,
  phase: string,
  timeoutMs: number,
  onTimeout?: () => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      onTimeout?.();
      reject(new RuntimeTimeoutError(phase, timeoutMs));
    }, timeoutMs);

    operation
      .then((value) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve(value);
      })
      .catch((error) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        reject(error);
      });
  });
}

export function formatApiError(error: unknown): string {
  if (error instanceof RuntimeTimeoutError) {
    return error.message;
  }

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

    const detail = parsed.detail?.message ?? parsed.error?.message ?? parsed.message;
    if (!detail) return message;

    const requestId = parsed.detail?.request_id;
    return requestId ? `${detail} (request: ${requestId})` : detail;
  } catch {
    return message;
  }
}

export function formatBytes(value?: number): string {
  if (typeof value !== "number" || Number.isNaN(value) || value < 0) {
    return "-";
  }
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
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
      filename: item.filename?.trim() || item.file?.filename?.trim() || "generated.bin",
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

function readLastSeenTenantRecord(): LastSeenTenantRecord | null {
  try {
    const raw = window.localStorage.getItem(LAST_SEEN_TENANT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return null;

    if (parsed.version !== LAST_SEEN_TENANT_STORAGE_VERSION) {
      return null;
    }

    const tenantId = typeof parsed.tenantId === "string" ? parsed.tenantId.trim() : "";
    const tenantLabel =
      typeof parsed.tenantLabel === "string" ? parsed.tenantLabel.trim() : "";
    const tenantSource =
      typeof parsed.tenantSource === "string" ? parsed.tenantSource.trim() : "";
    const seenAt =
      typeof parsed.seenAt === "number" && Number.isFinite(parsed.seenAt)
        ? parsed.seenAt
        : 0;

    if (!tenantId || !tenantLabel || !tenantSource || seenAt <= 0) {
      return null;
    }

    return {
      version: LAST_SEEN_TENANT_STORAGE_VERSION,
      tenantId,
      tenantLabel,
      tenantSource,
      seenAt,
      requestId:
        typeof parsed.requestId === "string" && parsed.requestId.trim()
          ? parsed.requestId.trim()
          : undefined,
    };
  } catch {
    return null;
  }
}

function persistLastSeenTenantRecord(
  bootstrap: WorkbenchBootstrapResponse,
  requestId?: string,
): void {
  try {
    const payload: LastSeenTenantRecord = {
      version: LAST_SEEN_TENANT_STORAGE_VERSION,
      tenantId: bootstrap.tenant.id,
      tenantLabel: bootstrap.tenant.label,
      tenantSource: bootstrap.tenant.source,
      seenAt: Date.now(),
      ...(requestId ? { requestId } : {}),
    };

    window.localStorage.setItem(LAST_SEEN_TENANT_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // localStorage write failure is non-fatal.
  }
}

function normalizeBootstrapResponse(
  payload: WorkbenchBootstrapResponse,
): WorkbenchBootstrapResponse {
  const tenantId = payload?.tenant?.id?.trim?.() ?? "";
  const tenantLabel = payload?.tenant?.label?.trim?.() ?? "";
  const tenantSource = payload?.tenant?.source?.trim?.() ?? "";
  const requestId = payload?.workbench?.request_id?.trim?.() ?? "";

  if (!tenantId || !tenantLabel || !tenantSource || !requestId) {
    throw new Error(
      "Malformed workbench bootstrap payload: missing tenant identity or request id",
    );
  }

  return payload;
}

function isTenantMismatchFlow(
  flow: string,
): flow is TenantMismatchWarning["flow"] {
  return (
    flow === "bootstrap" ||
    flow === "sessions" ||
    flow === "messages" ||
    flow === "files" ||
    flow === "upload" ||
    flow === "run_start" ||
    flow === "run_stream"
  );
}

export function useDocumentWorkspaceRuntime() {
  const [bootstrap, setBootstrap] = useState<WorkbenchBootstrapResponse | null>(null);
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
  const [tenantMismatchWarning, setTenantMismatchWarning] =
    useState<TenantMismatchWarning | null>(null);

  const [generatedFiles, setGeneratedFiles] = useState<WorkspaceGeneratedFile[]>([]);
  const [activityEntries, setActivityEntries] = useState<WorkspaceActivityEntry[]>([]);

  const activityIdRef = useRef(0);
  const streamAbortRef = useRef<AbortController | null>(null);
  const malformedStreamWarningShownRef = useRef(false);
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

  const mismatchActive = tenantMismatchWarning !== null;

  const appendActivity = useCallback(
    (
      kind: WorkspaceActivityKind,
      status: WorkspaceActivityStatus,
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

  const clearTenantScopedState = useCallback(() => {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;

    setSessions([]);
    setSessionsError(null);
    setSelectedSessionId(null);

    setMessages([]);
    setMessagesError(null);

    setRetainedFiles([]);
    setFilesError(null);
    setFilesRequestId(null);
    setSelectedFileIds([]);

    setGeneratedFiles([]);
    setActivityEntries([]);

    setSessionsLoading(false);
    setMessagesLoading(false);
    setFilesLoading(false);

    setRunPending(false);
    setRunRequestId(null);
    setStreamRequestId(null);
    setUploadPending(false);
    setUploadRequestId(null);
    setUploadError(null);
  }, []);

  const triggerTenantMismatch = useCallback(
    (warning: TenantMismatchWarning) => {
      clearTenantScopedState();
      setTenantMismatchWarning(warning);
      setRunError(warning.message);
      appendActivity("tenant", "error", warning.message, warning.requestId);
      showToast(warning.message, "error");
    },
    [appendActivity, clearTenantScopedState, showToast],
  );

  const ensureTenantMetaAligned = useCallback(
    (
      flow: TenantMismatchWarning["flow"],
      meta: WorkbenchProxyMeta,
    ): boolean => {
      if (!bootstrap) return true;

      const expectedTenant = bootstrap.tenant;
      if (!meta.tenantId || !meta.tenantLabel || !meta.tenantSource) {
        triggerTenantMismatch({
          kind: "missing_meta",
          flow,
          message:
            `Unsafe workspace response for ${flow}: tenant metadata headers were missing, so tenant-scoped state was cleared.`,
          expectedTenantId: expectedTenant.id,
          requestId: meta.requestId,
        });
        return false;
      }

      if (meta.tenantId !== expectedTenant.id) {
        triggerTenantMismatch({
          kind: "live_mismatch",
          flow,
          message:
            `Tenant drift detected on ${flow}: expected ${expectedTenant.id} but response resolved ${meta.tenantId}. Tenant-scoped state was cleared.`,
          expectedTenantId: expectedTenant.id,
          observedTenantId: meta.tenantId,
          requestId: meta.requestId,
        });
        return false;
      }

      return true;
    },
    [bootstrap, triggerTenantMismatch],
  );

  const loadBootstrap = useCallback(async () => {
    setBootstrapLoading(true);
    setBootstrapError(null);

    try {
      const payload = await withTimeout(
        api.getWorkbenchBootstrap(),
        "bootstrap",
        REQUEST_TIMEOUT_MS,
      );
      const nextBootstrap = normalizeBootstrapResponse(payload.bootstrap);
      setBootstrap(nextBootstrap);

      let warning: TenantMismatchWarning | null = null;

      if (!payload.tenantId || !payload.tenantLabel || !payload.tenantSource) {
        warning = {
          kind: "missing_meta",
          flow: "bootstrap",
          message:
            "Unsafe bootstrap response: tenant metadata headers were missing. Tenant-scoped state was cleared.",
          requestId: payload.requestId ?? nextBootstrap.workbench.request_id,
          expectedTenantId: nextBootstrap.tenant.id,
        };
      } else if (payload.tenantId !== nextBootstrap.tenant.id) {
        warning = {
          kind: "live_mismatch",
          flow: "bootstrap",
          message:
            `Bootstrap tenant mismatch: payload tenant ${nextBootstrap.tenant.id} disagreed with response header tenant ${payload.tenantId}. Tenant-scoped state was cleared.`,
          expectedTenantId: nextBootstrap.tenant.id,
          observedTenantId: payload.tenantId,
          requestId: payload.requestId ?? nextBootstrap.workbench.request_id,
        };
      } else {
        const stored = readLastSeenTenantRecord();
        if (stored && stored.tenantId !== nextBootstrap.tenant.id) {
          warning = {
            kind: "bootstrap_mismatch",
            flow: "bootstrap",
            message:
              `Stored tenant ${stored.tenantId} no longer matches resolved tenant ${nextBootstrap.tenant.id}. Tenant-scoped state was cleared until explicit reload.`,
            expectedTenantId: nextBootstrap.tenant.id,
            observedTenantId: nextBootstrap.tenant.id,
            storedTenantId: stored.tenantId,
            storedTenantLabel: stored.tenantLabel,
            requestId: payload.requestId ?? nextBootstrap.workbench.request_id,
          };
        }
      }

      if (!warning || warning.kind === "bootstrap_mismatch") {
        persistLastSeenTenantRecord(
          nextBootstrap,
          payload.requestId ?? nextBootstrap.workbench.request_id,
        );
      }

      if (warning) {
        triggerTenantMismatch(warning);
      } else {
        setTenantMismatchWarning(null);
      }
    } catch (error) {
      const detail = formatApiError(error);
      setBootstrapError(detail);
      appendActivity("stream", "error", `Bootstrap failed: ${detail}`);
    } finally {
      setBootstrapLoading(false);
    }
  }, [appendActivity, triggerTenantMismatch]);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    setSessionsError(null);

    try {
      const payload = await withTimeout(
        api.getWorkbenchSessions(50, 0),
        "sessions",
        REQUEST_TIMEOUT_MS,
      );
      if (!ensureTenantMetaAligned("sessions", payload)) return;

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
  }, [appendActivity, ensureTenantMetaAligned]);

  const loadMessages = useCallback(
    async (sessionId: string) => {
      setMessagesLoading(true);
      setMessagesError(null);

      try {
        const payload = await withTimeout(
          api.getWorkbenchSessionMessages(sessionId),
          "messages",
          REQUEST_TIMEOUT_MS,
        );
        if (!ensureTenantMetaAligned("messages", payload)) return;

        setMessages(payload.messages);
      } catch (error) {
        const detail = formatApiError(error);
        setMessagesError(detail);
        setMessages([]);
        appendActivity("stream", "error", `Message reload failed: ${detail}`);
      } finally {
        setMessagesLoading(false);
      }
    },
    [appendActivity, ensureTenantMetaAligned],
  );

  const loadFiles = useCallback(async () => {
    setFilesLoading(true);
    setFilesError(null);

    try {
      const payload = await withTimeout(
        api.getWorkbenchFiles(),
        "files",
        REQUEST_TIMEOUT_MS,
      );
      if (!ensureTenantMetaAligned("files", payload)) return;

      setFilesRequestId(payload.requestId ?? null);
      setRetainedFiles(payload.files);
      setSelectedFileIds((current) =>
        current.filter((fileId) => payload.files.some((file) => file.id === fileId)),
      );
    } catch (error) {
      const detail = formatApiError(error);
      setFilesError(detail);
      appendActivity("file", "error", `File list failed: ${detail}`);
      showToast(`File list failed: ${detail}`, "error");
    } finally {
      setFilesLoading(false);
    }
  }, [appendActivity, ensureTenantMetaAligned, showToast]);

  useEffect(() => {
    void loadBootstrap();
  }, [loadBootstrap]);

  useEffect(() => {
    if (!bootstrap || mismatchActive) return;
    void loadSessions();
    void loadFiles();
  }, [bootstrap, loadFiles, loadSessions, mismatchActive]);

  useEffect(() => {
    if (!selectedSessionId || mismatchActive) {
      setMessages([]);
      setMessagesError(null);
      return;
    }

    void loadMessages(selectedSessionId);
  }, [loadMessages, mismatchActive, selectedSessionId]);

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

  useEffect(() => {
    return () => {
      streamAbortRef.current?.abort();
    };
  }, []);

  const toggleFileSelection = useCallback(
    (fileId: string) => {
      if (mismatchActive) return;

      setSelectedFileIds((current) => {
        if (current.includes(fileId)) {
          return current.filter((existing) => existing !== fileId);
        }
        return [...current, fileId];
      });
    },
    [mismatchActive],
  );

  const uploadFile = useCallback(
    async (file: File) => {
      if (!file || runPending || mismatchActive) return;

      setUploadPending(true);
      setUploadError(null);

      try {
        const uploaded = await withTimeout(
          api.uploadWorkbenchFile(file, {
            filename: file.name,
            purpose: "uploads",
            source: "upload",
          }),
          "upload",
          REQUEST_TIMEOUT_MS,
        );

        if (!ensureTenantMetaAligned("upload", uploaded)) return;

        setUploadRequestId(uploaded.requestId ?? null);
        setRetainedFiles((current) => {
          const deduped = current.filter((existing) => existing.id !== uploaded.file.id);
          return [uploaded.file, ...deduped];
        });
        setSelectedFileIds((current) =>
          current.includes(uploaded.file.id) ? current : [...current, uploaded.file.id],
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
    },
    [
      appendActivity,
      ensureTenantMetaAligned,
      mismatchActive,
      runPending,
      showToast,
    ],
  );

  const handleDownloadFile = useCallback(
    async (file: { id: string; filename: string }) => {
      if (mismatchActive) return;

      try {
        const downloaded = await withTimeout(
          downloadWorkbenchFile(file.id),
          "files",
          REQUEST_TIMEOUT_MS,
        );
        if (!ensureTenantMetaAligned("files", downloaded)) return;

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
    },
    [appendActivity, ensureTenantMetaAligned, mismatchActive, showToast],
  );

  const submitPrompt = useCallback(
    async (
      rawPrompt: string,
      options?: {
        clearComposer?: boolean;
      },
    ) => {
      const prompt = rawPrompt.trim();
      if (!prompt) {
        setRunError("Prompt cannot be empty.");
        showToast("Prompt cannot be empty.", "error");
        return false;
      }

      if (runPending || bootstrapLoading || !bootstrap || mismatchActive) return false;

      if (options?.clearComposer) {
        setComposer("");
      }
      setRunPending(true);
      setRunError(null);
      setRunRequestId(null);
      setStreamRequestId(null);
      malformedStreamWarningShownRef.current = false;

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

        const runStart = await withTimeout(
          api.createWorkbenchRun(payload),
          "run_start",
          REQUEST_TIMEOUT_MS,
        );
        setRunRequestId(runStart.requestId ?? null);

        if (!ensureTenantMetaAligned("run_start", runStart)) {
          return false;
        }

        appendActivity(
          "run",
          "info",
          `Upstream run started: ${runStart.runId}`,
          runStart.requestId,
        );

        if (runStart.sessionId && runStart.sessionId !== selectedSessionId) {
          setSelectedSessionId(runStart.sessionId);
        }

        const streamAbortController = new AbortController();
        streamAbortRef.current = streamAbortController;

        const streamMeta = await withTimeout(
          api.streamWorkbenchRunEvents(
            runStart.runId,
            (evt: WorkbenchRunEvent) => {
              if (!evt || typeof evt !== "object" || typeof evt.event !== "string") {
                if (!malformedStreamWarningShownRef.current) {
                  malformedStreamWarningShownRef.current = true;
                  appendActivity(
                    "stream",
                    "error",
                    "Malformed stream payload ignored; workspace state was not mutated.",
                  );
                }
                return;
              }

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
                  appendActivity("tool", "info", `Tool started: ${evt.tool ?? "unknown"}`);
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
                  if (!isTenantMismatchFlow(evt.event)) {
                    // Unknown stream events are ignored to keep parser + UI resilient.
                  }
                  break;
              }
            },
            streamAbortController.signal,
          ),
          "run_stream",
          STREAM_TIMEOUT_MS,
          () => streamAbortController.abort(),
        );

        streamAbortRef.current = null;
        setStreamRequestId(streamMeta.requestId ?? null);

        if (!ensureTenantMetaAligned("run_stream", streamMeta)) {
          return false;
        }

        const nextSessionId = streamMeta.sessionId ?? runStart.sessionId ?? baseSessionId ?? null;

        await Promise.all([loadSessions(), loadFiles()]);
        if (nextSessionId) {
          setSelectedSessionId(nextSessionId);
          await loadMessages(nextSessionId);
        }

        return true;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          appendActivity("stream", "info", "Run stream aborted due to tenant safety guard.");
          return false;
        }

        const detail = formatApiError(error);
        const withPhase =
          error instanceof RuntimeTimeoutError
            ? `Run ${error.phase.replace("_", " ")} timed out after ${Math.round(error.timeoutMs / 1000)}s.`
            : detail;

        setRunError(withPhase);
        appendActivity("run", "error", `Run error: ${withPhase}`);

        setMessages((prev) => {
          const next = [...prev];
          for (let i = next.length - 1; i >= 0; i--) {
            if (next[i].role === "assistant" && !next[i].content?.trim()) {
              next[i] = {
                ...next[i],
                role: "system",
                content: `⚠️ ${withPhase}`,
              };
              return next;
            }
          }
          return [...next, { role: "system", content: `⚠️ ${withPhase}` }];
        });

        showToast(`Run failed: ${withPhase}`, "error");
        return false;
      } finally {
        streamAbortRef.current = null;
        setRunPending(false);
      }
    },
    [
      appendActivity,
      bootstrap,
      bootstrapLoading,
      ensureTenantMetaAligned,
      loadFiles,
      loadMessages,
      loadSessions,
      mismatchActive,
      runPending,
      selectedFileIds,
      selectedSessionId,
      showToast,
    ],
  );

  const submitComposer = useCallback(async () => {
    await submitPrompt(composer, { clearComposer: true });
  }, [composer, submitPrompt]);

  const handleMismatchRecovery = useCallback(async () => {
    clearTenantScopedState();
    setRunError(null);
    setTenantMismatchWarning(null);
    await loadBootstrap();
  }, [clearTenantScopedState, loadBootstrap]);

  const refreshWorkspace = useCallback(async () => {
    await Promise.all([loadSessions(), loadFiles()]);
    if (selectedSessionId) {
      await loadMessages(selectedSessionId);
    }
  }, [loadFiles, loadMessages, loadSessions, selectedSessionId]);

  return {
    toast,

    bootstrap,
    bootstrapLoading,
    bootstrapError,
    loadBootstrap,

    sessions,
    sessionsLoading,
    sessionsError,
    selectedSession,
    selectedSessionId,
    setSelectedSessionId,
    loadSessions,

    messages,
    messagesLoading,
    messagesError,
    loadMessages,

    retainedFiles,
    selectedFiles,
    selectedFileIds,
    filesLoading,
    filesError,
    filesRequestId,
    uploadPending,
    uploadError,
    uploadRequestId,
    loadFiles,
    uploadFile,
    toggleFileSelection,
    handleDownloadFile,

    generatedFiles,

    composer,
    setComposer,
    runPending,
    runError,
    runRequestId,
    streamRequestId,
    submitPrompt,
    submitComposer,

    mismatchActive,
    tenantMismatchWarning,
    handleMismatchRecovery,

    activityEntries,
    refreshWorkspace,
  };
}
