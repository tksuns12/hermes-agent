import { useMemo, useState } from "react";
import { ListChecks, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  buildGuidedPrompt,
  evaluateGuidedTaskEligibility,
  getGuidedTaskById,
  guidedDocumentTasks,
  type GuidedTaskFile,
} from "@/features/end-user/guidedDocumentTasks";
import type { WorkbenchFileMetadata } from "@/lib/api";

export interface GuidedTaskPanelProps {
  selectedFiles: WorkbenchFileMetadata[];
  runPending: boolean;
  mismatchActive: boolean;
  onRunGuided: (prompt: string) => Promise<boolean> | boolean;
}

function toGuidedFiles(files: WorkbenchFileMetadata[]): GuidedTaskFile[] {
  return files.map((file) => ({
    id: file.id,
    filename: file.filename,
    mime_type: file.mime_type,
  }));
}

export function GuidedTaskPanel({
  selectedFiles,
  runPending,
  mismatchActive,
  onRunGuided,
}: GuidedTaskPanelProps) {
  const [selectedTaskId, setSelectedTaskId] = useState(guidedDocumentTasks[0]?.id ?? "");
  const [detail, setDetail] = useState("");

  const task = useMemo(
    () => (selectedTaskId ? getGuidedTaskById(selectedTaskId) : null),
    [selectedTaskId],
  );

  const guidedFiles = useMemo(() => toGuidedFiles(selectedFiles), [selectedFiles]);

  const eligibility = useMemo(() => {
    if (!task) {
      return { eligible: false, errors: ["Select a guided task to continue."] };
    }
    return evaluateGuidedTaskEligibility(task, guidedFiles);
  }, [guidedFiles, task]);

  const runDisabled =
    mismatchActive || runPending || !task || guidedFiles.length === 0 || !eligibility.eligible;

  const handleGuidedRun = async () => {
    if (!task || runDisabled) return;

    const prompt = buildGuidedPrompt(task, {
      selectedFiles: guidedFiles,
      detail,
    });

    if (!prompt.ok || !prompt.prompt) return;
    await onRunGuided(prompt.prompt);
  };

  return (
    <section
      className="border border-border/70 bg-background/50 p-3"
      data-testid="guided-task-panel"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="inline-flex items-center gap-1.5 text-xs font-medium">
            <ListChecks className="h-3.5 w-3.5 text-muted-foreground" />
            Guided Office task
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Pick a DOCX/XLSX job, optionally add focus, and launch in one click.
          </p>
        </div>
      </div>

      <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <label className="text-xs" htmlFor="guided-task-select">
          <span className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
            Guided task
          </span>
          <select
            id="guided-task-select"
            aria-label="Guided task"
            className="w-full border border-input bg-background px-2 py-2 text-xs"
            value={selectedTaskId}
            onChange={(event) => {
              setSelectedTaskId(event.target.value);
              setDetail("");
            }}
            disabled={mismatchActive || runPending}
          >
            {guidedDocumentTasks.map((guidedTask) => (
              <option key={guidedTask.id} value={guidedTask.id}>
                {guidedTask.label}
              </option>
            ))}
          </select>
        </label>

        {task?.detailField ? (
          <label className="text-xs" htmlFor="guided-detail-input">
            <span className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
              {task.detailField.label}
            </span>
            <input
              id="guided-detail-input"
              aria-label={task.detailField.label}
              value={detail}
              onChange={(event) => setDetail(event.target.value)}
              maxLength={task.detailField.maxLength}
              placeholder={task.detailField.placeholder}
              className="w-full border border-input bg-background px-2 py-2 text-xs"
              disabled={mismatchActive || runPending}
            />
          </label>
        ) : null}
      </div>

      {task ? (
        <p className="mt-3 border border-border/60 bg-background/60 p-2 text-[11px] text-muted-foreground">
          {task.summary} · accepts {task.allowedExtensions.map((ext) => `.${ext}`).join("/")} · {task.minFiles}
          –{task.maxFiles} file(s)
        </p>
      ) : null}

      <div className="mt-3 border border-border/60 bg-background/60 p-2">
        <p className="text-[11px] font-medium">Selected files for this task ({guidedFiles.length})</p>
        {guidedFiles.length === 0 ? (
          <p className="mt-1 text-[11px] text-muted-foreground">No retained files selected yet.</p>
        ) : (
          <ul className="mt-1 list-inside list-disc space-y-0.5 text-[11px] text-muted-foreground">
            {guidedFiles.map((file) => (
              <li key={file.id}>{file.filename}</li>
            ))}
          </ul>
        )}
      </div>

      {eligibility.eligible ? (
        <p className="mt-3 text-[11px] text-success">
          Selection is eligible for this guided task.
        </p>
      ) : (
        <ul className="mt-3 list-inside list-disc space-y-1 border border-destructive/40 bg-destructive/[0.08] p-2 text-[11px] text-destructive">
          {eligibility.errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex items-center justify-between gap-2">
        <p className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
          <Sparkles className="h-3.5 w-3.5" />
          Freeform composer remains available below as fallback.
        </p>
        <Button
          type="button"
          size="sm"
          onClick={() => void handleGuidedRun()}
          disabled={runDisabled}
        >
          Run guided task
        </Button>
      </div>
    </section>
  );
}
