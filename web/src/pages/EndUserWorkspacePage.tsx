import { AlertTriangle, Loader2, RefreshCw, Sparkles, UserRound } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Toast } from "@/components/Toast";
import { DocumentRail } from "@/components/workspace/DocumentRail";
import { GuidedTaskPanel } from "@/components/workspace/GuidedTaskPanel";
import { RunComposer } from "@/components/workspace/RunComposer";
import { useDocumentWorkspaceRuntime } from "@/hooks/useDocumentWorkspaceRuntime";
import { timeAgo } from "@/lib/utils";
import { MessageList } from "@/pages/SessionsPage";

export default function EndUserWorkspacePage() {
  const runtime = useDocumentWorkspaceRuntime();
  const {
    toast,
    bootstrap,
    bootstrapLoading,
    bootstrapError,
    loadBootstrap,
    sessions,
    selectedSession,
    selectedSessionId,
    setSelectedSessionId,
    sessionsLoading,
    sessionsError,
    loadSessions,
    messages,
    messagesLoading,
    messagesError,
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
    runOutcome,
    activityEntries,
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
  } = runtime;

  if (bootstrapLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
      </div>
    );
  }

  if (!bootstrap || bootstrapError) {
    return (
      <section className="border border-destructive/40 bg-destructive/[0.08] p-5 text-destructive">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <AlertTriangle className="h-4 w-4" />
          Failed to load workspace bootstrap.
        </div>
        <p className="mt-2 text-xs text-destructive/80">
          {bootstrapError ?? "Unknown bootstrap error."}
        </p>
        <Button variant="outline" size="sm" className="mt-4" onClick={() => void loadBootstrap()}>
          Retry bootstrap
        </Button>
      </section>
    );
  }

  return (
    <>
      <section
        data-testid="end-user-home"
        className="border border-border/80 bg-background/40 p-5 sm:p-8"
      >
        <p className="text-[0.72rem] tracking-[0.14em] text-midground/70">HERMES WORKSPACE</p>

        <h1 className="mt-3 text-2xl tracking-[0.06em] text-midground sm:text-3xl">
          Work with your files in a live AI run.
        </h1>

        <p className="mt-4 max-w-2xl text-sm normal-case tracking-normal text-midground/75">
          Upload documents, attach context, and follow live assistant streaming from the
          same-origin Hermes runtime.
        </p>

        <div className="mt-5 flex flex-wrap items-center gap-2 text-[11px]">
          <Badge variant="outline">tenant: {bootstrap.tenant.label}</Badge>
          <Badge variant={bootstrap.tenant.fallback ? "warning" : "success"}>
            {bootstrap.tenant.fallback ? "fallback" : "resolved"}
          </Badge>
          <Badge variant="secondary">source: {bootstrap.tenant.source}</Badge>
          {bootstrap.workbench.request_id ? (
            <Badge variant="secondary">bootstrap_request_id={bootstrap.workbench.request_id}</Badge>
          ) : null}
        </div>

        {tenantMismatchWarning ? (
          <div className="mt-4 border border-warning/40 bg-warning/[0.08] p-3 text-xs text-warning">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <AlertTriangle className="h-4 w-4" />
              Workspace safety lock active
            </div>
            <p className="mt-2 text-[11px] leading-relaxed">{tenantMismatchWarning.message}</p>
            <p className="mt-2 font-mono-ui text-[10px] text-warning/90">
              flow={tenantMismatchWarning.flow}
              {tenantMismatchWarning.expectedTenantId
                ? ` · expected=${tenantMismatchWarning.expectedTenantId}`
                : ""}
              {tenantMismatchWarning.observedTenantId
                ? ` · observed=${tenantMismatchWarning.observedTenantId}`
                : ""}
              {tenantMismatchWarning.storedTenantId
                ? ` · stored=${tenantMismatchWarning.storedTenantId}`
                : ""}
              {tenantMismatchWarning.requestId
                ? ` · request_id=${tenantMismatchWarning.requestId}`
                : ""}
            </p>
            {tenantMismatchWarning.storedTenantLabel ? (
              <p className="mt-1 text-[10px] text-warning/90">
                previous_label={tenantMismatchWarning.storedTenantLabel}
              </p>
            ) : null}
            <div className="mt-3">
              <Button
                variant="outline"
                size="sm"
                onClick={() => void handleMismatchRecovery()}
                disabled={bootstrapLoading}
                className="gap-2"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${bootstrapLoading ? "animate-spin" : ""}`} />
                Reload tenant context
              </Button>
            </div>
          </div>
        ) : null}

        {runError ? (
          <div className="mt-4 border border-destructive/40 bg-destructive/[0.08] p-3 text-xs text-destructive">
            <strong>Runtime error:</strong> {runError}
          </div>
        ) : null}

        <div className="mt-6 grid grid-cols-1 gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
          <DocumentRail
            title="Documents & Outputs"
            subtitle="Upload source docs, keep selected context, and retrieve generated files."
            retainedFiles={retainedFiles}
            selectedFiles={selectedFiles}
            selectedFileIds={selectedFileIds}
            generatedFiles={generatedFiles}
            runOutcome={runOutcome}
            activityEntries={activityEntries}
            filesLoading={filesLoading}
            filesError={filesError}
            filesRequestId={filesRequestId}
            uploadPending={uploadPending}
            uploadError={uploadError}
            uploadRequestId={uploadRequestId}
            runRequestId={runRequestId}
            streamRequestId={streamRequestId}
            runPending={runPending}
            mismatchActive={mismatchActive}
            onRefreshFiles={loadFiles}
            onUploadFile={uploadFile}
            onToggleFileSelection={toggleFileSelection}
            onDownloadFile={handleDownloadFile}
          />

          <section className="flex min-h-[65vh] flex-col gap-3 border border-border bg-background/30 p-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="text-sm font-medium">Conversation</div>
                <p className="mt-1 text-[11px] text-muted-foreground">
                  {selectedSession
                    ? `${selectedSession.title || "Untitled"} · last active ${timeAgo(selectedSession.last_active)}`
                    : "Start a new workspace conversation."}
                </p>
              </div>
              <div className="text-[11px] text-muted-foreground">
                {runRequestId ? `run_request_id=${runRequestId}` : ""}
                {streamRequestId ? ` · stream_request_id=${streamRequestId}` : ""}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2 border border-border/70 bg-background/50 p-2">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => void loadSessions()}
                disabled={sessionsLoading || mismatchActive}
                aria-label="Refresh sessions"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${sessionsLoading ? "animate-spin" : ""}`} />
              </Button>

              {sessions.slice(0, 8).map((session) => {
                const active = session.id === selectedSessionId;
                return (
                  <button
                    key={session.id}
                    type="button"
                    onClick={() => setSelectedSessionId(session.id)}
                    disabled={mismatchActive}
                    className={`border px-2 py-1 text-[11px] transition-colors ${
                      active
                        ? "border-primary/40 bg-primary/[0.08] text-primary"
                        : "border-border bg-background/60 text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {session.title || session.preview || "Untitled"}
                  </button>
                );
              })}

              {sessions.length === 0 && !sessionsLoading ? (
                <span className="text-[11px] text-muted-foreground">No sessions yet.</span>
              ) : null}
            </div>

            {sessionsError ? (
              <p className="border border-destructive/30 bg-destructive/[0.08] p-2 text-xs text-destructive">
                {sessionsError}
              </p>
            ) : null}

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
                  <p className="text-sm">Send a prompt to start this workspace run.</p>
                </div>
              ) : (
                <MessageList messages={messages} autoScrollToBottom />
              )}
            </div>

            <GuidedTaskPanel
              selectedFiles={selectedFiles}
              runPending={runPending}
              mismatchActive={mismatchActive}
              onRunGuided={(prompt, options) => submitPrompt(prompt, options)}
            />

            <div className="border border-border/70 bg-background/50 p-3">
              <p className="mb-2 text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
                Freeform fallback
              </p>
              <RunComposer
                value={composer}
                onChange={setComposer}
                onSubmit={submitComposer}
                pending={runPending}
                disabled={mismatchActive}
                mismatchActive={mismatchActive}
                selectedFileCount={selectedFileIds.length}
                placeholder="Ask Hermes to analyze or transform your uploaded documents..."
                submitLabel="Run"
                pendingLabel="Running..."
              />
            </div>

            <p className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              <Sparkles className="h-3.5 w-3.5" />
              Only safe metadata and request correlation IDs are shown in this workspace.
            </p>
          </section>
        </div>
      </section>

      <Toast toast={toast} />
    </>
  );
}
