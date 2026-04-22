import { useRef, type ChangeEvent } from "react";
import {
  CheckCircle2,
  FileDown,
  FileUp,
  Loader2,
  Paperclip,
  RefreshCw,
  Sparkles,
  XCircle,
} from "lucide-react";
import type { WorkbenchFileMetadata } from "@/lib/api";
import type {
  WorkspaceActivityEntry,
  WorkspaceGeneratedFile,
} from "@/hooks/useDocumentWorkspaceRuntime";
import { formatBytes } from "@/hooks/useDocumentWorkspaceRuntime";
import { Button } from "@/components/ui/button";

function formatClock(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export interface DocumentRailProps {
  title?: string;
  subtitle?: string;
  retainedFiles: WorkbenchFileMetadata[];
  selectedFiles: WorkbenchFileMetadata[];
  selectedFileIds: string[];
  generatedFiles: WorkspaceGeneratedFile[];
  activityEntries: WorkspaceActivityEntry[];
  filesLoading: boolean;
  filesError: string | null;
  filesRequestId: string | null;
  uploadPending: boolean;
  uploadError: string | null;
  uploadRequestId: string | null;
  runPending: boolean;
  mismatchActive: boolean;
  onRefreshFiles: () => Promise<void> | void;
  onUploadFile: (file: File) => Promise<void> | void;
  onToggleFileSelection: (fileId: string) => void;
  onDownloadFile: (file: { id: string; filename: string }) => Promise<void> | void;
}

export function DocumentRail({
  title = "Workspace Files",
  subtitle,
  retainedFiles,
  selectedFiles,
  selectedFileIds,
  generatedFiles,
  activityEntries,
  filesLoading,
  filesError,
  filesRequestId,
  uploadPending,
  uploadError,
  uploadRequestId,
  runPending,
  mismatchActive,
  onRefreshFiles,
  onUploadFile,
  onToggleFileSelection,
  onDownloadFile,
}: DocumentRailProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const handleUploadChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    await onUploadFile(file);
  };

  return (
    <aside className="flex min-h-[65vh] flex-col gap-3 border border-border bg-background/30 p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium">{title}</div>
          {subtitle ? (
            <p className="mt-1 text-[11px] text-muted-foreground">{subtitle}</p>
          ) : null}
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => void onRefreshFiles()}
            disabled={filesLoading || runPending || mismatchActive}
            aria-label="Refresh files"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${filesLoading ? "animate-spin" : ""}`} />
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 text-[11px]"
            disabled={uploadPending || runPending || mismatchActive}
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
            onChange={(event) => void handleUploadChange(event)}
          />
        </div>
      </div>

      <div className="text-[11px] text-muted-foreground">
        {filesRequestId ? `files_request_id=${filesRequestId}` : ""}
        {uploadRequestId ? ` · upload_request_id=${uploadRequestId}` : ""}
      </div>

      {filesError ? (
        <p className="border border-destructive/30 bg-destructive/[0.08] p-2 text-xs text-destructive">
          {filesError}
        </p>
      ) : null}

      {uploadError ? (
        <p className="border border-destructive/30 bg-destructive/[0.08] p-2 text-xs text-destructive">
          {uploadError}
        </p>
      ) : null}

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
                  onChange={() => onToggleFileSelection(file.id)}
                  disabled={runPending || mismatchActive}
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
                  onClick={() => void onDownloadFile(file)}
                  className="inline-flex h-6 w-6 items-center justify-center border border-border/80 text-muted-foreground hover:text-foreground"
                  title="Download file"
                  disabled={mismatchActive}
                >
                  <FileDown className="h-3.5 w-3.5" />
                </button>
              </div>
            </label>
          );
        })}

        {!filesLoading && retainedFiles.length === 0 ? (
          <div className="border border-border/60 bg-background/40 p-3 text-xs text-muted-foreground">
            No retained files yet for this tenant.
          </div>
        ) : null}
      </div>

      <div className="border border-border/70 bg-background/50 p-2">
        <div className="mb-2 flex items-center gap-2 text-xs font-medium">
          <Paperclip className="h-3.5 w-3.5 text-muted-foreground" />
          Attached to next run ({selectedFileIds.length})
        </div>
        {selectedFiles.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">Select retained files to attach.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {selectedFiles.map((file) => (
              <button
                key={file.id}
                type="button"
                className="inline-flex items-center gap-1 border border-primary/40 bg-primary/[0.08] px-2 py-1 text-[10px] text-primary"
                onClick={() => onToggleFileSelection(file.id)}
                disabled={mismatchActive}
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
            Generated files from <code>run.completed.files</code> appear here.
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
                  onClick={() =>
                    void onDownloadFile({
                      id: file.id,
                      filename: file.filename,
                    })
                  }
                  className="inline-flex h-6 w-6 items-center justify-center border border-border/80 text-muted-foreground hover:text-foreground"
                  title="Download generated file"
                  disabled={mismatchActive}
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
          <p className="text-[11px] text-muted-foreground">Stream/tool activity appears here.</p>
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
                  {entry.requestId ? (
                    <p className="mt-1 truncate text-[10px] text-muted-foreground">
                      request_id={entry.requestId}
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
}
