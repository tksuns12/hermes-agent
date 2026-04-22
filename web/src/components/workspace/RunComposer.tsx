import { Loader2, SendHorizonal } from "lucide-react";
import { Button } from "@/components/ui/button";

export interface RunComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => Promise<void> | void;
  pending: boolean;
  disabled?: boolean;
  mismatchActive?: boolean;
  selectedFileCount: number;
  placeholder?: string;
  submitLabel?: string;
  pendingLabel?: string;
}

export function RunComposer({
  value,
  onChange,
  onSubmit,
  pending,
  disabled = false,
  mismatchActive = false,
  selectedFileCount,
  placeholder = "Type your prompt...",
  submitLabel = "Send",
  pendingLabel = "Streaming...",
}: RunComposerProps) {
  const handleSubmit = (event: { preventDefault: () => void }) => {
    event.preventDefault();
    void onSubmit();
  };

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2">
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={pending || disabled}
        rows={4}
        placeholder={placeholder}
        className="flex min-h-[100px] w-full border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      />
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-muted-foreground">
          {mismatchActive
            ? "Tenant safety lock active — reload context to continue."
            : selectedFileCount > 0
              ? `${selectedFileCount} retained file(s) attached`
              : "Text-only run"}
        </span>
        <Button
          type="submit"
          disabled={pending || disabled || !value.trim()}
          className="gap-2"
        >
          {pending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <SendHorizonal className="h-4 w-4" />
          )}
          {pending ? pendingLabel : submitLabel}
        </Button>
      </div>
    </form>
  );
}
