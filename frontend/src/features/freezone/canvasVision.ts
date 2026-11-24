import { uploadFreezoneImage } from '@/api/ops';
import { getNodeSize } from '@/features/canvas/application/autoLayout';
import { loadImageElement } from '@/features/canvas/application/imageData';
import {
  CANVAS_NODE_TYPES,
  resolveNodeSourceImageUrl,
  type CanvasEdge,
  type CanvasNode,
} from '@/features/canvas/domain/canvasNodes';
import type { ChatAttachment } from '@/features/superchat/types';

export const CANVAS_VISUAL_OVERVIEW_TYPE = 'canvas_visual_overview';

type Rect = { x: number; y: number; width: number; height: number };
type VisualNode = Rect & {
  id: string; number: number; label: string; text: string;
  group: boolean; imageUrl: string | null;
};
export type CanvasVisionScene = {
  nodes: VisualNode[];
  edges: { source: string; target: string }[];
  bounds: Rect;
};

export function needsCanvasVision(text: string): boolean {
  return /画布|布局|排版|整理|重叠|对齐|排列|摆放|整体效果|越.*越乱|canvas|\blayout\b|\barrang\w*\b|\boverlap\w*\b/i.test(text);
}

function nodeString(node: CanvasNode, keys: string[]): string {
  const data = node.data as Record<string, unknown>;
  for (const key of keys) {
    if (typeof data[key] === 'string' && data[key]) return data[key] as string;
  }
  return '';
}

export function buildCanvasVisionScene(nodes: CanvasNode[], edges: CanvasEdge[]): CanvasVisionScene {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const positions = new Map<string, { x: number; y: number } | null>();
  function position(node: CanvasNode, visiting = new Set<string>()): { x: number; y: number } | null {
    if (positions.has(node.id)) return positions.get(node.id)!;
    if (node.hidden || visiting.has(node.id) || !Number.isFinite(node.position.x) || !Number.isFinite(node.position.y)) return null;
    visiting.add(node.id);
    const parent = node.parentId ? byId.get(node.parentId) : undefined;
    const origin = parent ? position(parent, visiting) : { x: 0, y: 0 };
    visiting.delete(node.id);
    const result = origin ? { x: origin.x + node.position.x, y: origin.y + node.position.y } : null;
    positions.set(node.id, result);
    return result;
  }
  const visualNodes: VisualNode[] = [];
  for (const node of nodes) {
    const origin = position(node);
    const size = getNodeSize(node);
    if (!origin || !Number.isFinite(size.width) || !Number.isFinite(size.height) || size.width <= 0 || size.height <= 0) continue;
    visualNodes.push({
      id: node.id, number: visualNodes.length + 1, ...origin, ...size,
      group: node.type === CANVAS_NODE_TYPES.group,
      label: (nodeString(node, ['displayName', 'label', 'title', 'name']) || node.type || node.id).slice(0, 80),
      text: nodeString(node, ['text', 'content', 'prompt']).slice(0, 500),
      imageUrl: resolveNodeSourceImageUrl(node) || nodeString(node, ['previewImageUrl']) || null,
    });
  }
  const ids = new Set(visualNodes.map(node => node.id));
  const x = Math.min(...visualNodes.map(node => node.x));
  const y = Math.min(...visualNodes.map(node => node.y));
  return {
    nodes: visualNodes,
    edges: edges.filter(edge => !edge.hidden && ids.has(edge.source) && ids.has(edge.target))
      .map(edge => ({ source: edge.source, target: edge.target })),
    bounds: visualNodes.length ? {
      x, y,
      width: Math.max(...visualNodes.map(node => node.x + node.width)) - x,
      height: Math.max(...visualNodes.map(node => node.y + node.height)) - y,
    } : { x: 0, y: 0, width: 0, height: 0 },
  };
}

export async function renderCanvasVisionScene(scene: CanvasVisionScene): Promise<{ blob: Blob; missingPreviews: number }> {
  const canvas = document.createElement('canvas');
  const padding = 24;
  const scale = Math.min(1, 1232 / Math.max(scene.bounds.width, scene.bounds.height, 1));
  canvas.width = Math.ceil(scene.bounds.width * scale) + padding * 2;
  canvas.height = Math.ceil(scene.bounds.height * scale) + padding * 2;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Canvas rendering unavailable');
  ctx.fillStyle = '#f4f5f7';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const images = new Map<string, HTMLImageElement>();
  const candidates = scene.nodes.filter(node => node.imageUrl);
  const pending = candidates.slice(0, 32);
  const deadline = Date.now() + 5000;
  let cursor = 0;
  // Bound image decoding and wait time, including failed external previews.
  await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
    while (cursor < pending.length && Date.now() < deadline) {
      const node = pending[cursor++];
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const image = await Promise.race([
          loadImageElement(node.imageUrl!),
          new Promise<never>((_, reject) => {
            timer = setTimeout(() => reject(new Error('Preview timed out')), Math.max(1, deadline - Date.now()));
          }),
        ]);
        images.set(node.id, image);
      } catch { /* The overview labels unavailable previews explicitly. */ }
      finally { if (timer) clearTimeout(timer); }
    }
  }));
  const point = (node: Rect) => ({ x: padding + (node.x - scene.bounds.x) * scale, y: padding + (node.y - scene.bounds.y) * scale });
  const byId = new Map(scene.nodes.map(node => [node.id, node]));
  const drawNode = (node: VisualNode) => {
    const { x, y } = point(node);
    const w = node.width * scale;
    const h = node.height * scale;
    ctx.fillStyle = node.group ? '#e2e6ec' : '#ffffff';
    ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = node.group ? '#a7b0bc' : '#516171';
    ctx.lineWidth = 1;
    ctx.strokeRect(x, y, w, h);
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, w, h);
    ctx.clip();
    const image = images.get(node.id);
    const header = Math.min(20, h);
    if (image && h > header && image.naturalWidth && image.naturalHeight) {
      const fit = Math.min(w / image.naturalWidth, (h - header) / image.naturalHeight);
      const iw = image.naturalWidth * fit;
      const ih = image.naturalHeight * fit;
      ctx.drawImage(image, x + (w - iw) / 2, y + header + (h - header - ih) / 2, iw, ih);
    } else if (!node.group) {
      ctx.fillStyle = '#3f4956';
      ctx.font = '11px sans-serif';
      const text = node.imageUrl ? '[preview unavailable]' : node.text;
      for (let offset = 0, row = 0; offset < text.length && row < 8; row++) {
        const chars = Math.max(1, Math.floor(w / 10));
        ctx.fillText(text.slice(offset, offset + chars), x + 5, y + header + 15 + row * 15);
        offset += chars;
      }
    }
    ctx.fillStyle = '#202a37';
    ctx.fillRect(x, y, w, header);
    ctx.fillStyle = '#ffffff';
    ctx.font = '11px sans-serif';
    ctx.fillText(`${node.number}. ${node.label}`, x + 4, y + 14);
    ctx.restore();
  };
  scene.nodes.filter(node => node.group).forEach(drawNode);
  ctx.strokeStyle = '#74899d';
  ctx.lineWidth = 1;
  for (const edge of scene.edges) {
    const source = byId.get(edge.source)!;
    const target = byId.get(edge.target)!;
    const from = point(source);
    const to = point(target);
    ctx.beginPath();
    ctx.moveTo(from.x + source.width * scale, from.y + source.height * scale / 2);
    ctx.lineTo(to.x, to.y + target.height * scale / 2);
    ctx.stroke();
  }
  scene.nodes.filter(node => !node.group).forEach(drawNode);
  const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(
    value => value ? resolve(value) : reject(new Error('Canvas image encoding failed')), 'image/jpeg', 0.88,
  ));
  return { blob, missingPreviews: candidates.length - images.size };
}

export async function prepareCanvasVisualOverview(
  project: string, canvasId: string, nodes: CanvasNode[], edges: CanvasEdge[],
): Promise<{ attachment: ChatAttachment; context: string } | null> {
  const scene = buildCanvasVisionScene(nodes, edges);
  if (!scene.nodes.length) return null;
  const { blob, missingPreviews } = await renderCanvasVisionScene(scene);
  const uploaded = await uploadFreezoneImage(project, blob, 'canvas-overview.jpg', { timeoutMs: 10000 });
  return {
    attachment: {
      id: `canvas-visual-${canvasId}-${Date.now()}`,
      type: CANVAS_VISUAL_OVERVIEW_TYPE, mimeType: 'image/jpeg',
      fileName: 'canvas-overview.jpg', label: '画布视觉概览', fileSize: blob.size,
      url: uploaded.url,
      content: JSON.stringify({ source_url: uploaded.url, canvas_id: canvasId }),
    },
    context: [
      '[CANVAS_VISUAL_OVERVIEW]',
      'The first image is a fresh read-only visual overview of this canvas, rendered from actual node coordinates, dimensions and image previews. It preserves overlap. It is not a new canvas node or a pixel-perfect browser screenshot.',
      `Canvas: ${canvasId}. Unavailable image previews: ${missingPreviews}. Never claim to see unavailable previews.`,
      'Image labels map to real node IDs below. Use canvas tools for exact structure and modifications. After a layout command, use its receipt and new geometry to check overlap; this image describes the state BEFORE that command.',
      JSON.stringify(scene.nodes.map(({ id, number, label, x, y, width, height }) => ({ number, node_id: id, label, x, y, width, height }))),
      '[/CANVAS_VISUAL_OVERVIEW]',
    ].join('\n'),
  };
}
