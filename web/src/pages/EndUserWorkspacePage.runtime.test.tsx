import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import EndUserWorkspacePage from "./EndUserWorkspacePage";

const mockApi = vi.hoisted(() => ({
  getWorkbenchBootstrap: vi.fn(),
  getWorkbenchSessions: vi.fn(),
  getWorkbenchSessionMessages: vi.fn(),
  getWorkbenchFiles: vi.fn(),
  uploadWorkbenchFile: vi.fn(),
  createWorkbenchRun: vi.fn(),
  streamWorkbenchRunEvents: vi.fn(),
  downloadWorkbenchFile: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getWorkbenchBootstrap: (...args: unknown[]) =>
      mockApi.getWorkbenchBootstrap(...args),
    getWorkbenchSessions: (...args: unknown[]) => mockApi.getWorkbenchSessions(...args),
    getWorkbenchSessionMessages: (...args: unknown[]) =>
      mockApi.getWorkbenchSessionMessages(...args),
    getWorkbenchFiles: (...args: unknown[]) => mockApi.getWorkbenchFiles(...args),
    uploadWorkbenchFile: (...args: unknown[]) => mockApi.uploadWorkbenchFile(...args),
    createWorkbenchRun: (...args: unknown[]) => mockApi.createWorkbenchRun(...args),
    streamWorkbenchRunEvents: (...args: unknown[]) =>
      mockApi.streamWorkbenchRunEvents(...args),
  },
  buildWorkbenchRunPayload: ({
    prompt,
    sessionId,
    selectedFileIds = [],
  }: {
    prompt: string;
    sessionId?: string | null;
    selectedFileIds?: string[];
  }) => ({
    input:
      selectedFileIds.length === 0
        ? prompt
        : [
            {
              role: "user",
              content: [
                { type: "input_text", text: prompt },
                ...selectedFileIds.map((fileId) => ({
                  type: "input_file",
                  file_id: fileId,
                })),
              ],
            },
          ],
    ...(sessionId ? { session_id: sessionId } : {}),
  }),
  downloadWorkbenchFile: (...args: unknown[]) => mockApi.downloadWorkbenchFile(...args),
  getWorkbenchFileDownloadUrl: (fileId: string) =>
    `/api/workbench/files/${encodeURIComponent(fileId)}/content`,
}));

vi.mock("@/pages/SessionsPage", () => ({
  MessageList: ({
    messages,
  }: {
    messages: Array<{ role: string; content: string | null }>;
  }) => (
    <div data-testid="message-list">
      {messages.map((message, index) => (
        <p key={`${message.role}-${index}`}>{message.content}</p>
      ))}
    </div>
  ),
}));

vi.mock("@/components/Toast", () => ({
  Toast: () => null,
}));

const NOW = Math.floor(Date.now() / 1000);

function installLocalStorageShim() {
  const storage = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => (storage.has(key) ? storage.get(key)! : null),
      setItem: (key: string, value: string) => {
        storage.set(key, String(value));
      },
      removeItem: (key: string) => {
        storage.delete(key);
      },
      clear: () => {
        storage.clear();
      },
    },
  });
}

function bootstrapResult(tenantId = "tenant-alpha") {
  return {
    bootstrap: {
      tenant: {
        id: tenantId,
        label: "Tenant Alpha",
        source: "browser_cookie",
        fallback: false,
        fallback_reason: null,
        identity_hint: "hint",
      },
      workbench: {
        context_version: 1,
        tenant_id: tenantId,
        tenant_label: "Tenant Alpha",
        request_id: "req-bootstrap",
        ignored_browser_user_id: false,
        ignored_browser_user_id_sources: [],
      },
    },
    requestId: "req-bootstrap",
    tenantId,
    tenantLabel: "Tenant Alpha",
    tenantSource: "browser_cookie",
  };
}

function sessionsResult(tenantId = "tenant-alpha") {
  return {
    sessions: [
      {
        id: "sess-1",
        source: "cli",
        model: "anthropic/claude",
        title: "Quarterly Analysis",
        started_at: NOW - 600,
        ended_at: null,
        last_active: NOW - 30,
        is_active: true,
        message_count: 2,
        tool_call_count: 0,
        input_tokens: 10,
        output_tokens: 25,
        preview: "Summarize the uploaded report",
      },
    ],
    total: 1,
    limit: 50,
    offset: 0,
    requestId: "req-sessions",
    tenantId,
    tenantLabel: "Tenant Alpha",
    tenantSource: "browser_cookie",
  };
}

function sessionMessagesResult(tenantId = "tenant-alpha") {
  return {
    session_id: "sess-1",
    messages: [{ role: "assistant", content: "Initial response" }],
    requestId: "req-messages",
    tenantId,
    tenantLabel: "Tenant Alpha",
    tenantSource: "browser_cookie",
  };
}

function filesResult(tenantId = "tenant-alpha") {
  return {
    files: [
      {
        id: "file-1",
        object: "file",
        filename: "brief.md",
        bytes: 128,
        created_at: NOW,
        purpose: "uploads",
        mime_type: "text/markdown",
        source: "upload",
        download_url: "/api/workbench/files/file-1/content",
      },
    ],
    requestId: "req-files",
    tenantId,
    tenantLabel: "Tenant Alpha",
    tenantSource: "browser_cookie",
  };
}

function configureHappyPathMocks() {
  mockApi.getWorkbenchBootstrap.mockResolvedValue(bootstrapResult());
  mockApi.getWorkbenchSessions.mockResolvedValue(sessionsResult());
  mockApi.getWorkbenchSessionMessages.mockResolvedValue(sessionMessagesResult());
  mockApi.getWorkbenchFiles.mockResolvedValue(filesResult());
  mockApi.uploadWorkbenchFile.mockResolvedValue({
    file: {
      id: "file-2",
      object: "file",
      filename: "upload.txt",
      bytes: 256,
      created_at: NOW,
      purpose: "uploads",
      mime_type: "text/plain",
      source: "upload",
      download_url: "/api/workbench/files/file-2/content",
    },
    requestId: "req-upload",
    tenantId: "tenant-alpha",
    tenantLabel: "Tenant Alpha",
    tenantSource: "browser_cookie",
  });
  mockApi.createWorkbenchRun.mockResolvedValue({
    runId: "run-1",
    status: "started",
    sessionId: "sess-1",
    requestId: "req-run",
    tenantId: "tenant-alpha",
    tenantLabel: "Tenant Alpha",
    tenantSource: "browser_cookie",
  });
  mockApi.streamWorkbenchRunEvents.mockImplementation(
    async (
      _runId: string,
      onEvent: (event: Record<string, unknown>) => void,
    ) => {
      onEvent({ run_id: "run-1" });
      onEvent({ event: "message.delta", run_id: "run-1", delta: "Live stream" });
      onEvent({
        event: "run.completed",
        run_id: "run-1",
        output: "Live stream output",
        files: [
          {
            file_id: "gen-1",
            filename: "analysis.csv",
            size_bytes: 512,
            mime_type: "text/csv",
          },
        ],
      });
      return {
        requestId: "req-stream",
        sessionId: "sess-1",
        tenantId: "tenant-alpha",
        tenantLabel: "Tenant Alpha",
        tenantSource: "browser_cookie",
      };
    },
  );
}

beforeEach(() => {
  installLocalStorageShim();
  vi.clearAllMocks();
  window.localStorage.removeItem("hermes.workbench.lastSeenTenant.v1");
  configureHappyPathMocks();
});

describe("EndUserWorkspacePage runtime", () => {
  it("boots against real runtime signals, uploads files, and streams run outputs", async () => {
    render(<EndUserWorkspacePage />);

    await screen.findByText(/work with your files in a live ai run\./i);
    await screen.findByText("brief.md");

    const uploadInput = document.querySelector("input[type='file']") as HTMLInputElement;
    const uploadFile = new File(["notes"], "upload.txt", { type: "text/plain" });
    fireEvent.change(uploadInput, { target: { files: [uploadFile] } });

    await waitFor(() => {
      expect(mockApi.uploadWorkbenchFile).toHaveBeenCalledTimes(1);
    });
    const uploadEntries = await screen.findAllByText("upload.txt");
    expect(uploadEntries.length).toBeGreaterThan(0);

    const firstCheckbox = screen.getAllByRole("checkbox")[0];
    fireEvent.click(firstCheckbox);

    fireEvent.change(
      screen.getByPlaceholderText(/ask hermes to analyze or transform/i),
      { target: { value: "Summarize this file" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    await waitFor(() => {
      expect(mockApi.createWorkbenchRun).toHaveBeenCalledTimes(1);
    });

    const runPayload = mockApi.createWorkbenchRun.mock.calls[0][0] as {
      session_id?: string;
      input: unknown;
    };
    expect(runPayload.session_id).toBe("sess-1");
    expect(runPayload.input).toBeTruthy();

    await screen.findByText(/Run completed/i);
    await screen.findByText("analysis.csv");
    expect((await screen.findAllByText(/req-run/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText(/req-stream/)).length).toBeGreaterThan(0);
    await screen.findByText(/Malformed stream payload ignored/i);
  });

  it("prevents empty prompts even when files are selected", async () => {
    render(<EndUserWorkspacePage />);

    await screen.findByText(/work with your files in a live ai run\./i);
    await screen.findByText("brief.md");
    fireEvent.click(screen.getAllByRole("checkbox")[0]);

    const composer = screen.getByPlaceholderText(/ask hermes to analyze or transform/i);
    fireEvent.change(composer, { target: { value: "   " } });

    const runButton = screen.getByRole("button", { name: /^run$/i });
    expect((runButton as HTMLButtonElement).disabled).toBe(true);
    expect(mockApi.createWorkbenchRun).not.toHaveBeenCalled();
    expect(screen.getByText(/attached to next run \(1\)/i)).toBeTruthy();
  });

  it("shows tenant mismatch warning when stored tenant drifts", async () => {
    window.localStorage.setItem(
      "hermes.workbench.lastSeenTenant.v1",
      JSON.stringify({
        version: 1,
        tenantId: "tenant-old",
        tenantLabel: "Old Tenant",
        tenantSource: "browser_cookie",
        seenAt: Date.now() - 10_000,
      }),
    );

    render(<EndUserWorkspacePage />);

    await screen.findByText(/workspace safety lock active/i);
    expect(screen.getByText(/stored=tenant-old/i)).toBeTruthy();
    expect((screen.getByRole("button", { name: /^run$/i }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("rehydrates retained generated files after a file refresh", async () => {
    mockApi.getWorkbenchFiles
      .mockResolvedValueOnce(filesResult())
      .mockResolvedValueOnce({
        files: [
          ...filesResult().files,
          {
            id: "file-gen-retained",
            object: "file",
            filename: "retained-output.csv",
            bytes: 640,
            created_at: NOW,
            purpose: "assistant_output",
            mime_type: "text/csv",
            source: "assistant_output",
            download_url: "/api/workbench/files/file-gen-retained/content",
          },
        ],
        requestId: "req-files-refresh",
        tenantId: "tenant-alpha",
        tenantLabel: "Tenant Alpha",
        tenantSource: "browser_cookie",
      });

    render(<EndUserWorkspacePage />);

    await screen.findByText("brief.md");
    fireEvent.click(screen.getByRole("button", { name: /refresh files/i }));

    await waitFor(() => {
      expect(mockApi.getWorkbenchFiles).toHaveBeenCalledTimes(2);
    });

    await screen.findByText("retained-output.csv");
    expect(screen.getByText(/generated outputs/i)).toBeTruthy();
    expect(screen.getByText(/files_request_id=req-files-refresh/i)).toBeTruthy();
  });

  it("surfaces run-start proxy failures with request-id detail", async () => {
    mockApi.createWorkbenchRun.mockRejectedValue(
      new Error(
        'Run create failed {"detail":{"message":"upstream exploded","request_id":"req-run-5xx"}}',
      ),
    );

    render(<EndUserWorkspacePage />);

    await screen.findByText(/work with your files in a live ai run\./i);

    fireEvent.change(
      screen.getByPlaceholderText(/ask hermes to analyze or transform/i),
      { target: { value: "Trigger upstream failure" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    await screen.findByText(/runtime error:/i);
    expect((await screen.findAllByText(/upstream exploded \(request: req-run-5xx\)/i)).length)
      .toBeGreaterThan(0);
    expect(mockApi.streamWorkbenchRunEvents).not.toHaveBeenCalled();
  });

  it("surfaces stream failures with explicit runtime error state", async () => {
    mockApi.streamWorkbenchRunEvents.mockRejectedValue(
      new Error("SSE connection dropped"),
    );

    render(<EndUserWorkspacePage />);

    await screen.findByText(/work with your files in a live ai run\./i);

    fireEvent.change(
      screen.getByPlaceholderText(/ask hermes to analyze or transform/i),
      { target: { value: "Retry stream" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    await screen.findByText(/runtime error:/i);
    expect(screen.getAllByText(/SSE connection dropped/i).length).toBeGreaterThan(0);
    await waitFor(() => {
      expect(mockApi.createWorkbenchRun).toHaveBeenCalledTimes(1);
    });
  });
});
