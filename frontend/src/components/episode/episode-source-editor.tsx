// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useState } from "react";
import { BookOpen } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

interface EpisodeSourceEditorLabels {
  rawLabel: string;
  rawActionLabel: string;
  noRawText: string;
  sourceLabel: string;
  sourceMeta: (count: number) => string;
  sourcePlaceholder: string;
  linePreviewLabel?: string;
  lineCount?: (count: number) => string;
  noLines?: string;
}

interface EpisodeSourceEditorProps {
  rawContent: string;
  sourceText: string;
  labels: EpisodeSourceEditorLabels;
  disabled?: boolean;
  saving?: boolean;
  headerRight?: React.ReactNode;
  onSave: (value: string) => void | Promise<void>;
  className?: string;
}

function normalizeSourceText(value: string): string {
  return value.replace(/\r\n?/g, "\n");
}

function countNonEmptyParagraphs(value: string): number {
  const normalized = normalizeSourceText(value).trim();
  if (!normalized) return 0;
  return normalized
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean).length;
}

export function EpisodeSourceEditor({
  rawContent,
  sourceText,
  labels,
  disabled = false,
  saving = false,
  headerRight,
  onSave,
  className,
}: EpisodeSourceEditorProps) {
  void saving;
  const [draft, setDraft] = useState(() => normalizeSourceText(sourceText));
  const [rawOpen, setRawOpen] = useState(false);

  useEffect(() => {
    setDraft(normalizeSourceText(sourceText));
  }, [sourceText]);

  const trimmedDraft = draft.trim();
  const savedTrimmed = normalizeSourceText(sourceText).trim();
  const charCount = trimmedDraft.length;
  const paragraphCount = countNonEmptyParagraphs(draft);

  const commitValue = (rawValue: string) => {
    const nextTrimmed = normalizeSourceText(rawValue).trim();
    if (nextTrimmed === savedTrimmed) return;
    void onSave(nextTrimmed);
  };

  return (
    <div className={cn("space-y-4", className)}>
      <section className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 flex-wrap items-baseline gap-2">
            <h2 className="text-sm font-semibold tracking-tight text-foreground">
              {labels.sourceLabel}
            </h2>
            <span className="text-[11px] text-muted-foreground/68">
              ({labels.sourceMeta(charCount)}
              {paragraphCount > 0 ? ` · ${paragraphCount} 段` : ""})
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setRawOpen(true)}
              className="h-7 gap-1.5 rounded-[7px] border border-white/[0.08] bg-white/[0.025] px-2.5 text-xs font-normal text-foreground/78 shadow-none hover:border-white/[0.16] hover:bg-white/[0.05] hover:text-foreground dark:border-white/[0.08] dark:bg-white/[0.025] dark:hover:bg-white/[0.05] [&_svg]:size-3.5"
            >
              <BookOpen />
              {labels.rawActionLabel}
            </Button>
            {headerRight}
          </div>
        </div>

        <textarea
          aria-label={labels.sourceLabel}
          value={draft}
          disabled={disabled}
          rows={14}
          placeholder={labels.sourcePlaceholder}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={(event) => commitValue(event.target.value)}
          className={cn(
            "ui-scrollbar min-h-[280px] w-full resize-y rounded-[12px] border border-white/[0.08] bg-[#0d0e14] px-3.5 py-2.5 text-[13px] leading-relaxed text-foreground outline-none transition-[border-color,box-shadow] duration-150",
            "placeholder:text-muted-foreground/50 hover:border-white/[0.14] focus:border-[var(--accent)]/60 focus:ring-[3px] focus:ring-[var(--accent)]/15",
            "disabled:cursor-not-allowed disabled:opacity-55",
          )}
        />
      </section>

      <Dialog open={rawOpen} onOpenChange={setRawOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{labels.rawLabel}</DialogTitle>
          </DialogHeader>
          <div className="max-h-[60vh] overflow-y-auto rounded-lg border border-border/60 bg-background/40 p-3 text-sm leading-relaxed text-foreground whitespace-pre-wrap">
            {rawContent.trim() ? rawContent : labels.noRawText}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
