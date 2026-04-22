import { ArrowRight, FileUp, Radio } from "lucide-react";

export default function EndUserWorkspacePage() {
  return (
    <section
      data-testid="end-user-home"
      className="border border-border/80 bg-background/40 p-5 sm:p-8"
    >
      <p className="text-[0.72rem] tracking-[0.14em] text-midground/70">
        HERMES WORKSPACE
      </p>

      <h1 className="mt-3 text-2xl sm:text-3xl tracking-[0.06em] text-midground">
        Work with your files in a live AI run.
      </h1>

      <p className="mt-4 max-w-2xl text-sm normal-case tracking-normal text-midground/75">
        This browser workspace is for end users: upload a document, start a run,
        and follow streaming activity without entering operator tools.
      </p>

      <ul className="mt-6 grid gap-3 sm:grid-cols-3">
        <li className="border border-border/70 bg-background/50 p-3 text-xs normal-case tracking-normal">
          <div className="mb-2 inline-flex items-center gap-1 text-midground">
            <FileUp className="h-3.5 w-3.5" />
            Upload
          </div>
          Attach local files directly in the workspace before you run prompts.
        </li>
        <li className="border border-border/70 bg-background/50 p-3 text-xs normal-case tracking-normal">
          <div className="mb-2 inline-flex items-center gap-1 text-midground">
            <ArrowRight className="h-3.5 w-3.5" />
            Run
          </div>
          Send prompts that execute against the live Hermes backend runtime.
        </li>
        <li className="border border-border/70 bg-background/50 p-3 text-xs normal-case tracking-normal">
          <div className="mb-2 inline-flex items-center gap-1 text-midground">
            <Radio className="h-3.5 w-3.5" />
            Stream
          </div>
          Track status updates, generated outputs, and request correlation IDs.
        </li>
      </ul>
    </section>
  );
}
