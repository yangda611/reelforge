// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import { resolveInputsForSkill } from "@/features/freezone/context/skillNodeInputs";
import { nodeDataForOutput } from "@/features/freezone/context/skillNodeOutputs";
import type { SkillDefinition } from "@/features/freezone/context/skillRoles";

const beatContextSkill: SkillDefinition = {
  id: "freezone.scene_360",
  provider: "freezone_mainline",
  display_name: "Scene 360",
  description: "Scene 360",
  inputs: [
    {
      role: "beat_context",
      label: "Shot context",
      accepts: { node_types: ["beatContextNode"] },
      required: true,
      cardinality: "single",
    },
  ],
  outputs: [],
};

const frameInputSkill: SkillDefinition = {
  id: "freezone.scene_360",
  provider: "freezone_mainline",
  display_name: "Scene 360",
  description: "Scene 360",
  inputs: [
    {
      role: "frame",
      label: "Frame",
      accepts: { node_types: ["imageGenNode"], media_kinds: ["image"] },
      required: true,
      cardinality: "single",
    },
  ],
  outputs: [],
};

const propInputSkill: SkillDefinition = {
  id: "freezone.scene_360",
  provider: "freezone_mainline",
  display_name: "Scene 360",
  description: "Scene 360",
  inputs: [
    {
      role: "prop",
      label: "Prop",
      accepts: { node_types: ["uploadNode"], media_kinds: ["image"] },
      required: false,
      cardinality: "multi",
    },
  ],
  outputs: [],
};

const identityInputSkill: SkillDefinition = {
  id: "freezone.scene_360",
  provider: "freezone_mainline",
  display_name: "Scene 360",
  description: "Scene 360",
  inputs: [
    {
      role: "identity",
      label: "Identity",
      accepts: { node_types: ["uploadNode"], media_kinds: ["image"] },
      required: false,
      cardinality: "multi",
    },
  ],
  outputs: [],
};

describe("resolveInputsForSkill", () => {
  it("deduplicates reference edges for the same skill identity handle before submitting", () => {
    const nodes = new Map([
      [
        "identity_ref",
        {
          id: "identity_ref",
          type: "uploadNode",
          data: {
            imageUrl: "/assets/identity.png",
            media_kind: "image",
            __freezone_source: {
              kind: "identity",
              role: "character_identity",
              meta: { identity_id: "陆辰_青年时期" },
            },
          },
        },
      ],
      [
        "portrait_ref",
        {
          id: "portrait_ref",
          type: "uploadNode",
          data: {
            imageUrl: "/assets/portrait.png",
            media_kind: "image",
            __freezone_source: {
              kind: "identity",
              role: "character_portrait",
              meta: { character: "陆辰" },
            },
          },
        },
      ],
      [
        "skill_scene",
        {
          id: "skill_scene",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const inputs = resolveInputsForSkill(
      identityInputSkill,
      nodes.get("skill_scene")!,
      [
        {
          id: "edge_identity_ref",
          source: "identity_ref",
          target: "skill_scene",
          targetHandle: "identity:陆辰_青年时期",
          data: {
            role: "identity",
            reference_target: { kind: "identity", identity_id: "陆辰_青年时期" },
          },
        },
        {
          id: "edge_portrait_ref",
          source: "portrait_ref",
          target: "skill_scene",
          targetHandle: "identity:陆辰_青年时期",
          data: {
            role: "identity",
            reference_target: { kind: "identity", identity_id: "陆辰_青年时期" },
          },
        },
      ],
      nodes,
    );

    expect(inputs).toHaveLength(1);
    expect(inputs[0].node_id).toBe("identity_ref");
  });

  it("ignores no-prop sentinel reference edges", () => {
    const nodes = new Map([
      [
        "prop_image",
        {
          id: "prop_image",
          type: "uploadNode",
          data: {
            imageUrl: "/assets/no-prop.png",
            media_kind: "image",
            label: "__NO_PROP__",
          },
        },
      ],
      [
        "skill_scene",
        {
          id: "skill_scene",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const inputs = resolveInputsForSkill(
      propInputSkill,
      nodes.get("skill_scene")!,
      [
        {
          id: "edge_no_prop_to_skill",
          source: "prop_image",
          target: "skill_scene",
          targetHandle: "prop:__NO_PROP__",
          data: {
            role: "prop",
            reference_target: { kind: "prop", prop_id: "__NO_PROP__" },
          },
        },
      ],
      nodes,
    );

    expect(inputs).toEqual([]);
  });

  it("uses the current BeatContextNode draft as the beat_context input", () => {
    const nodes = new Map([
      [
        "context_beat",
        {
          id: "context_beat",
          type: "beatContextNode",
          data: {
            projectId: "demo",
            episode: 1,
            beat: 4,
            content: "DB 旧画面",
            snapshot: {
              visualDescription: "本地当前画面",
              detectedIdentities: ["面馆男青年_青年时期"],
              detectedProps: ["纸箱"],
            },
            beat_edit_fields: {
              visual_description: "本地当前画面",
              detected_identities: ["面馆女青年_青年时期"],
              detected_props: ["纸箱", "天线电视"],
            },
          },
        },
      ],
      [
        "skill_scene",
        {
          id: "skill_scene",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const inputs = resolveInputsForSkill(
      beatContextSkill,
      nodes.get("skill_scene")!,
      [
        {
          id: "edge_context_to_skill",
          source: "context_beat",
          target: "skill_scene",
          targetHandle: "beat_context",
          data: { role: "beat_context" },
        },
      ],
      nodes,
    );

    expect(inputs).toHaveLength(1);
    expect(inputs[0].beat_context).toEqual({
      episode: 1,
      beat: 4,
      visual_description: "本地当前画面",
      detected_identities: ["面馆女青年_青年时期", "面馆男青年_青年时期"],
      detected_props: ["纸箱", "天线电视"],
    });
  });
  it("uses standalone BeatContextNode beat_context without mainline provenance", () => {
    const standaloneContext = {
      schema: "beat_context.v1",
      source: "standalone",
      title: "自定义镜头",
      visual_description: "雨夜里，{{女主_雨衣}} 站在便利店门口拿着 [[红伞]] 回头。",
      narration_segment: "她终于意识到，这不是偶遇。",
      scene_id: "便利店门口",
      detected_identities: ["女主_雨衣"],
      detected_props: ["红伞"],
      sketch_colors: { "女主_雨衣": "#FF00FF MAGENTA" },
      prop_marker_colors: { "红伞": "#D32F2F RED" },
    };
    const nodes = new Map([
      [
        "context_beat",
        {
          id: "context_beat",
          type: "beatContextNode",
          data: {
            context_scope: "standalone",
            beat_context: standaloneContext,
            snapshot: {
              visualDescription: "旧 snapshot 不应覆盖标准上下文",
            },
          },
        },
      ],
      [
        "skill_scene",
        {
          id: "skill_scene",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const inputs = resolveInputsForSkill(
      beatContextSkill,
      nodes.get("skill_scene")!,
      [
        {
          id: "edge_context_to_skill",
          source: "context_beat",
          target: "skill_scene",
          targetHandle: "beat_context",
          data: { role: "beat_context" },
        },
      ],
      nodes,
    );

    expect(inputs).toHaveLength(1);
    expect(inputs[0].beat_context).toEqual(standaloneContext);
    expect(inputs[0]).not.toHaveProperty("mainline_context");
    expect(inputs[0]).not.toHaveProperty("slot_target");
  });

  it("keeps mainline and standalone BeatContextNode payloads aligned except provenance", () => {
    const common = {
      visual_description: "{{Kris}} 拿着一把 [[雨伞]]",
      narration_segment: "她终于意识到，这不是偶遇。",
      detected_identities: ["Kris"],
      detected_props: ["雨伞"],
      sketch_colors: { Kris: "#FF00FF" },
      prop_marker_colors: { "雨伞": "#B71C1C" },
    };
    const standaloneContext = {
      schema: "beat_context.v1",
      source: "standalone",
      title: "自定义镜头上下文",
      ...common,
    };
    const nodes = new Map([
      [
        "mainline_context",
        {
          id: "mainline_context",
          type: "beatContextNode",
          data: {
            projectId: "demo",
            episode: 1,
            beat: 4,
            snapshot: {
              visualDescription: common.visual_description,
              narrationSegment: common.narration_segment,
              detectedIdentities: common.detected_identities,
              detectedProps: common.detected_props,
              sketchColors: common.sketch_colors,
              propMarkerColors: common.prop_marker_colors,
            },
            mainline_context: [
              {
                kind: "beat",
                projectId: "demo",
                episode: 1,
                beat: 4,
              },
            ],
          },
        },
      ],
      [
        "standalone_context",
        {
          id: "standalone_context",
          type: "beatContextNode",
          data: {
            context_scope: "standalone",
            beat_context: standaloneContext,
            snapshot: {},
          },
        },
      ],
      [
        "skill_mainline",
        {
          id: "skill_mainline",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
      [
        "skill_standalone",
        {
          id: "skill_standalone",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const mainlineInputs = resolveInputsForSkill(
      beatContextSkill,
      nodes.get("skill_mainline")!,
      [
        {
          id: "edge_mainline_to_skill",
          source: "mainline_context",
          target: "skill_mainline",
          targetHandle: "beat_context",
          data: { role: "beat_context" },
        },
      ],
      nodes,
    );
    const standaloneInputs = resolveInputsForSkill(
      beatContextSkill,
      nodes.get("skill_standalone")!,
      [
        {
          id: "edge_standalone_to_skill",
          source: "standalone_context",
          target: "skill_standalone",
          targetHandle: "beat_context",
          data: { role: "beat_context" },
        },
      ],
      nodes,
    );

    expect(mainlineInputs[0].beat_context).toMatchObject({
      episode: 1,
      beat: 4,
      ...common,
    });
    expect(standaloneInputs[0].beat_context).toEqual(standaloneContext);
    expect(mainlineInputs[0]).toHaveProperty("mainline_context");
    expect(standaloneInputs[0]).not.toHaveProperty("mainline_context");
    expect(standaloneInputs[0]).not.toHaveProperty("slot_target");
  });

  it("ignores leaked standalone beat_context when the BeatContextNode has mainline provenance", () => {
    const nodes = new Map([
      [
        "context_beat",
        {
          id: "context_beat",
          type: "beatContextNode",
          data: {
            context_scope: "standalone",
            beat_context: {
              schema: "beat_context.v1",
              source: "standalone",
              title: "自定义镜头上下文",
              visual_description: "",
              narration_segment: "",
              detected_identities: [],
              detected_props: [],
              sketch_colors: {},
              prop_marker_colors: {},
            },
            projectId: "demo",
            episode: 1,
            beat: 3,
            snapshot: {
              visualDescription: "主线画面",
              narrationSegment: "主线旁白",
              sceneId: "主线场景",
              detectedIdentities: ["主线角色"],
              detectedProps: ["主线道具"],
            },
            mainline_context: [
              {
                kind: "beat",
                projectId: "demo",
                episode: 1,
                beat: 3,
              },
            ],
          },
        },
      ],
      [
        "skill_scene",
        {
          id: "skill_scene",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const inputs = resolveInputsForSkill(
      beatContextSkill,
      nodes.get("skill_scene")!,
      [
        {
          id: "edge_context_to_skill",
          source: "context_beat",
          target: "skill_scene",
          targetHandle: "beat_context",
          data: { role: "beat_context" },
        },
      ],
      nodes,
    );

    expect(inputs[0].beat_context).toMatchObject({
      episode: 1,
      beat: 3,
      scene_id: "主线场景",
      visual_description: "主线画面",
      narration_segment: "主线旁白",
      detected_identities: ["主线角色"],
      detected_props: ["主线道具"],
    });
    expect(inputs[0].beat_context).not.toHaveProperty("source", "standalone");
    expect(inputs[0]).toHaveProperty("mainline_context");
  });

  it("keeps mainline_context on skill output candidates as provenance", () => {
    const mainlineContext = [{ kind: "frame", episode: 1, beat: 4 }];

    const data = nodeDataForOutput(
      {
        role: "current_frame_candidate",
        media_type: "image",
        node_type: "imageGenNode",
        pushable: false,
        image_url: "/static/frame.png",
        mainline_context: mainlineContext,
      },
      "freezone.set_selected_background",
      "skill_bg",
    );

    expect(data.mainline_context).toEqual(mainlineContext);
    expect(data.candidate_origin).toEqual({
      skill_id: "freezone.set_selected_background",
      skill_node_id: "skill_bg",
    });
  });

  it("does not consume a normal candidate node mainline_context as skill input context", () => {
    const nodes = new Map([
      [
        "frame_candidate",
        {
          id: "frame_candidate",
          type: "imageGenNode",
          data: {
            imageUrl: "/static/frame.png",
            media_kind: "image",
            mainline_context: [{ kind: "frame", episode: 1, beat: 4 }],
            slot_target: { kind: "frame", episode: 1, beat: 4 },
            candidate_origin: {
              skill_id: "freezone.set_selected_background",
              skill_node_id: "skill_bg",
            },
          },
        },
      ],
      [
        "skill_scene",
        {
          id: "skill_scene",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const inputs = resolveInputsForSkill(
      frameInputSkill,
      nodes.get("skill_scene")!,
      [
        {
          id: "edge_frame_to_skill",
          source: "frame_candidate",
          target: "skill_scene",
          targetHandle: "frame",
          data: { role: "frame" },
        },
      ],
      nodes,
    );

    expect(inputs).toHaveLength(1);
    expect(inputs[0]).not.toHaveProperty("mainline_context");
    expect(inputs[0].slot_target).toEqual({ kind: "frame", episode: 1, beat: 4 });
    expect(inputs[0].candidate_origin).toEqual({
      skill_id: "freezone.set_selected_background",
      skill_node_id: "skill_bg",
    });
  });

  it("can still infer slot_target from normal candidate provenance without consuming it as context", () => {
    const nodes = new Map([
      [
        "legacy_frame_candidate",
        {
          id: "legacy_frame_candidate",
          type: "imageGenNode",
          data: {
            imageUrl: "/static/frame.png",
            media_kind: "image",
            mainline_context: [{ kind: "frame", episode: 1, beat: 4 }],
            candidate_origin: {
              skill_id: "freezone.set_selected_background",
              skill_node_id: "skill_bg",
            },
          },
        },
      ],
      [
        "skill_scene",
        {
          id: "skill_scene",
          type: "skillNode",
          data: { skill_id: "freezone.scene_360" },
        },
      ],
    ]);

    const inputs = resolveInputsForSkill(
      frameInputSkill,
      nodes.get("skill_scene")!,
      [
        {
          id: "edge_frame_to_skill",
          source: "legacy_frame_candidate",
          target: "skill_scene",
          targetHandle: "frame",
          data: { role: "frame" },
        },
      ],
      nodes,
    );

    expect(inputs).toHaveLength(1);
    expect(inputs[0]).not.toHaveProperty("mainline_context");
    expect(inputs[0].slot_target).toEqual({ kind: "frame", episode: 1, beat: 4 });
  });
});
