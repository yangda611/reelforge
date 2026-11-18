// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import {
  translateSkillDescription,
  translateSkillInputLabel,
  translateSkillName,
  translateSkillOutputLabel,
  translateSkillCardinality,
  translateSkillParameterLabel,
  translateSkillParameterOption,
  translateSkillRequirement,
} from "@/features/freezone/context/skillI18n";
import type { SkillDefinition } from "@/features/freezone/context/skillRoles";

const translations: Record<string, string> = {
  "viewer.threeD.skillDefinitions.freezone_set_selected_background.name": "设为当前背景",
  "viewer.threeD.skillDefinitions.freezone_set_selected_background.description": "将明确指定的图片设为当前镜头的背景。",
  "viewer.threeD.skillInputLabels.beat_context": "镜头上下文",
  "viewer.threeD.skillOutputLabels.current_frame_candidate": "分镜候选",
  "viewer.threeD.skillParameterLabels.freezone_scene_360.aspect_ratio": "比例",
  "viewer.threeD.skillParameterOptions.freezone_scene_360.aspect_ratio.2_1": "横幅 2:1",
  "viewer.threeD.skillParameterLabels.freezone_scene_360.reference_mode": "参考模式",
  "viewer.threeD.skillParameterOptions.freezone_scene_360.reference_mode.material_only": "仅外观",
  "viewer.threeD.skillParameterOptions.freezone_scene_360.reference_mode.scene_anchor": "场景锚点",
  "viewer.threeD.skillRequirement.required": "必填",
  "viewer.threeD.skillRequirement.optional": "可选",
  "viewer.threeD.skillCardinality.single": "单个",
  "viewer.threeD.skillCardinality.multi": "多个",
};

const t = ((key: string) => translations[key] ?? key) as never;

describe("mainline skill i18n", () => {
  it("translates backend English names and descriptions by skill id", () => {
    const skill = {
      id: "freezone.set_selected_background",
      display_name: "Set Selected Background",
      description: "Set an explicitly chosen image as the current beat background.",
    } as SkillDefinition;

    expect(translateSkillName(skill, t)).toBe("设为当前背景");
    expect(translateSkillDescription(skill, t)).toBe("将明确指定的图片设为当前镜头的背景。");
  });

  it("keeps skill node input/output labels in Chinese", () => {
    expect(translateSkillInputLabel("beat_context", "Beat context", t)).toBe("镜头上下文");
    expect(translateSkillOutputLabel("current_frame_candidate", "Current frame candidate", t)).toBe("分镜候选");
  });

  it("translates skill parameter labels and option values", () => {
    expect(
      translateSkillParameterLabel(
        "freezone.scene_360",
        "aspect_ratio",
        "Aspect ratio",
        t,
      ),
    ).toBe("比例");
    expect(
      translateSkillParameterOption(
        "freezone.scene_360",
        "aspect_ratio",
        "2:1",
        t,
      ),
    ).toBe("横幅 2:1");
    expect(
      translateSkillParameterLabel(
        "freezone.scene_360",
        "reference_mode",
        "Reference mode",
        t,
      ),
    ).toBe("参考模式");
    expect(
      translateSkillParameterOption(
        "freezone.scene_360",
        "reference_mode",
        "material_only",
        t,
      ),
    ).toBe("仅外观");
    expect(
      translateSkillParameterOption(
        "freezone.scene_360",
        "reference_mode",
        "scene_anchor",
        t,
      ),
    ).toBe("场景锚点");
  });

  it("translates requirement and cardinality labels", () => {
    expect(translateSkillRequirement(true, t)).toBe("必填");
    expect(translateSkillRequirement(false, t)).toBe("可选");
    expect(translateSkillCardinality("single", t)).toBe("单个");
    expect(translateSkillCardinality("multi", t)).toBe("多个");
  });
});
