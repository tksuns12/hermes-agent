const BASE = "";

// Ephemeral session token for protected endpoints.
// Injected into index.html by the server — never fetched via API.
declare global {
    interface Window {
        __HERMES_SESSION_TOKEN__?: string;
    }
}
let _sessionToken: string | null = null;

function withSessionAuthHeaders(init?: RequestInit): Headers {
    const headers = new Headers(init?.headers);
    const token = window.__HERMES_SESSION_TOKEN__;
    if (token && !headers.has("Authorization")) {
        headers.set("Authorization", `Bearer ${token}`);
    }
    return headers;
}

export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
    const headers = withSessionAuthHeaders(init);
    const res = await fetch(`${BASE}${url}`, { ...init, headers });
    if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        throw new Error(`${res.status}: ${text}`);
    }
    return res.json();
}

async function getSessionToken(): Promise<string> {
    if (_sessionToken) return _sessionToken;
    const injected = window.__HERMES_SESSION_TOKEN__;
    if (injected) {
        _sessionToken = injected;
        return _sessionToken;
    }
    throw new Error("Session token not available — page must be served by the Hermes dashboard server");
}

function safeJSONParse(raw: string): unknown {
    try {
        return JSON.parse(raw);
    } catch {
        return null;
    }
}

function parseWorkbenchSSEChunk(chunk: string): WorkbenchRunEvent | null {
    const lines = chunk
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    const dataLines = lines
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim());

    if (dataLines.length === 0) return null;
    const payload = dataLines.join("\n");
    const parsed = safeJSONParse(payload);
    if (!isRecord(parsed)) return null;

    const event = typeof parsed.event === "string" ? parsed.event.trim() : "";
    const runId = typeof parsed.run_id === "string" ? parsed.run_id.trim() : "";

    if (!event || !runId) {
        return null;
    }

    return parsed as unknown as WorkbenchRunEvent;
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return !!value && typeof value === "object" && !Array.isArray(value);
}

function parseBooleanHeader(value: string | null): boolean | undefined {
    if (value == null) return undefined;
    const normalized = value.trim().toLowerCase();
    if (normalized === "true") return true;
    if (normalized === "false") return false;
    return undefined;
}

function getWorkbenchProxyMeta(res: Response): WorkbenchProxyMeta {
    return {
        requestId: res.headers.get("X-Workbench-Request-Id") ?? undefined,
        sessionId: res.headers.get("X-Hermes-Session-Id") ?? undefined,
        tenantId: res.headers.get("X-Hermes-Tenant-Id") ?? undefined,
        tenantLabel: res.headers.get("X-Hermes-Tenant-Label") ?? undefined,
        tenantSource: res.headers.get("X-Hermes-Tenant-Source") ?? undefined,
        tenantFallback: parseBooleanHeader(res.headers.get("X-Hermes-Tenant-Fallback")),
        tenantFallbackReason:
            res.headers.get("X-Hermes-Tenant-Fallback-Reason") ?? undefined,
    };
}

function buildWorkbenchHttpError(
    res: Response,
    responseText: string,
    fallback: string,
): Error {
    const parsed = responseText ? safeJSONParse(responseText) : null;
    if (parsed) {
        return new Error(`${res.status}: ${responseText}`);
    }

    const requestId = res.headers.get("X-Workbench-Request-Id");
    const suffix = requestId ? ` (request: ${requestId})` : "";
    const detail = responseText || res.statusText || fallback;
    return new Error(`${res.status}: ${detail}${suffix}`);
}

function normalizeWorkbenchFileMetadata(
    payload: unknown,
    context: string,
): WorkbenchFileMetadata {
    if (!isRecord(payload)) {
        throw new Error(`Malformed ${context} file metadata: expected object`);
    }

    const id = typeof payload.id === "string" ? payload.id.trim() : "";
    const filename =
        typeof payload.filename === "string" ? payload.filename.trim() : "";

    if (!id) {
        throw new Error(`Malformed ${context} file metadata: missing id`);
    }
    if (!filename) {
        throw new Error(`Malformed ${context} file metadata: missing filename`);
    }

    const bytesRaw = payload.bytes;
    const createdAtRaw = payload.created_at;

    return {
        id,
        object:
            typeof payload.object === "string" && payload.object
                ? payload.object
                : "file",
        filename,
        bytes: typeof bytesRaw === "number" && Number.isFinite(bytesRaw)
            ? bytesRaw
            : undefined,
        created_at:
            typeof createdAtRaw === "number" && Number.isFinite(createdAtRaw)
                ? createdAtRaw
                : undefined,
        purpose:
            typeof payload.purpose === "string" ? payload.purpose : undefined,
        mime_type:
            typeof payload.mime_type === "string" ? payload.mime_type : undefined,
        source: typeof payload.source === "string" ? payload.source : undefined,
        source_run_id:
            typeof payload.source_run_id === "string" && payload.source_run_id.trim()
                ? payload.source_run_id.trim()
                : undefined,
        download_url:
            typeof payload.download_url === "string"
                ? payload.download_url
                : getWorkbenchFileDownloadUrl(id),
    };
}

function normalizeWorkbenchFileList(
    payload: unknown,
): WorkbenchFileMetadata[] {
    if (!isRecord(payload) || !Array.isArray(payload.data)) {
        throw new Error("Malformed workbench file list payload: missing data array");
    }

    return payload.data.map((item, index) =>
        normalizeWorkbenchFileMetadata(item, `workbench files[${index}]`),
    );
}

export function getWorkbenchFileDownloadUrl(fileId: string): string {
    return `/api/workbench/files/${encodeURIComponent(fileId)}/content`;
}

export async function downloadWorkbenchFile(id: string): Promise<
    { blob: Blob; contentType?: string } & WorkbenchProxyMeta
> {
    const headers = withSessionAuthHeaders();
    const res = await fetch(`${BASE}${getWorkbenchFileDownloadUrl(id)}`, {
        method: "GET",
        headers,
    });

    if (!res.ok) {
        const responseText = await res.text().catch(() => res.statusText);
        throw buildWorkbenchHttpError(res, responseText, "Failed to download workbench file");
    }

    return {
        blob: await res.blob(),
        contentType: res.headers.get("content-type") ?? undefined,
        ...getWorkbenchProxyMeta(res),
    };
}

export function buildWorkbenchRunPayload(params: {
    prompt: string;
    sessionId?: string | null;
    selectedFileIds?: string[];
}): WorkbenchRunCreatePayload {
    const prompt = params.prompt.trim();
    const selectedFileIds = (params.selectedFileIds ?? []).filter(
        (fileId): fileId is string =>
            typeof fileId === "string" && fileId.trim().length > 0,
    );

    const input: WorkbenchRunCreatePayload["input"] =
        selectedFileIds.length === 0
            ? prompt
            : [
                {
                    role: "user",
                    content: [
                        { type: "input_text", text: prompt },
                        ...selectedFileIds.map((fileId) => ({
                            type: "input_file" as const,
                            file_id: fileId,
                        })),
                    ],
                },
            ];

    return {
        input,
        ...(params.sessionId ? { session_id: params.sessionId } : {}),
    };
}

export const api = {
    getStatus: () => fetchJSON<StatusResponse>("/api/status"),
    getSessions: (limit = 20, offset = 0) =>
        fetchJSON<PaginatedSessions>(`/api/sessions?limit=${limit}&offset=${offset}`),
    getSessionMessages: (id: string) =>
        fetchJSON<SessionMessagesResponse>(`/api/sessions/${encodeURIComponent(id)}/messages`),
    deleteSession: (id: string) =>
        fetchJSON<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`, {
            method: "DELETE",
        }),
    getLogs: (params: { file?: string; lines?: number; level?: string; component?: string }) => {
        const qs = new URLSearchParams();
        if (params.file) qs.set("file", params.file);
        if (params.lines) qs.set("lines", String(params.lines));
        if (params.level && params.level !== "ALL") qs.set("level", params.level);
        if (params.component && params.component !== "all") qs.set("component", params.component);
        return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
    },
    getAnalytics: (days: number) =>
        fetchJSON<AnalyticsResponse>(`/api/analytics/usage?days=${days}`),
    getConfig: () => fetchJSON<Record<string, unknown>>("/api/config"),
    getDefaults: () => fetchJSON<Record<string, unknown>>("/api/config/defaults"),
    getSchema: () => fetchJSON<{ fields: Record<string, unknown>; category_order: string[] }>("/api/config/schema"),
    getModelInfo: () => fetchJSON<ModelInfoResponse>("/api/model/info"),
    saveConfig: (config: Record<string, unknown>) =>
        fetchJSON<{ ok: boolean }>("/api/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config }),
        }),
    getConfigRaw: () => fetchJSON<{ yaml: string }>("/api/config/raw"),
    saveConfigRaw: (yaml_text: string) =>
        fetchJSON<{ ok: boolean }>("/api/config/raw", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ yaml_text }),
        }),
    getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
    setEnvVar: (key: string, value: string) =>
        fetchJSON<{ ok: boolean }>("/api/env", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key, value }),
        }),
    deleteEnvVar: (key: string) =>
        fetchJSON<{ ok: boolean }>("/api/env", {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key }),
        }),
    revealEnvVar: async (key: string) => {
        const token = await getSessionToken();
        return fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ key }),
        });
    },

    // Cron jobs
    getCronJobs: () => fetchJSON<CronJob[]>("/api/cron/jobs"),
    createCronJob: (job: { prompt: string; schedule: string; name?: string; deliver?: string }) =>
        fetchJSON<CronJob>("/api/cron/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(job),
        }),
    pauseCronJob: (id: string) =>
        fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/pause`, { method: "POST" }),
    resumeCronJob: (id: string) =>
        fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/resume`, { method: "POST" }),
    triggerCronJob: (id: string) =>
        fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/trigger`, { method: "POST" }),
    deleteCronJob: (id: string) =>
        fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}`, { method: "DELETE" }),

    // Skills & Toolsets
    getSkills: () => fetchJSON<SkillInfo[]>("/api/skills"),
    toggleSkill: (name: string, enabled: boolean) =>
        fetchJSON<{ ok: boolean }>("/api/skills/toggle", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, enabled }),
        }),
    getToolsets: () => fetchJSON<ToolsetInfo[]>("/api/tools/toolsets"),

    // Session search (FTS5)
    searchSessions: (q: string) =>
        fetchJSON<SessionSearchResponse>(`/api/sessions/search?q=${encodeURIComponent(q)}`),

    // Tenant-aware workbench
    getWorkbenchBootstrap: async () => {
        const headers = withSessionAuthHeaders();
        const res = await fetch(`${BASE}/api/workbench/bootstrap`, {
            method: "GET",
            headers,
        });

        const responseText = await res.text();
        const parsed = responseText ? safeJSONParse(responseText) : null;

        if (!res.ok) {
            throw buildWorkbenchHttpError(res, responseText, "Failed to load workbench bootstrap");
        }

        if (!isRecord(parsed)) {
            throw new Error("Malformed workbench bootstrap payload: expected object");
        }

        const tenant = parsed.tenant;
        const workbench = parsed.workbench;

        if (!isRecord(tenant) || !isRecord(workbench)) {
            throw new Error("Malformed workbench bootstrap payload: missing tenant/workbench objects");
        }

        return {
            bootstrap: parsed as unknown as WorkbenchBootstrapResponse,
            ...getWorkbenchProxyMeta(res),
        } satisfies WorkbenchBootstrapResult;
    },
    getWorkbenchSessions: async (limit = 50, offset = 0) => {
        const headers = withSessionAuthHeaders();
        const res = await fetch(
            `${BASE}/api/workbench/sessions?limit=${limit}&offset=${offset}`,
            {
                method: "GET",
                headers,
            },
        );

        const responseText = await res.text();
        const parsed = responseText ? safeJSONParse(responseText) : null;

        if (!res.ok) {
            throw buildWorkbenchHttpError(res, responseText, "Failed to load workbench sessions");
        }

        if (!isRecord(parsed) || !Array.isArray(parsed.sessions)) {
            throw new Error("Malformed workbench sessions payload: missing sessions array");
        }

        return {
            ...(parsed as unknown as PaginatedSessions),
            ...getWorkbenchProxyMeta(res),
        } satisfies WorkbenchSessionsResult;
    },
    getWorkbenchSessionMessages: async (id: string) => {
        const headers = withSessionAuthHeaders();
        const res = await fetch(
            `${BASE}/api/workbench/sessions/${encodeURIComponent(id)}/messages`,
            {
                method: "GET",
                headers,
            },
        );

        const responseText = await res.text();
        const parsed = responseText ? safeJSONParse(responseText) : null;

        if (!res.ok) {
            throw buildWorkbenchHttpError(res, responseText, "Failed to load workbench messages");
        }

        if (!isRecord(parsed) || !Array.isArray(parsed.messages)) {
            throw new Error("Malformed workbench messages payload: missing messages array");
        }

        return {
            ...(parsed as unknown as SessionMessagesResponse),
            ...getWorkbenchProxyMeta(res),
        } satisfies WorkbenchSessionMessagesResult;
    },
    getWorkbenchFiles: async () => {
        const headers = withSessionAuthHeaders();
        const res = await fetch(`${BASE}/api/workbench/files`, {
            method: "GET",
            headers,
        });

        const responseText = await res.text();
        const parsed = responseText ? safeJSONParse(responseText) : null;

        if (!res.ok) {
            throw buildWorkbenchHttpError(res, responseText, "Failed to list workbench files");
        }

        return {
            files: normalizeWorkbenchFileList(parsed),
            ...getWorkbenchProxyMeta(res),
        } satisfies WorkbenchFileListResult;
    },
    uploadWorkbenchFile: async (
        blob: Blob,
        options: { filename: string; purpose?: string; source?: string },
    ) => {
        const filename = options.filename.trim();
        if (!filename) {
            throw new Error("Missing filename for workbench upload");
        }

        const qs = new URLSearchParams({
            filename,
            purpose: options.purpose?.trim() || "uploads",
            source: options.source?.trim() || "upload",
        });

        const headers = withSessionAuthHeaders({
            headers: {
                "Content-Type": blob.type || "application/octet-stream",
            },
        });

        const res = await fetch(`${BASE}/api/workbench/files?${qs.toString()}`, {
            method: "POST",
            headers,
            body: blob,
        });

        const responseText = await res.text();
        const parsed = responseText ? safeJSONParse(responseText) : null;

        if (!res.ok) {
            throw buildWorkbenchHttpError(res, responseText, "Failed to upload workbench file");
        }

        return {
            file: normalizeWorkbenchFileMetadata(parsed, "uploaded"),
            ...getWorkbenchProxyMeta(res),
        } satisfies WorkbenchFileMutationResult;
    },
    getWorkbenchFileMetadata: async (id: string) => {
        const headers = withSessionAuthHeaders();
        const res = await fetch(
            `${BASE}/api/workbench/files/${encodeURIComponent(id)}`,
            {
                method: "GET",
                headers,
            },
        );

        const responseText = await res.text();
        const parsed = responseText ? safeJSONParse(responseText) : null;

        if (!res.ok) {
            throw buildWorkbenchHttpError(res, responseText, "Failed to load workbench file metadata");
        }

        return {
            file: normalizeWorkbenchFileMetadata(parsed, "workbench metadata"),
            ...getWorkbenchProxyMeta(res),
        } satisfies WorkbenchFileMutationResult;
    },
    createWorkbenchRun: async (payload: WorkbenchRunCreatePayload) => {
        const headers = withSessionAuthHeaders({
            headers: { "Content-Type": "application/json" },
        });
        const res = await fetch(`${BASE}/api/workbench/runs`, {
            method: "POST",
            headers,
            body: JSON.stringify(payload),
        });

        const responseText = await res.text();
        const parsed = responseText ? safeJSONParse(responseText) : null;

        if (!res.ok) {
            throw buildWorkbenchHttpError(res, responseText, "Workbench run start failed");
        }

        if (!isRecord(parsed)) {
            throw new Error("Malformed run start response: expected object payload");
        }

        const runId =
            typeof parsed.run_id === "string" ? parsed.run_id.trim() : "";
        if (!runId) {
            throw new Error("Malformed run start response: missing run_id");
        }

        return {
            runId,
            status:
                typeof parsed.status === "string" && parsed.status
                    ? parsed.status
                    : "started",
            upstreamRunId: res.headers.get("X-Upstream-Run-Id") ?? undefined,
            ...getWorkbenchProxyMeta(res),
        } satisfies WorkbenchRunStartResult;
    },
    streamWorkbenchRunEvents: async (
        runId: string,
        onEvent: (event: WorkbenchRunEvent) => void,
        signal?: AbortSignal,
    ) => {
        const headers = withSessionAuthHeaders();
        const res = await fetch(
            `${BASE}/api/workbench/runs/${encodeURIComponent(runId)}/events`,
            {
                method: "GET",
                headers,
                signal,
            },
        );

        if (!res.ok) {
            const responseText = await res.text().catch(() => res.statusText);
            throw buildWorkbenchHttpError(
                res,
                responseText,
                "Workbench event stream failed",
            );
        }

        const reader = res.body?.getReader();
        if (!reader) {
            throw new Error("Workbench event stream did not include a readable body");
        }

        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            let separatorIndex = buffer.indexOf("\n\n");
            while (separatorIndex >= 0) {
                const chunk = buffer.slice(0, separatorIndex);
                buffer = buffer.slice(separatorIndex + 2);
                const event = parseWorkbenchSSEChunk(chunk);
                if (event) onEvent(event);
                separatorIndex = buffer.indexOf("\n\n");
            }
        }

        if (buffer.trim()) {
            const event = parseWorkbenchSSEChunk(buffer);
            if (event) onEvent(event);
        }

        return {
            ...getWorkbenchProxyMeta(res),
        };
    },

    // OAuth provider management
    getOAuthProviders: () =>
        fetchJSON<OAuthProvidersResponse>("/api/providers/oauth"),
    disconnectOAuthProvider: async (providerId: string) => {
        const token = await getSessionToken();
        return fetchJSON<{ ok: boolean; provider: string }>(
            `/api/providers/oauth/${encodeURIComponent(providerId)}`,
            {
                method: "DELETE",
                headers: { Authorization: `Bearer ${token}` },
            },
        );
    },
    startOAuthLogin: async (providerId: string) => {
        const token = await getSessionToken();
        return fetchJSON<OAuthStartResponse>(
            `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Authorization: `Bearer ${token}`,
                },
                body: "{}",
            },
        );
    },
    submitOAuthCode: async (providerId: string, sessionId: string, code: string) => {
        const token = await getSessionToken();
        return fetchJSON<OAuthSubmitResponse>(
            `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Authorization: `Bearer ${token}`,
                },
                body: JSON.stringify({ session_id: sessionId, code }),
            },
        );
    },
    pollOAuthSession: (providerId: string, sessionId: string) =>
        fetchJSON<OAuthPollResponse>(
            `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`,
        ),
    cancelOAuthSession: async (sessionId: string) => {
        const token = await getSessionToken();
        return fetchJSON<{ ok: boolean }>(
            `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
            {
                method: "DELETE",
                headers: { Authorization: `Bearer ${token}` },
            },
        );
    },

    // Dashboard plugins
    getPlugins: () =>
        fetchJSON<PluginManifestResponse[]>("/api/dashboard/plugins"),
    rescanPlugins: () =>
        fetchJSON<{ ok: boolean; count: number }>("/api/dashboard/plugins/rescan"),

    // Dashboard themes
    getThemes: () =>
        fetchJSON<DashboardThemesResponse>("/api/dashboard/themes"),
    setTheme: (name: string) =>
        fetchJSON<{ ok: boolean; theme: string }>("/api/dashboard/theme", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        }),
};

export interface PlatformStatus {
    error_code?: string;
    error_message?: string;
    state: string;
    updated_at: string;
}

export interface StatusResponse {
    active_sessions: number;
    config_path: string;
    config_version: number;
    env_path: string;
    gateway_exit_reason: string | null;
    gateway_health_url: string | null;
    gateway_pid: number | null;
    gateway_platforms: Record<string, PlatformStatus>;
    gateway_running: boolean;
    gateway_state: string | null;
    gateway_updated_at: string | null;
    hermes_home: string;
    latest_config_version: number;
    release_date: string;
    version: string;
}

export interface SessionInfo {
    id: string;
    source: string | null;
    model: string | null;
    title: string | null;
    started_at: number;
    ended_at: number | null;
    last_active: number;
    is_active: boolean;
    message_count: number;
    tool_call_count: number;
    input_tokens: number;
    output_tokens: number;
    preview: string | null;
}

export interface PaginatedSessions {
    sessions: SessionInfo[];
    total: number;
    limit: number;
    offset: number;
}

export interface EnvVarInfo {
    is_set: boolean;
    redacted_value: string | null;
    description: string;
    url: string | null;
    category: string;
    is_password: boolean;
    tools: string[];
    advanced: boolean;
}

export interface SessionMessage {
    role: "user" | "assistant" | "system" | "tool";
    content: string | null;
    tool_calls?: Array<{
        id: string;
        function: { name: string; arguments: string };
    }>;
    tool_name?: string;
    tool_call_id?: string;
    timestamp?: number;
}

export interface SessionMessagesResponse {
    session_id: string;
    messages: SessionMessage[];
}

export interface LogsResponse {
    file: string;
    lines: string[];
}

export interface AnalyticsDailyEntry {
    day: string;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    reasoning_tokens: number;
    estimated_cost: number;
    actual_cost: number;
    sessions: number;
}

export interface AnalyticsModelEntry {
    model: string;
    input_tokens: number;
    output_tokens: number;
    estimated_cost: number;
    sessions: number;
}

export interface AnalyticsSkillEntry {
    skill: string;
    view_count: number;
    manage_count: number;
    total_count: number;
    percentage: number;
    last_used_at: number | null;
}

export interface AnalyticsSkillsSummary {
    total_skill_loads: number;
    total_skill_edits: number;
    total_skill_actions: number;
    distinct_skills_used: number;
}

export interface AnalyticsResponse {
    daily: AnalyticsDailyEntry[];
    by_model: AnalyticsModelEntry[];
    totals: {
        total_input: number;
        total_output: number;
        total_cache_read: number;
        total_reasoning: number;
        total_estimated_cost: number;
        total_actual_cost: number;
        total_sessions: number;
    };
    skills: {
        summary: AnalyticsSkillsSummary;
        top_skills: AnalyticsSkillEntry[];
    };
}

export interface CronJob {
    id: string;
    name?: string;
    prompt: string;
    schedule: { kind: string; expr: string; display: string };
    schedule_display: string;
    enabled: boolean;
    state: string;
    deliver?: string;
    last_run_at?: string | null;
    next_run_at?: string | null;
    last_error?: string | null;
}

export interface SkillInfo {
    name: string;
    description: string;
    category: string;
    enabled: boolean;
}

export interface ToolsetInfo {
    name: string;
    label: string;
    description: string;
    enabled: boolean;
    configured: boolean;
    tools: string[];
}

export interface SessionSearchResult {
    session_id: string;
    snippet: string;
    role: string | null;
    source: string | null;
    model: string | null;
    session_started: number | null;
}

export interface SessionSearchResponse {
    results: SessionSearchResult[];
}

export interface WorkbenchBootstrapResponse {
    tenant: {
        id: string;
        label: string;
        source: string;
        fallback: boolean;
        fallback_reason: string | null;
        identity_hint: string;
    };
    workbench: {
        context_version: number;
        tenant_id: string;
        tenant_label: string;
        request_id: string;
        ignored_browser_user_id: boolean;
        ignored_browser_user_id_sources: string[];
    };
}

export interface WorkbenchProxyMeta {
    requestId?: string;
    sessionId?: string;
    tenantId?: string;
    tenantLabel?: string;
    tenantSource?: string;
    tenantFallback?: boolean;
    tenantFallbackReason?: string;
}

export interface WorkbenchBootstrapResult extends WorkbenchProxyMeta {
    bootstrap: WorkbenchBootstrapResponse;
}

export interface WorkbenchSessionsResult
    extends PaginatedSessions, WorkbenchProxyMeta {}

export interface WorkbenchSessionMessagesResult
    extends SessionMessagesResponse, WorkbenchProxyMeta {}

export interface WorkbenchFileMetadata {
    id: string;
    object: string;
    bytes?: number;
    created_at?: number;
    filename: string;
    purpose?: string;
    mime_type?: string;
    source?: string;
    source_run_id?: string;
    download_url: string;
}

export interface WorkbenchFileListResult extends WorkbenchProxyMeta {
    files: WorkbenchFileMetadata[];
}

export interface WorkbenchFileMutationResult extends WorkbenchProxyMeta {
    file: WorkbenchFileMetadata;
}

export type WorkbenchRunContentPart =
    | { type: "input_text"; text: string }
    | { type: "input_file"; file_id: string };

export interface WorkbenchRunInputMessage {
    role: string;
    content: string | WorkbenchRunContentPart[];
}

export interface WorkbenchRunCreatePayload {
    input: string | WorkbenchRunInputMessage[];
    session_id?: string;
    conversation_history?: Array<{
        role: string;
        content: string | WorkbenchRunContentPart[];
    }>;
    instructions?: string;
    previous_response_id?: string;
}

export interface WorkbenchRunCreateResponse {
    run_id: string;
    status: string;
}

export interface WorkbenchRunStartResult extends WorkbenchProxyMeta {
    runId: string;
    status: string;
    upstreamRunId?: string;
}

export interface WorkbenchRunOutputFile {
    type?: string;
    file_id: string;
    filename?: string;
    mime_type?: string;
    size_bytes?: number;
    source_run_id?: string;
    download_url?: string;
    file?: {
        id?: string;
        filename?: string;
        bytes?: number;
        mime_type?: string;
        purpose?: string;
        source?: string;
        source_run_id?: string;
    };
}

export interface WorkbenchRunEvent {
    event: string;
    run_id: string;
    timestamp?: number;
    delta?: string;
    output?: string;
    error?: string | boolean;
    tool?: string;
    preview?: string;
    duration?: number;
    files?: WorkbenchRunOutputFile[];
    usage?: {
        input_tokens?: number;
        output_tokens?: number;
        total_tokens?: number;
    };
    text?: string;
}

// ── Model info types ──────────────────────────────────────────────────

export interface ModelInfoResponse {
    model: string;
    provider: string;
    auto_context_length: number;
    config_context_length: number;
    effective_context_length: number;
    capabilities: {
        supports_tools?: boolean;
        supports_vision?: boolean;
        supports_reasoning?: boolean;
        context_window?: number;
        max_output_tokens?: number;
        model_family?: string;
    };
}

// ── OAuth provider types ────────────────────────────────────────────────

export interface OAuthProviderStatus {
    logged_in: boolean;
    source?: string | null;
    source_label?: string | null;
    token_preview?: string | null;
    expires_at?: string | null;
    has_refresh_token?: boolean;
    last_refresh?: string | null;
    error?: string;
}

export interface OAuthProvider {
    id: string;
    name: string;
    /** "pkce" (browser redirect + paste code), "device_code" (show code + URL),
     *  or "external" (delegated to a separate CLI like Claude Code or Qwen). */
    flow: "pkce" | "device_code" | "external";
    cli_command: string;
    docs_url: string;
    status: OAuthProviderStatus;
}

export interface OAuthProvidersResponse {
    providers: OAuthProvider[];
}

/** Discriminated union — the shape of /start depends on the flow. */
export type OAuthStartResponse =
    | {
        session_id: string;
        flow: "pkce";
        auth_url: string;
        expires_in: number;
    }
    | {
        session_id: string;
        flow: "device_code";
        user_code: string;
        verification_url: string;
        expires_in: number;
        poll_interval: number;
    };

export interface OAuthSubmitResponse {
    ok: boolean;
    status: "approved" | "error";
    message?: string;
}

export interface OAuthPollResponse {
    session_id: string;
    status: "pending" | "approved" | "denied" | "expired" | "error";
    error_message?: string | null;
    expires_at?: number | null;
}

// ── Dashboard theme types ──────────────────────────────────────────────

export interface DashboardThemeSummary {
    description: string;
    label: string;
    name: string;
}

export interface DashboardThemesResponse {
    active: string;
    themes: DashboardThemeSummary[];
}

// ── Dashboard plugin types ─────────────────────────────────────────────

export interface PluginManifestResponse {
    name: string;
    label: string;
    description: string;
    icon: string;
    version: string;
    tab: { path: string; position: string };
    entry: string;
    css?: string | null;
    has_api: boolean;
    source: string;
}
