import { describe, expect, it } from "vitest";
import {
  DOCUMENT_OUTCOME_ENVELOPE_END,
  DOCUMENT_OUTCOME_ENVELOPE_START,
  OFFICE_OUTCOME_ENVELOPE_INSTRUCTION,
} from "./documentOutcomeContract";
import {
  HONEST_OFFICE_BOUNDARY_INSTRUCTION,
  buildGuidedPrompt,
  evaluateGuidedTaskEligibility,
  getGuidedTaskById,
  guidedDocumentTasks,
  validateGuidedTaskCatalog,
} from "./guidedDocumentTasks";

function file(id: string, filename: string) {
  return { id, filename };
}

describe("guidedDocumentTasks catalog", () => {
  it("defines four guided Office jobs with explicit file rules", () => {
    expect(guidedDocumentTasks).toHaveLength(4);

    const docxTasks = guidedDocumentTasks.filter((task) => task.family === "docx");
    const xlsxTasks = guidedDocumentTasks.filter((task) => task.family === "xlsx");

    expect(docxTasks).toHaveLength(2);
    expect(xlsxTasks).toHaveLength(2);

    for (const task of guidedDocumentTasks) {
      expect(task.allowedExtensions.length).toBeGreaterThan(0);
      expect(task.minFiles).toBeGreaterThanOrEqual(1);
      expect(task.maxFiles).toBeGreaterThanOrEqual(task.minFiles);
    }
  });

  it("passes catalog validation checks", () => {
    const errors = validateGuidedTaskCatalog(guidedDocumentTasks);
    expect(errors).toEqual([]);
  });
});

describe("evaluateGuidedTaskEligibility", () => {
  it("rejects zero files", () => {
    const task = getGuidedTaskById("docx-summary");
    expect(task).toBeTruthy();

    const eligibility = evaluateGuidedTaskEligibility(task!, []);
    expect(eligibility.eligible).toBe(false);
    expect(eligibility.errors.join(" ")).toMatch(/Select at least 1/i);
  });

  it("rejects wrong extension family", () => {
    const task = getGuidedTaskById("docx-summary");
    expect(task).toBeTruthy();

    const eligibility = evaluateGuidedTaskEligibility(task!, [file("f1", "sheet.xlsx")]);
    expect(eligibility.eligible).toBe(false);
    expect(eligibility.errors.join(" ")).toMatch(/only accepts \.docx/i);
  });

  it("rejects too many files", () => {
    const task = getGuidedTaskById("xlsx-summary");
    expect(task).toBeTruthy();

    const eligibility = evaluateGuidedTaskEligibility(task!, [
      file("f1", "q1.xlsx"),
      file("f2", "q2.xlsx"),
    ]);
    expect(eligibility.eligible).toBe(false);
    expect(eligibility.errors.join(" ")).toMatch(/no more than 1/i);
  });

  it("rejects mixed docx/xlsx files on single-family tasks", () => {
    const task = getGuidedTaskById("docx-action-items");
    expect(task).toBeTruthy();

    const eligibility = evaluateGuidedTaskEligibility(task!, [
      file("f1", "meeting.docx"),
      file("f2", "budget.xlsx"),
    ]);

    expect(eligibility.eligible).toBe(false);
    expect(eligibility.errors.join(" ")).toContain("budget.xlsx");
  });

  it("accepts one-file and two-file valid boundaries", () => {
    const requiresOne = getGuidedTaskById("docx-summary");
    const allowsTwo = getGuidedTaskById("xlsx-anomalies");
    expect(requiresOne).toBeTruthy();
    expect(allowsTwo).toBeTruthy();

    const oneFile = evaluateGuidedTaskEligibility(requiresOne!, [
      file("f1", "memo.docx"),
    ]);
    const twoFiles = evaluateGuidedTaskEligibility(allowsTwo!, [
      file("f1", "north.xlsx"),
      file("f2", "south.xlsx"),
    ]);

    expect(oneFile.eligible).toBe(true);
    expect(twoFiles.eligible).toBe(true);
  });
});

describe("buildGuidedPrompt", () => {
  it("returns blocking local errors for invalid file/task combinations", () => {
    const task = getGuidedTaskById("xlsx-summary");
    expect(task).toBeTruthy();

    const result = buildGuidedPrompt(task!, {
      selectedFiles: [file("f1", "policy.docx")],
      detail: "Focus on totals",
    });

    expect(result.ok).toBe(false);
    expect(result.prompt).toBeUndefined();
    expect(result.error).toMatch(/only accepts \.xlsx/i);
  });

  it("composes plain prompt text with honest Office-boundary and structured outcome instructions", () => {
    const task = getGuidedTaskById("docx-action-items");
    expect(task).toBeTruthy();

    const result = buildGuidedPrompt(task!, {
      selectedFiles: [file("f1", "notes.docx")],
      detail: "Highlight blocker ownership",
    });

    expect(result.ok).toBe(true);
    expect(typeof result.prompt).toBe("string");
    expect(result.prompt).toContain("notes.docx");
    expect(result.prompt).toContain("Highlight blocker ownership");
    expect(result.prompt).toContain(HONEST_OFFICE_BOUNDARY_INSTRUCTION);
    expect(result.prompt).toContain(OFFICE_OUTCOME_ENVELOPE_INSTRUCTION);
    expect(result.prompt).toContain(DOCUMENT_OUTCOME_ENVELOPE_START);
    expect(result.prompt).toContain(DOCUMENT_OUTCOME_ENVELOPE_END);
  });
});
