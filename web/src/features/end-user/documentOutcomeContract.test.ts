import { describe, expect, it } from "vitest";
import {
  DOCUMENT_OUTCOME_ENVELOPE_END,
  DOCUMENT_OUTCOME_ENVELOPE_START,
  DOCUMENT_OUTCOME_STATUSES,
  OFFICE_OUTCOME_ENVELOPE_INSTRUCTION,
  parseDocumentOutcomeEnvelope,
  stripDocumentOutcomeEnvelope,
} from "./documentOutcomeContract";

function envelope(payload: string): string {
  return `${DOCUMENT_OUTCOME_ENVELOPE_START}${payload}${DOCUMENT_OUTCOME_ENVELOPE_END}`;
}

describe("documentOutcomeContract", () => {
  it("documents the supported status values in the guided instruction", () => {
    expect(DOCUMENT_OUTCOME_STATUSES).toEqual([
      "success",
      "partial_success",
      "unsupported",
      "no_output",
    ]);

    expect(OFFICE_OUTCOME_ENVELOPE_INSTRUCTION).toContain(
      DOCUMENT_OUTCOME_ENVELOPE_START,
    );
    expect(OFFICE_OUTCOME_ENVELOPE_INSTRUCTION).toContain(
      DOCUMENT_OUTCOME_ENVELOPE_END,
    );
    expect(OFFICE_OUTCOME_ENVELOPE_INSTRUCTION).toContain(
      DOCUMENT_OUTCOME_STATUSES.join("|"),
    );
  });

  it("parses a valid success envelope and strips it from user-facing output", () => {
    const result = parseDocumentOutcomeEnvelope(
      [
        "Created a draft summary from meeting-notes.docx.",
        envelope(
          JSON.stringify({
            status: "success",
            explanation: "Output document generated successfully.",
            next_steps: ["Download the artifact"],
          }),
        ),
      ].join("\n\n"),
    );

    expect(result.outcome.isFallback).toBe(false);
    expect(result.outcome.status).toBe("success");
    expect(result.outcome.explanation).toBe(
      "Output document generated successfully.",
    );
    expect(result.outcome.nextSteps).toEqual(["Download the artifact"]);
    expect(result.strippedOutput).toBe(
      "Created a draft summary from meeting-notes.docx.",
    );
  });

  it("normalizes nextSteps from either next_steps array or nextSteps string", () => {
    const fromString = parseDocumentOutcomeEnvelope(
      envelope(
        JSON.stringify({
          status: "partial_success",
          explanation: "One table could not be parsed.",
          nextSteps: "Verify the malformed worksheet manually.",
        }),
      ),
    );

    expect(fromString.outcome.isFallback).toBe(false);
    expect(fromString.outcome.status).toBe("partial_success");
    expect(fromString.outcome.nextSteps).toEqual([
      "Verify the malformed worksheet manually.",
    ]);
  });

  it("fails closed when envelope is missing", () => {
    const result = parseDocumentOutcomeEnvelope("Plain answer without markers");

    expect(result.foundEnvelope).toBe(false);
    expect(result.outcome.isFallback).toBe(true);
    expect(result.outcome.status).toBe("unsupported");
    expect(result.outcome.parseIssue).toBe("missing_envelope");
    expect(result.strippedOutput).toBe("Plain answer without markers");
  });

  it("fails closed for malformed json, invalid status, and blank explanation", () => {
    const malformedJson = parseDocumentOutcomeEnvelope(
      envelope('{"status":"success"'),
    );
    expect(malformedJson.outcome.isFallback).toBe(true);
    expect(malformedJson.outcome.parseIssue).toBe("invalid_json");

    const invalidStatus = parseDocumentOutcomeEnvelope(
      envelope(
        JSON.stringify({
          status: "done",
          explanation: "Looks complete.",
        }),
      ),
    );
    expect(invalidStatus.outcome.isFallback).toBe(true);
    expect(invalidStatus.outcome.parseIssue).toBe("invalid_status");

    const missingExplanation = parseDocumentOutcomeEnvelope(
      envelope(
        JSON.stringify({
          status: "no_output",
          explanation: "   ",
        }),
      ),
    );
    expect(missingExplanation.outcome.isFallback).toBe(true);
    expect(missingExplanation.outcome.parseIssue).toBe("missing_explanation");
  });

  it("fails closed when multiple envelopes are present", () => {
    const result = parseDocumentOutcomeEnvelope(
      [
        envelope(
          JSON.stringify({
            status: "success",
            explanation: "first",
          }),
        ),
        envelope(
          JSON.stringify({
            status: "success",
            explanation: "second",
          }),
        ),
      ].join("\n"),
    );

    expect(result.outcome.isFallback).toBe(true);
    expect(result.outcome.parseIssue).toBe("multiple_envelopes");
    expect(result.strippedOutput).toBe("");
  });

  it("stripDocumentOutcomeEnvelope removes envelope markers while preserving narrative text", () => {
    const output = [
      "Line one",
      envelope(
        JSON.stringify({
          status: "unsupported",
          explanation: "Boundary reached",
        }),
      ),
      "Line two",
    ].join("\n\n");

    expect(stripDocumentOutcomeEnvelope(output)).toBe("Line one\n\nLine two");
  });
});
