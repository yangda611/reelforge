// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import type {
  Beat,
  S9EpisodePreviewMeta,
  S9SegmentMeta,
  S9SubShot,
  SceneRef,
} from "./episode";

export interface Script {
  beats: Beat[];
  review_summary?: string;
  // identity_id → "#HEX COLOR_NAME" (e.g. "#FF00FF MAGENTA"). Assigned by
  // EpisodeOptimizer.assign_sketch_colors on the backend; the whole string
  // is the value — FE parses out the hex for UI.
  sketch_colors?: Record<string, string>;
  s9_preview_meta?: S9EpisodePreviewMeta | null;
}

export interface BeatUpdate {
  narration_segment?: string;
  visual_description?: string;
  scene_ref?: SceneRef | null;
  time_of_day?: string;
  video_prompt?: string;
  keyframe_prompt?: string;
  video_mode?: string;
  seedance2_config_json?: string;
  audio_type?: string;
  speaker?: string;
  detected_identities?: string[];
  detected_props?: string[];
  segment_title?: string;
  s9_subshots?: S9SubShot[];
  s9_segment_meta?: S9SegmentMeta;
  planned_duration_seconds?: number;
  duration_seconds?: number;
  estimated_duration?: number;
}
