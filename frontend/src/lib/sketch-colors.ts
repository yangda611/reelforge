// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * Helpers for the `sketch_colors` map on the episode script.
 *
 * `sketch_colors` lives on the script JSON as `{ identity_id: "#HEX NAME" }`
 * (e.g. `"#FF00FF MAGENTA"`).
 */

export function parseColorValue(value: string): { hex: string | null; name: string } {
  const match = value.trim().match(/^(#[0-9a-fA-F]{3,8})\s*(.*)$/);
  if (!match) return { hex: null, name: value.trim() };
  return { hex: match[1], name: match[2] };
}
