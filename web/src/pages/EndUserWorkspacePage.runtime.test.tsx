import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

function outcomeEnvelope(
  status: "success" | "partial_success" | "unsupported" | "no_output",
  explanation: string,
  nextSteps: string[] = [],
): string {
  return `<hermes_office_outcome>${JSON.stringify({
    status,
    explanation,
    next_steps: nextSteps,
  })}</hermes_office_outcome>`;
}

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
      {
        id: "file-docx",
        object: "file",
        filename: "meeting-notes.docx",
        bytes: 2624,
        created_at: NOW,
        purpose: "uploads",
        mime_type:
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source: "upload",
        download_url: "/api/workbench/files/file-docx/content",
      },
      {
        id: "file-xlsx-1",
        object: "file",
        filename: "finance-q1.xlsx",
        bytes: 3610,
        created_at: NOW,
        purpose: "uploads",
        mime_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        source: "upload",
        download_url: "/api/workbench/files/file-xlsx-1/content",
      },
      {
        id: "file-xlsx-2",
        object: "file",
        filename: "finance-q2.xlsx",
        bytes: 3720,
        created_at: NOW,
        purpose: "uploads",
        mime_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        source: "upload",
        download_url: "/api/workbench/files/file-xlsx-2/content",
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
            source_run_id: "run-1",
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
  mockApi.downloadWorkbenchFile.mockResolvedValue({
    blob: new Blob(["download"], { type: "text/csv" }),
    contentType: "text/csv",
    requestId: "req-download",
    tenantId: "tenant-alpha",
    tenantLabel: "Tenant Alpha",
    tenantSource: "browser_cookie",
  });
}

function toggleFileSelectionByName(filename: string) {
  const fileLabel = screen.getByText(filename).closest("label");
  expect(fileLabel).toBeTruthy();
  const checkbox = within(fileLabel as HTMLElement).getByRole("checkbox");
  fireEvent.click(checkbox);
}

function latestRunPayload() {
  const lastCall = mockApi.createWorkbenchRun.mock.calls.at(-1);
  expect(lastCall).toBeTruthy();
  return lastCall?.[0] as {
    session_id?: string;
    input: unknown;
  };
}

beforeEach(() => {
  installLocalStorageShim();
  Object.defineProperty(window.URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(() => "blob:mock-download"),
  });
  Object.defineProperty(window.URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(),
  });
  Object.defineProperty(HTMLAnchorElement.prototype, "click", {
    configurable: true,
    writable: true,
    value: vi.fn(),
  });

  vi.clearAllMocks();
  window.localStorage.removeItem("hermes.workbench.lastSeenTenant.v1");
  configureHappyPathMocks();
});

describe("EndUserWorkspacePage runtime", () => {
  it("boots against real runtime signals, renders run-tied output cards, and downloads generated files", async () => {
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

    toggleFileSelectionByName("brief.md");

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
    await screen.findByText(/source_run_id=run-1/i);
    expect((await screen.findAllByText(/req-run/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText(/req-stream/)).length).toBeGreaterThan(0);
    await screen.findByText(/Malformed stream payload ignored/i);

    fireEvent.click(
      screen.getByRole("button", { name: /download generated file analysis\.csv/i }),
    );

    await waitFor(() => {
      expect(mockApi.downloadWorkbenchFile).toHaveBeenCalledWith("gen-1");
    });
    await screen.findByText(/Downloaded analysis\.csv/i);
  });

  it("prevents empty prompts even when files are selected", async () => {
    render(<EndUserWorkspacePage />);

    await screen.findByText(/work with your files in a live ai run\./i);
    await screen.findByText("brief.md");
    toggleFileSelectionByName("brief.md");

    const composer = screen.getByPlaceholderText(/ask hermes to analyze or transform/i);
    fireEvent.change(composer, { target: { value: "   " } });

    const runButton = screen.getByRole("button", { name: /^run$/i });
    expect((runButton as HTMLButtonElement).disabled).toBe(true);
    expect(mockApi.createWorkbenchRun).not.toHaveBeenCalled();
    expect(screen.getByText(/attached to next run \(1\)/i)).toBeTruthy();
  });

  it("submits guided DOCX runs with selected-file context and keeps freeform fallback visible", async () => {
    render(<EndUserWorkspacePage />);

    await screen.findByText("meeting-notes.docx");
    expect(screen.getByText(/freeform fallback/i)).toBeTruthy();

    toggleFileSelectionByName("meeting-notes.docx");

    fireEvent.change(screen.getByLabelText(/guided task/i), {
      target: { value: "docx-summary" },
    });
    fireEvent.change(screen.getByLabelText(/summary focus/i), {
      target: { value: "Emphasize executive decisions" },
    });

    expect(screen.getByText(/Selected files for this task \(1\)/i)).toBeTruthy();
    expect(screen.getAllByText("meeting-notes.docx").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /run guided task/i }));

    await waitFor(() => {
      expect(mockApi.createWorkbenchRun).toHaveBeenCalledTimes(1);
    });

    const runPayload = latestRunPayload();
    expect(runPayload.session_id).toBe("sess-1");

    const turn = (runPayload.input as Array<{
      content: Array<{ type: string; text?: string; file_id?: string }>;
    }>)[0];
    const promptPart = turn.content.find((part) => part.type === "input_text");

    expect(promptPart?.text).toContain('Run the guided task "Summarize DOCX"');
    expect(promptPart?.text).toContain("meeting-notes.docx");
    expect(turn.content).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ type: "input_file", file_id: "file-docx" }),
      ]),
    );

    await screen.findByText(/Run completed/i);
  });

  it("keeps freeform composer available when guided local validation blocks submit", async () => {
    render(<EndUserWorkspacePage />);

    await screen.findByText(/work with your files in a live ai run\./i);
    expect(screen.getByText(/select at least 1 \.docx file/i)).toBeTruthy();

    const guidedRunButton = screen.getByRole("button", { name: /run guided task/i });
    expect((guidedRunButton as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(
      screen.getByPlaceholderText(/ask hermes to analyze or transform/i),
      { target: { value: "Fallback freeform run" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    await waitFor(() => {
      expect(mockApi.createWorkbenchRun).toHaveBeenCalledTimes(1);
    });

    const runPayload = latestRunPayload();
    expect(typeof runPayload.input).toBe("string");
  });

  it("recovers guided eligibility after task/file switches and submits XLSX runs", async () => {
    render(<EndUserWorkspacePage />);

    await screen.findByText("finance-q1.xlsx");

    toggleFileSelectionByName("finance-q1.xlsx");
    expect(screen.getByText(/only accepts \.docx files/i)).toBeTruthy();

    fireEvent.change(screen.getByLabelText(/guided task/i), {
      target: { value: "xlsx-summary" },
    });

    await waitFor(() => {
      expect(screen.getByText(/selection is eligible for this guided task/i)).toBeTruthy();
    });

    const guidedRunButton = screen.getByRole("button", { name: /run guided task/i });
    expect((guidedRunButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(guidedRunButton);

    await waitFor(() => {
      expect(mockApi.createWorkbenchRun).toHaveBeenCalledTimes(1);
    });

    const runPayload = latestRunPayload();
    const turn = (runPayload.input as Array<{
      content: Array<{ type: string; text?: string; file_id?: string }>;
    }>)[0];
    const promptPart = turn.content.find((part) => part.type === "input_text");

    expect(promptPart?.text).toContain('Run the guided task "Summarize XLSX"');
    expect(promptPart?.text).toContain("finance-q1.xlsx");
    expect(turn.content).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ type: "input_file", file_id: "file-xlsx-1" }),
      ]),
    );
    expect(screen.getByPlaceholderText(/ask hermes to analyze or transform/i)).toBeTruthy();
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
    expect(screen.getByText(/source_run_id=unknown/i)).toBeTruthy();
    expect(screen.getByText(/files_request_id=req-files-refresh/i)).toBeTruthy();
  });

  it("shows an honest partial-success boundary panel alongside generated outputs", async () => {
    const partialOutcome = outcomeEnvelope(
      "partial_success",
      "Generated output with formatting caveats.",
      ["Review table formatting before sharing."],
    );

    mockApi.getWorkbenchSessionMessages.mockResolvedValue({
      ...sessionMessagesResult(),
      messages: [{ role: "assistant", content: `Here is your draft.\n${partialOutcome}` }],
    });

    mockApi.streamWorkbenchRunEvents.mockImplementation(
      async (
        _runId: string,
        onEvent: (event: Record<string, unknown>) => void,
      ) => {
        onEvent({ event: "run.completed", run_id: "run-1", output: partialOutcome, files: [
          {
            file_id: "gen-partial",
            filename: "draft.docx",
            size_bytes: 1024,
            mime_type:
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            source_run_id: "run-1",
          },
        ] });
        return {
          requestId: "req-stream-partial",
          sessionId: "sess-1",
          tenantId: "tenant-alpha",
          tenantLabel: "Tenant Alpha",
          tenantSource: "browser_cookie",
        };
      },
    );

    render(<EndUserWorkspacePage />);
    await screen.findByText(/work with your files in a live ai run\./i);

    fireEvent.change(
      screen.getByPlaceholderText(/ask hermes to analyze or transform/i),
      { target: { value: "Run partial" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    const partialBoundary = await screen.findByTestId("workspace-run-boundary");
    expect(within(partialBoundary).getByText(/Partial output boundary/i)).toBeTruthy();
    expect(within(partialBoundary).getByText(/status=partial_success/i)).toBeTruthy();
    expect(within(partialBoundary).getByText(/source_run_id=run-1/i)).toBeTruthy();
    expect(
      within(partialBoundary).getByText(/Generated output with formatting caveats\./i),
    ).toBeTruthy();
    await screen.findByText("draft.docx");
  });

  it("shows no-output boundary state without fake generated-file download affordances", async () => {
    const noOutputEnvelope = outcomeEnvelope(
      "no_output",
      "No output was intentionally generated for this request.",
      ["Ask a follow-up with a narrower output format."],
    );

    mockApi.getWorkbenchSessionMessages.mockResolvedValue({
      ...sessionMessagesResult(),
      messages: [{ role: "assistant", content: noOutputEnvelope }],
    });

    mockApi.streamWorkbenchRunEvents.mockImplementation(
      async (
        _runId: string,
        onEvent: (event: Record<string, unknown>) => void,
      ) => {
        onEvent({
          event: "run.completed",
          run_id: "run-1",
          output: noOutputEnvelope,
          files: [],
        });
        return {
          requestId: "req-stream-no-output",
          sessionId: "sess-1",
          tenantId: "tenant-alpha",
          tenantLabel: "Tenant Alpha",
          tenantSource: "browser_cookie",
        };
      },
    );

    render(<EndUserWorkspacePage />);
    await screen.findByText(/work with your files in a live ai run\./i);

    fireEvent.change(
      screen.getByPlaceholderText(/ask hermes to analyze or transform/i),
      { target: { value: "Run no output" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    const noOutputBoundary = await screen.findByTestId("workspace-run-boundary");
    expect(within(noOutputBoundary).getByText(/No output boundary/i)).toBeTruthy();
    expect(within(noOutputBoundary).getByText(/status=no_output/i)).toBeTruthy();
    expect(
      within(noOutputBoundary).getByText(/No output was intentionally generated for this request\./i),
    ).toBeTruthy();
    await screen.findByText(/Generated outputs \(0\)/i);
    expect(screen.queryByRole("button", { name: /download generated file/i })).toBeNull();
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
