import { OFFICE_OUTCOME_ENVELOPE_INSTRUCTION } from "./documentOutcomeContract";

export type GuidedOfficeExtension = "docx" | "xlsx";

export interface GuidedTaskFile {
  id: string;
  filename: string;
  mime_type?: string;
}

export interface GuidedTaskDetailField {
  label: string;
  placeholder: string;
  helperText?: string;
  maxLength?: number;
}

export interface GuidedTaskPromptContext {
  selectedFiles: GuidedTaskFile[];
  detail?: string;
}

export interface GuidedDocumentTask {
  id: string;
  label: string;
  summary: string;
  family: GuidedOfficeExtension;
  allowedExtensions: GuidedOfficeExtension[];
  minFiles: number;
  maxFiles: number;
  detailField?: GuidedTaskDetailField;
  composePrompt: (context: GuidedTaskPromptContext) => string;
}

export interface GuidedTaskEligibility {
  eligible: boolean;
  errors: string[];
}

export interface GuidedPromptBuildResult {
  ok: boolean;
  prompt?: string;
  error?: string;
}

export const HONEST_OFFICE_BOUNDARY_INSTRUCTION =
  "Inspect only the attached Office files, never invent missing content, and clearly state uncertainty when evidence is incomplete.";

function getDetailText(detail: string | undefined): string | null {
  const normalized = detail?.trim();
  return normalized ? normalized : null;
}

export function getGuidedFileExtension(filename: string): string | null {
  const trimmed = filename.trim();
  if (!trimmed) return null;

  const lastDot = trimmed.lastIndexOf(".");
  if (lastDot < 0 || lastDot === trimmed.length - 1) return null;
  return trimmed.slice(lastDot + 1).toLowerCase();
}

function formatExtensionList(extensions: readonly GuidedOfficeExtension[]): string {
  return extensions.map((ext) => `.${ext}`).join(" or ");
}

function buildPrompt(
  taskLabel: string,
  instruction: string,
  context: GuidedTaskPromptContext,
): string {
  const filenames = context.selectedFiles.map((file) => file.filename).join(", ");
  const detail = getDetailText(context.detail);

  const promptParts = [
    `Run the guided task \"${taskLabel}\" on the attached Office document set.`,
    `Attached files: ${filenames}.`,
    instruction,
    detail ? `User focus detail: ${detail}.` : null,
    HONEST_OFFICE_BOUNDARY_INSTRUCTION,
    OFFICE_OUTCOME_ENVELOPE_INSTRUCTION,
    "Return concise, actionable output and reference which attached file supports each conclusion.",
  ];

  return promptParts.filter(Boolean).join("\n\n");
}

export const guidedDocumentTasks: GuidedDocumentTask[] = [
  {
    id: "docx-summary",
    label: "Summarize DOCX",
    summary: "Produce a concise executive summary from one DOCX document.",
    family: "docx",
    allowedExtensions: ["docx"],
    minFiles: 1,
    maxFiles: 1,
    detailField: {
      label: "Summary focus (optional)",
      placeholder: "e.g. Highlight risks, owners, and due dates",
      helperText: "Provide optional emphasis for the summary output.",
      maxLength: 240,
    },
    composePrompt: (context) =>
      buildPrompt(
        "Summarize DOCX",
        "Create a clear summary with key points, decisions, and next steps from the attached DOCX file. Also generate a downloadable markdown artifact named docx-summary-report.md containing that summary.",
        context,
      ),
  },
  {
    id: "docx-action-items",
    label: "Extract DOCX action items",
    summary: "Extract owners, deadlines, and unresolved action items from one or two DOCX files.",
    family: "docx",
    allowedExtensions: ["docx"],
    minFiles: 1,
    maxFiles: 2,
    detailField: {
      label: "Action-item filter (optional)",
      placeholder: "e.g. Include only unresolved engineering tasks",
      helperText: "Narrow extraction scope without rewriting the prompt.",
      maxLength: 240,
    },
    composePrompt: (context) =>
      buildPrompt(
        "Extract DOCX action items",
        "Extract action items, owners, and due dates from the attached DOCX files. Flag missing owners or ambiguous deadlines.",
        context,
      ),
  },
  {
    id: "xlsx-summary",
    label: "Summarize XLSX",
    summary: "Summarize one spreadsheet with notable metrics and trends.",
    family: "xlsx",
    allowedExtensions: ["xlsx"],
    minFiles: 1,
    maxFiles: 1,
    detailField: {
      label: "Summary focus (optional)",
      placeholder: "e.g. Prioritize revenue variance by region",
      helperText: "Optional focus area for spreadsheet summary.",
      maxLength: 240,
    },
    composePrompt: (context) =>
      buildPrompt(
        "Summarize XLSX",
        "Summarize key spreadsheet findings, notable trends, and important exceptions from the attached XLSX file.",
        context,
      ),
  },
  {
    id: "xlsx-anomalies",
    label: "Find XLSX anomalies",
    summary: "Find outliers and suspicious data quality patterns in one or two XLSX files.",
    family: "xlsx",
    allowedExtensions: ["xlsx"],
    minFiles: 1,
    maxFiles: 2,
    detailField: {
      label: "Anomaly focus (optional)",
      placeholder: "e.g. Flag abrupt week-over-week inventory swings",
      helperText: "Optional pattern hints for anomaly detection.",
      maxLength: 240,
    },
    composePrompt: (context) =>
      buildPrompt(
        "Find XLSX anomalies",
        "Identify anomalies, outliers, and suspicious value patterns in the attached XLSX files, then suggest likely root causes. Also generate a downloadable CSV artifact named xlsx-anomalies-export.csv listing anomaly rows and reason codes.",
        context,
      ),
  },
];

export function getGuidedTaskById(taskId: string): GuidedDocumentTask | null {
  return guidedDocumentTasks.find((task) => task.id === taskId) ?? null;
}

export function evaluateGuidedTaskEligibility(
  task: GuidedDocumentTask,
  selectedFiles: GuidedTaskFile[],
): GuidedTaskEligibility {
  const errors: string[] = [];

  if (selectedFiles.length < task.minFiles) {
    errors.push(
      `Select at least ${task.minFiles} ${formatExtensionList(task.allowedExtensions)} file${task.minFiles === 1 ? "" : "s"}.`,
    );
  }

  if (selectedFiles.length > task.maxFiles) {
    errors.push(
      `Select no more than ${task.maxFiles} ${formatExtensionList(task.allowedExtensions)} file${task.maxFiles === 1 ? "" : "s"}.`,
    );
  }

  const invalidFiles = selectedFiles.filter((file) => {
    const extension = getGuidedFileExtension(file.filename);
    if (!extension) return true;
    return !task.allowedExtensions.includes(extension as GuidedOfficeExtension);
  });

  if (invalidFiles.length > 0) {
    const invalidLabel = invalidFiles.map((file) => file.filename).join(", ");
    errors.push(
      `This task only accepts ${formatExtensionList(task.allowedExtensions)} files. Remove: ${invalidLabel}.`,
    );
  }

  return {
    eligible: errors.length === 0,
    errors,
  };
}

export function buildGuidedPrompt(
  task: GuidedDocumentTask,
  context: GuidedTaskPromptContext,
): GuidedPromptBuildResult {
  const eligibility = evaluateGuidedTaskEligibility(task, context.selectedFiles);
  if (!eligibility.eligible) {
    return {
      ok: false,
      error: eligibility.errors.join(" "),
    };
  }

  return {
    ok: true,
    prompt: task.composePrompt(context),
  };
}

export function validateGuidedTaskCatalog(tasks: GuidedDocumentTask[]): string[] {
  const errors: string[] = [];
  const seenIds = new Set<string>();

  for (const task of tasks) {
    if (!task.id.trim()) {
      errors.push("Task id must be non-empty.");
      continue;
    }

    if (seenIds.has(task.id)) {
      errors.push(`Duplicate guided task id: ${task.id}`);
    }
    seenIds.add(task.id);

    if (!task.label.trim()) {
      errors.push(`Task ${task.id} is missing a label.`);
    }

    if (task.minFiles < 1) {
      errors.push(`Task ${task.id} must require at least one file.`);
    }

    if (task.maxFiles < task.minFiles) {
      errors.push(`Task ${task.id} has invalid file limits (${task.minFiles}-${task.maxFiles}).`);
    }

    if (task.allowedExtensions.length === 0) {
      errors.push(`Task ${task.id} must declare allowedExtensions.`);
    }

    const samplePrompt = task.composePrompt({
      selectedFiles: [{ id: "sample", filename: `sample.${task.allowedExtensions[0]}` }],
      detail: "sample detail",
    });

    if (!samplePrompt.includes(HONEST_OFFICE_BOUNDARY_INSTRUCTION)) {
      errors.push(`Task ${task.id} prompt must include honest Office-boundary guidance.`);
    }

    if (!samplePrompt.includes(OFFICE_OUTCOME_ENVELOPE_INSTRUCTION)) {
      errors.push(`Task ${task.id} prompt must include structured Office outcome guidance.`);
    }
  }

  return errors;
}

const catalogErrors = validateGuidedTaskCatalog(guidedDocumentTasks);
if (catalogErrors.length > 0) {
  throw new Error(`Invalid guided document task catalog: ${catalogErrors.join(" | ")}`);
}
