export const DOCUMENT_OUTCOME_STATUSES = [
  "success",
  "partial_success",
  "unsupported",
  "no_output",
] as const;

export type DocumentOutcomeStatus = (typeof DOCUMENT_OUTCOME_STATUSES)[number];

export type DocumentOutcomeParseIssue =
  | "missing_envelope"
  | "multiple_envelopes"
  | "invalid_json"
  | "invalid_status"
  | "missing_explanation";

export interface DocumentOutcomeEnvelope {
  status: DocumentOutcomeStatus;
  explanation: string;
  nextSteps: string[];
}

export interface NormalizedDocumentOutcome extends DocumentOutcomeEnvelope {
  isFallback: boolean;
  parseIssue?: DocumentOutcomeParseIssue;
}

export interface ParseDocumentOutcomeResult {
  outcome: NormalizedDocumentOutcome;
  strippedOutput: string;
  foundEnvelope: boolean;
  rawEnvelope?: string;
}

interface OutcomeEnvelopeMatch {
  start: number;
  end: number;
  raw: string;
  payload: string;
}

export const DOCUMENT_OUTCOME_ENVELOPE_START = "<hermes_office_outcome>";
export const DOCUMENT_OUTCOME_ENVELOPE_END = "</hermes_office_outcome>";

const DOCUMENT_OUTCOME_STATUSES_DISPLAY = DOCUMENT_OUTCOME_STATUSES.join("|");

export const OFFICE_OUTCOME_ENVELOPE_INSTRUCTION = [
  "Always append exactly one machine-readable outcome envelope at the end of your response using this exact wrapper:",
  `${DOCUMENT_OUTCOME_ENVELOPE_START}{\"status\":\"${DOCUMENT_OUTCOME_STATUSES_DISPLAY}\",\"explanation\":\"<one-sentence reason>\",\"next_steps\":[\"<optional next step>\"]}${DOCUMENT_OUTCOME_ENVELOPE_END}`,
  "Only use the listed status values; choose unsupported or no_output whenever you cannot safely produce a complete Office artifact.",
].join(" ");

const FALLBACK_EXPLANATIONS: Record<DocumentOutcomeParseIssue, string> = {
  missing_envelope:
    "Structured Office outcome envelope was missing, so Hermes cannot claim success for this run.",
  multiple_envelopes:
    "Multiple structured Office outcome envelopes were returned, so Hermes cannot determine a single trustworthy outcome.",
  invalid_json:
    "Structured Office outcome envelope was malformed JSON, so Hermes cannot trust it as a successful result.",
  invalid_status:
    "Structured Office outcome envelope used an unsupported status, so Hermes cannot trust it as a successful result.",
  missing_explanation:
    "Structured Office outcome envelope omitted a usable explanation, so Hermes cannot claim success for this run.",
};

const FALLBACK_NEXT_STEPS = [
  "Review assistant output and attached files manually.",
  "Retry the guided task to produce a valid structured outcome envelope.",
];

function collapseBlankLines(text: string): string {
  return text
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function isDocumentOutcomeStatus(value: unknown): value is DocumentOutcomeStatus {
  return (
    typeof value === "string" &&
    (DOCUMENT_OUTCOME_STATUSES as readonly string[]).includes(value)
  );
}

function normalizeNextSteps(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .map((step) => (typeof step === "string" ? step.trim() : ""))
      .filter((step) => step.length > 0);
  }

  if (typeof value === "string") {
    const normalized = value.trim();
    return normalized ? [normalized] : [];
  }

  return [];
}

function parseEnvelopeMatches(output: string): OutcomeEnvelopeMatch[] {
  if (!output.includes(DOCUMENT_OUTCOME_ENVELOPE_START)) return [];

  const matches: OutcomeEnvelopeMatch[] = [];
  let cursor = 0;

  while (cursor < output.length) {
    const start = output.indexOf(DOCUMENT_OUTCOME_ENVELOPE_START, cursor);
    if (start < 0) break;

    const payloadStart = start + DOCUMENT_OUTCOME_ENVELOPE_START.length;
    const endTagStart = output.indexOf(DOCUMENT_OUTCOME_ENVELOPE_END, payloadStart);
    if (endTagStart < 0) break;

    const end = endTagStart + DOCUMENT_OUTCOME_ENVELOPE_END.length;
    matches.push({
      start,
      end,
      raw: output.slice(start, end),
      payload: output.slice(payloadStart, endTagStart).trim(),
    });

    cursor = end;
  }

  return matches;
}

function buildFallbackOutcome(issue: DocumentOutcomeParseIssue): NormalizedDocumentOutcome {
  return {
    status: "unsupported",
    explanation: FALLBACK_EXPLANATIONS[issue],
    nextSteps: FALLBACK_NEXT_STEPS,
    isFallback: true,
    parseIssue: issue,
  };
}

export function stripDocumentOutcomeEnvelope(output: string): string {
  const matches = parseEnvelopeMatches(output);
  if (matches.length === 0) return collapseBlankLines(output);

  let cursor = 0;
  const pieces: string[] = [];
  for (const match of matches) {
    if (match.start > cursor) {
      pieces.push(output.slice(cursor, match.start));
    }
    cursor = match.end;
  }
  if (cursor < output.length) {
    pieces.push(output.slice(cursor));
  }

  return collapseBlankLines(pieces.join("\n"));
}

export function parseDocumentOutcomeEnvelope(
  output: string,
): ParseDocumentOutcomeResult {
  const rawOutput = typeof output === "string" ? output : "";
  const matches = parseEnvelopeMatches(rawOutput);

  if (matches.length === 0) {
    return {
      outcome: buildFallbackOutcome("missing_envelope"),
      strippedOutput: collapseBlankLines(rawOutput),
      foundEnvelope: false,
    };
  }

  if (matches.length > 1) {
    return {
      outcome: buildFallbackOutcome("multiple_envelopes"),
      strippedOutput: stripDocumentOutcomeEnvelope(rawOutput),
      foundEnvelope: true,
      rawEnvelope: matches.map((match) => match.raw).join("\n"),
    };
  }

  const [match] = matches;
  let parsed: unknown;
  try {
    parsed = JSON.parse(match.payload);
  } catch {
    return {
      outcome: buildFallbackOutcome("invalid_json"),
      strippedOutput: stripDocumentOutcomeEnvelope(rawOutput),
      foundEnvelope: true,
      rawEnvelope: match.raw,
    };
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      outcome: buildFallbackOutcome("invalid_json"),
      strippedOutput: stripDocumentOutcomeEnvelope(rawOutput),
      foundEnvelope: true,
      rawEnvelope: match.raw,
    };
  }

  const status = (parsed as { status?: unknown }).status;
  const explanation = (parsed as { explanation?: unknown }).explanation;

  if (!isDocumentOutcomeStatus(status)) {
    return {
      outcome: buildFallbackOutcome("invalid_status"),
      strippedOutput: stripDocumentOutcomeEnvelope(rawOutput),
      foundEnvelope: true,
      rawEnvelope: match.raw,
    };
  }

  const normalizedExplanation =
    typeof explanation === "string" ? explanation.trim() : "";
  if (!normalizedExplanation) {
    return {
      outcome: buildFallbackOutcome("missing_explanation"),
      strippedOutput: stripDocumentOutcomeEnvelope(rawOutput),
      foundEnvelope: true,
      rawEnvelope: match.raw,
    };
  }

  const nextSteps = normalizeNextSteps(
    (parsed as { next_steps?: unknown; nextSteps?: unknown }).next_steps ??
      (parsed as { next_steps?: unknown; nextSteps?: unknown }).nextSteps,
  );

  return {
    outcome: {
      status,
      explanation: normalizedExplanation,
      nextSteps,
      isFallback: false,
    },
    strippedOutput: stripDocumentOutcomeEnvelope(rawOutput),
    foundEnvelope: true,
    rawEnvelope: match.raw,
  };
}
