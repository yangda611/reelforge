// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
export interface SceneRef {
  scene_id: string;
  variant_id?: string;
  render_anchor_id?: string;
  render_anchor_source_id?: string;
}

export interface EpisodeSceneMenuItem {
  scene_id: string;
  base_scene_id?: string;
  variant_id?: string;
  time_of_day?: string;
}

export interface EpisodePropMenuItem {
  prop_id: string;
  prop_type?: string;
  visual_prompt?: string;
  description?: string;
  owner_identity_id?: string;
  marker_color?: string;
}

export interface S9DurationAssessment {
  language: string;
  constraint_summary: string;
  core_chain_seconds: string;
  estimated_total_seconds: number;
  segment_count: number;
  conclusion: string;
}

export interface S9AssetManifestItem {
  slot_label: string;
  media_type: "image" | "audio";
  asset_kind: "identity" | "scene" | "prop" | "voice" | "first_frame";
  name: string;
  asset_id: string;
  variant_id?: string;
  status_label?: string;
}

export interface S9SegmentAssetSlot {
  slot: number;
  media_type: "image" | "audio";
  asset_kind: "first_frame" | "identity" | "scene" | "prop" | "voice";
  name: string;
  asset_id: string;
  variant_id?: string;
}

export interface S9SubShot {
  shot_number: number;
  label: string;
  title: string;
  planned_duration_seconds: number;
  start_seconds: number;
  end_seconds: number;
  shot_scale_movement: string;
  camera_angle: string;
  camera_motion: string;
  content: string;
  audio_type: "dialogue" | "narration" | "silence";
  speaker: string;
  spoken_text: string;
  identity_ids: string[];
  scene_id: string;
  scene_variant_id?: string;
  prop_ids: string[];
}

export interface S9SegmentMeta {
  segment_number: number;
  segment_title: string;
  planned_duration_seconds: number;
  max_segment_seconds: number;
  story_beat: string;
  tail_anchor: boolean;
  include_first_frame: boolean;
  segment_asset_slots: S9SegmentAssetSlot[];
}

export interface S9EpisodePreviewMeta {
  version: number;
  generation_mode: string;
  max_segment_seconds: number;
  tail_anchor: boolean;
  visual_style_id: string;
  visual_style_label: string;
  aspect_ratio: string;
  duration_assessment?: S9DurationAssessment | null;
  asset_manifest: S9AssetManifestItem[];
}

export interface Episode {
  number: number;
  title: string;
  summary?: string;
  raw_content?: string;
  beat_source_text?: string;
  content_summary?: string;
  character_names?: string[];
  key_events?: string[];
  cliffhanger?: string;
  identity_ids?: string[];
  identity_default_map?: Record<string, string>;
  scene_menu?: EpisodeSceneMenuItem[];
  prop_menu?: EpisodePropMenuItem[];
  /** 该集已拆出的镜头数，由列表接口一次分组查询带出。未拆镜为 0。 */
  beat_count?: number;
  s9_preview_meta?: S9EpisodePreviewMeta | null;
}

export interface Chapter {
  number: number;
  title?: string | null;
  start_line?: number;
  end_line?: number;
  content?: string;
  word_count?: number;
  char_count?: number;
  scene_blocks?: ChapterSceneBlock[];
  /** Zero-based slice offsets for text before the first recognized scene. */
  unparsed_content_start_line?: number;
  unparsed_content_end_line?: number;
}

export interface ChapterSceneBlock {
  header: string;
  scene_no?: string;
  location?: string;
  time_of_day?: string;
  interior_exterior?: string;
  characters?: string[];
  /** Legacy payload field retained for compatibility with older backends. */
  content?: string;
  /** Zero-based slice offsets into Chapter.content.splitlines(). */
  content_start_line?: number;
  content_end_line?: number;
}

export interface Beat {
  beat_number: number;
  narration_segment: string;
  visual_description: string;
  scene_ref?: SceneRef | null;
  location?: string;
  location_description?: string;
  time_of_day?: string;
  speaker?: string;
  audio_type?: string; // "silence" | "narration" | "dialogue"
  video_prompt?: string;
  keyframe_prompt?: string;
  video_mode?: string; // "first_frame" | "keyframe"
  seedance2_config_json?: string;
  estimated_duration?: number;
  duration_seconds?: number | null;
  segment_title?: string;
  s9_subshots_json?: string;
  s9_segment_meta_json?: string;
  s9_subshots?: S9SubShot[];
  s9_segment_meta?: S9SegmentMeta | null;
  /** Identity IDs bound to this beat. */
  detected_identities?: string[];
  /** Prop IDs bound to this beat. */
  detected_props?: string[];
  /** True for user-inserted manual shots — only these can be deleted. */
  is_manual_shot?: boolean;
  frame_url?: string | null;
  video_url?: string | null;
  audio_url?: string | null;
  /** 实测音频时长（秒）；视频时长须 >= 此值，用作时长控件默认值/下限。 */
  audio_duration_seconds?: number | null;
}

export interface PipelineEpisodeStatus {
  episode: number;
  script: boolean;
  audio: boolean;
  video: boolean;
  compose: boolean;
}

export interface PipelineProjectStatus {
  ingested: boolean;
  configured: boolean;
  characters: number;
  episodes: number;
  portraits_done: boolean;
}

export interface PipelineStepStatus {
  identity_plan?: boolean;
  identity_images?: boolean;
  script?: boolean;
  tts?: boolean;
  video?: boolean;
}

export interface PipelineStatus {
  project: string;
  global: PipelineProjectStatus;
  current_episode: number | null;
  episode_status: PipelineStepStatus | null;
  next_step: string;
  next_step_name: string;
}
