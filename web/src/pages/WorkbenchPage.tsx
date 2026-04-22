import { AlertTriangle, Bot, Loader2, RefreshCw, Sparkles, UserRound } from "lucide-react";
import { H2 } from "@nous-research/ui";
import { MessageList } from "@/pages/SessionsPage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { timeAgo } from "@/lib/utils";
import { Toast } from "@/components/Toast";
import { DocumentRail } from "@/components/workspace/DocumentRail";
import { RunComposer } from "@/components/workspace/RunComposer";
import { useDocumentWorkspaceRuntime } from "@/hooks/useDocumentWorkspaceRuntime";

export default function WorkbenchPage() {
  const runtime = useDocumentWorkspaceRuntime();
  const {
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
    composer,
    setComposer,
    runPending,
    runError,
    runRequestId,
    streamRequestId,
    submitComposer,
    mismatchActive,
    tenantMismatchWarning,
    handleMismatchRecovery,
    activityEntries,
    loadSessions,
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

          {bootstrap.workbench.ignored_browser_user_id ? (
            <div className="mt-3 border border-warning/40 bg-warning/[0.08] p-3 text-xs text-warning">
              Browser user identifiers were ignored from:{" "}
              {bootstrap.workbench.ignored_browser_user_id_sources.join(", ") ||
                "unknown"}
            </div>
          ) : null}

          {runError ? (
            <div className="mt-3 border border-destructive/40 bg-destructive/[0.08] p-3 text-xs text-destructive">
              <strong>Proxy/stream error:</strong> {runError}
            </div>
          ) : null}

          {tenantMismatchWarning ? (
            <div className="mt-3 border border-warning/40 bg-warning/[0.08] p-3 text-xs text-warning">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <AlertTriangle className="h-4 w-4" />
                Tenant safety lock active
              </div>
              <p className="mt-2 text-[11px] leading-relaxed">
                {tenantMismatchWarning.message}
              </p>
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
                  <RefreshCw
                    className={`h-3.5 w-3.5 ${bootstrapLoading ? "animate-spin" : ""}`}
                  />
                  Reload tenant context
                </Button>
              </div>
            </div>
          ) : null}
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
                disabled={sessionsLoading || mismatchActive}
                aria-label="Refresh sessions"
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 ${sessionsLoading ? "animate-spin" : ""}`}
                />
              </Button>
            </div>

            {sessionsError ? (
              <p className="mb-2 border border-destructive/30 bg-destructive/[0.08] p-2 text-xs text-destructive">
                {sessionsError}
              </p>
            ) : null}

            <div className="flex max-h-[60vh] flex-col gap-2 overflow-y-auto pr-1">
              {sessions.map((session) => {
                const active = session.id === selectedSessionId;
                const title = session.title || session.preview || "Untitled session";
                return (
                  <button
                    key={session.id}
                    type="button"
                    onClick={() => setSelectedSessionId(session.id)}
                    disabled={mismatchActive}
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

              {sessions.length === 0 && !sessionsLoading ? (
                <p className="py-8 text-center text-xs text-muted-foreground">
                  No sessions yet for this tenant.
                </p>
              ) : null}
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

            <RunComposer
              value={composer}
              onChange={setComposer}
              onSubmit={submitComposer}
              pending={runPending}
              disabled={mismatchActive}
              mismatchActive={mismatchActive}
              selectedFileCount={selectedFileIds.length}
              placeholder="Type your prompt for this tenant-scoped workbench..."
            />
          </section>

          <DocumentRail
            title="Workspace Files"
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
        </section>
      </div>

      <Toast toast={toast} />
    </>
  );
}
