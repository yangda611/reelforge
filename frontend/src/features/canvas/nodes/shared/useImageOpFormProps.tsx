// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useCallback, type ReactNode } from 'react';

import {
  ASSET_BOARD_IMAGE_OP_MAP,
  clearAssetBoardImageOp,
  isAssetBoardImageOpKey,
  isGridImageOpKey,
  runAssetBoardImageOp,
} from '@/features/canvas/application/assetBoardImageOps';
import { GRID_ACTION_MODE_MAP } from '@/features/canvas/application/gridTemplateAction';
import { FREEZONE_IMAGE_FEATURES } from '@/features/canvas/application/freezoneImageFeatureBilling';
import {
  resolveFixedFeatureModelRequest,
  resolvePanoramaModelRequest,
} from '@/features/canvas/domain/fixedFeatureModelRequest';
import { useFreezoneImageModels } from '@/features/canvas/hooks/useFreezoneImageModels';
import { AssetBoardImageOpChip } from '@/features/canvas/ui/asset-board/AssetBoardImageOpChip';
import type { CreditPromotionDisplay } from '@/components/credits/credit-visual';
import { useGenerationCreditCost } from '@/lib/queries/generation-credit-cost';
import { useCanvasStore } from '@/stores/canvasStore';

/** 覆盖到 `ImageGenerationForm` 上的那几个 prop（功能节点专用）。 */
export interface ImageOpFormProps {
  promptLeadingChip: ReactNode;
  onPromptLeadingChipDelete: () => void;
  promptPlaceholder: string;
  onSubmit: () => void;
  submitDisabled: boolean;
  totalCreditCostDisplay: string | null;
  /**
   * 必须跟着 `totalCreditCostDisplay` 一起覆盖：这两个 prop 是 spread 在宿主表单
   * 之后的，只盖 display 会让宫格的价格配上底图询价的促销标签。
   */
  creditPromotion: CreditPromotionDisplay | null;
}

/**
 * 功能节点（点「九宫格 / 宫格模板」下拉某一项后建出来的那种）在生成表单上的差异
 * 部分：输入框**里**多一枚可切可删的功能 chip、功能说明当占位文案、↑ 走对应模板
 * 而不是常规文生图。
 *
 * 工作流（`ImageGenNode`）与故事板（`AssetBoardImageGenForm`）挂的是同一张
 * `ImageGenerationForm`，这条差异也就只该写一遍——两处 spread 同一个返回值，
 * 交互天然一致。普通图片生成节点没有 `imageOpKey`，返回 null，宿主渲染与从前一致。
 */
export function useImageOpFormProps(
  nodeId: string,
  options: { isGenerating: boolean },
): ImageOpFormProps | null {
  const opKeyRaw = useCanvasStore((state) => {
    const node = state.nodes.find((candidate) => candidate.id === nodeId);
    return (node?.data as { imageOpKey?: unknown } | undefined)?.imageOpKey;
  });
  const opKey = isAssetBoardImageOpKey(opKeyRaw) ? opKeyRaw : null;

  // 在输入框开头退格 = 删掉这枚 chip = 该节点退回普通图片生成。
  const handleChipDelete = useCallback(() => {
    clearAssetBoardImageOp(nodeId);
  }, [nodeId]);

  const { models } = useFreezoneImageModels();
  const isGridOp = opKey !== null && isGridImageOpKey(opKey);
  const modelRequest = isGridOp && models[0]
    ? resolveFixedFeatureModelRequest(models[0], { mode: GRID_ACTION_MODE_MAP[opKey] })
    : opKey === 'scene360' ? resolvePanoramaModelRequest(models) : null;
  const creditCost = useGenerationCreditCost(
    'feature',
    modelRequest
      ? (isGridOp ? FREEZONE_IMAGE_FEATURES.grid : FREEZONE_IMAGE_FEATURES.panorama)
      : null,
    { surface: 'canvas', params: modelRequest?.billingParams },
  );
  const handleSubmit = () => {
    if (!opKey || !modelRequest || options.isGenerating) return;
    void runAssetBoardImageOp(nodeId, opKey, modelRequest.submit);
  };

  if (!opKey) {
    return null;
  }

  return {
    promptLeadingChip: (
      <AssetBoardImageOpChip nodeId={nodeId} opKey={opKey} disabled={options.isGenerating} />
    ),
    onPromptLeadingChipDelete: handleChipDelete,
    promptPlaceholder: ASSET_BOARD_IMAGE_OP_MAP[opKey].description,
    // 功能提交不要求提示词（模板本身就是「基于当前图像生成」，提示词是可选补充），
    // 所以不能沿用共用表单那条 `submitDisabled = isGenerating || !hasEffectivePrompt`。
    onSubmit: handleSubmit,
    submitDisabled: options.isGenerating || !modelRequest,
    totalCreditCostDisplay: modelRequest ? (creditCost.data?.data.display ?? null) : null,
    creditPromotion: modelRequest ? (creditCost.data?.data.promotion ?? null) : null,
  };
}
