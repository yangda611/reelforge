import { describe, expect, it } from 'vitest';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { buildCanvasVisionScene, needsCanvasVision } from '@/features/freezone/canvasVision';

function node(id: string, options: Partial<CanvasNode> = {}): CanvasNode {
  return { id, type: 'uploadNode', position: { x: 0, y: 0 }, data: {}, ...options } as CanvasNode;
}

describe('canvas visual overview', () => {
  it('keeps actual overlap and resolves nested group coordinates and measured dimensions', () => {
    const scene = buildCanvasVisionScene([
      node('group', { type: 'groupNode', position: { x: -100, y: 40 }, width: 800, height: 600 }),
      node('child', { parentId: 'group', position: { x: 20, y: 60 }, measured: { width: 500, height: 300 } }),
      node('nested', { type: 'groupNode', parentId: 'group', position: { x: 100, y: 100 }, width: 300, height: 200 }),
      node('grandchild', { parentId: 'nested', position: { x: 10, y: 20 }, width: 90, height: 70 }),
    ], [{ id: 'e', source: 'child', target: 'grandchild' }]);

    expect(scene.nodes.find(item => item.id === 'child')).toMatchObject({ x: -80, y: 100, width: 500, height: 300 });
    expect(scene.nodes.find(item => item.id === 'grandchild')).toMatchObject({ x: 10, y: 160 });
    expect(scene.bounds).toEqual({ x: -100, y: 40, width: 800, height: 600 });
    expect(scene.edges).toEqual([{ source: 'child', target: 'grandchild' }]);
  });

  it('excludes hidden subtrees and invalid geometry without breaking other nodes', () => {
    const scene = buildCanvasVisionScene([
      node('hidden', { hidden: true, type: 'groupNode' }),
      node('child', { parentId: 'hidden' }),
      node('bad', { position: { x: NaN, y: 0 } }),
      node('cycle-a', { parentId: 'cycle-b' }),
      node('cycle-b', { parentId: 'cycle-a' }),
      node('visible', { width: 120, height: 80 }),
    ], [{ id: 'e', source: 'hidden', target: 'visible' }]);

    expect(scene.nodes.map(item => item.id)).toEqual(['visible']);
    expect(scene.bounds).toEqual({ x: 0, y: 0, width: 120, height: 80 });
    expect(scene.edges).toEqual([]);
  });

  it('retains a real image and text content for visual inspection', () => {
    const scene = buildCanvasVisionScene([
      node('photo', { data: { imageUrl: '/static/p/photo.png', label: 'Actor' } as CanvasNode['data'] }),
      node('note', { type: 'textAnnotationNode', data: { text: 'Scene at sunset' } as CanvasNode['data'] }),
    ], []);
    expect(scene.nodes[0]).toMatchObject({ imageUrl: '/static/p/photo.png', label: 'Actor', number: 1 });
    expect(scene.nodes[1].text).toContain('Scene at sunset');
  });

  it.each(['帮我整理画布', '现在越整理越乱', '你能看到画布吗', '看看整体效果', 'arrange the canvas', 'fix overlapping nodes'])(
    'includes a fresh overview for %s', text => expect(needsCanvasVision(text)).toBe(true),
  );

  it('does not attach the canvas to unrelated conversation', () => {
    expect(needsCanvasVision('你好，介绍一下自己')).toBe(false);
  });
});
