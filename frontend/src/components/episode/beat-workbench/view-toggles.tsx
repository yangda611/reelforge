// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useTranslation } from "react-i18next";
import type { SelectionState } from "@/hooks/use-selection";

interface ViewTogglesProps {
  selection: SelectionState;
  totalBeats: number;
  onSelectAll?: () => void;
  onClearSelection: () => void;
}

const SELECT_ALL_BUTTON_CLASS =
  "inline-flex h-[22px] items-center rounded-[5px] border border-white/[0.08] bg-white/[0.03] px-2 text-[11px] font-medium text-foreground/80 transition-colors hover:border-white/[0.16] hover:bg-white/[0.06] hover:text-foreground";

/** Selection bar above the beat card grid: select-all / clear / total count. */
export function ViewToggles({
  selection,
  totalBeats,
  onSelectAll,
  onClearSelection,
}: ViewTogglesProps) {
  const { t } = useTranslation();
  const checkedCount = selection.mode === "multi" ? selection.checked.size : 0;
  const hasSelection = checkedCount > 0;
  const allSelected = totalBeats > 0 && checkedCount >= totalBeats;

  return (
    <div className="flex shrink-0 flex-col">
      <div className="flex min-h-10 items-center gap-2 border-b border-white/[0.055] px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {hasSelection ? (
            <>
              <span className="tabular-nums text-[11px] text-foreground/70">
                {t("episode.workbench.view.selectedCount", { count: checkedCount })}
              </span>
              {onSelectAll && !allSelected && (
                <button
                  type="button"
                  onClick={onSelectAll}
                  className={SELECT_ALL_BUTTON_CLASS}
                >
                  {t("episode.workbench.view.selectAll")}
                </button>
              )}
              <button
                type="button"
                onClick={onClearSelection}
                className="inline-flex h-[22px] items-center rounded-md px-2 text-[11px] text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground"
              >
                {t("episode.workbench.view.clear")}
              </button>
            </>
          ) : (
            onSelectAll &&
            totalBeats > 0 && (
              <button
                type="button"
                onClick={onSelectAll}
                className={SELECT_ALL_BUTTON_CLASS}
              >
                {t("episode.workbench.view.selectAll")}
              </button>
            )
          )}
        </div>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground/75">
          {t("episode.workbench.view.totalShots", { count: totalBeats })}
        </span>
      </div>
    </div>
  );
}