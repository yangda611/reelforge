import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { MultiSelectionToolbar } from '@/features/canvas/ui/MultiSelectionToolbar';
import { useCanvasStore } from '@/stores/canvasStore';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';

const warning = vi.hoisted(() => vi.fn());
vi.mock('sonner', () => ({ toast: { warning, success: vi.fn(), error: vi.fn() } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@xyflow/react', async (importOriginal) => ({
  ...await importOriginal<typeof import('@xyflow/react')>(),
  NodeToolbar: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock('@/features/canvas/ui/ZoomScaledToolbar', () => ({
  ZoomScaledToolbar: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock('@/features/freezone/canvasChatCommands', () => ({
  applyCanvasChatCommandsAsync: vi.fn(),
  cancelCanvasWorkflowExecution: vi.fn(),
  CANVAS_CHAT_COMMANDS_SCHEMA_VERSION: 1,
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it.each(['arrangeGrid', 'arrangeHorizontal', 'arrangeVertical'])('arranging %s moves selected groups without moving their selected children twice', (mode) => {
  useCanvasStore.setState({ nodes: [
    { id: 'g', type: 'groupNode', selected: true, position: { x: 100, y: 100 }, width: 1000, height: 900, measured: { width: 320, height: 200 }, data: {} },
    { id: 'a', type: 'imageNode', selected: true, parentId: 'g', position: { x: 20, y: 34 }, measured: { width: 700, height: 800 }, data: {} },
    { id: 'outside', type: 'imageNode', selected: true, position: { x: 150, y: 100 }, measured: { width: 700, height: 850 }, data: {} },
  ] as CanvasNode[], edges: [] });
  const view = render(<MultiSelectionToolbar />);
  fireEvent.click(view.getByRole('button', { name: 'canvas.multiSelect.arrange' }));
  const arrangeButtons = view.getAllByRole('button', { name: `canvas.multiSelect.${mode}` });
  fireEvent.click(arrangeButtons[arrangeButtons.length - 1]);
  const nodes = useCanvasStore.getState().nodes;
  expect(nodes.find((node) => node.id === 'a')?.position).toEqual({ x: 20, y: 34 });
  const group = nodes.find((node) => node.id === 'g')!;
  const outside = nodes.find((node) => node.id === 'outside')!;
  expect(outside.position.x >= group.position.x + 1000 || outside.position.y >= group.position.y + 900).toBe(true);
});

function seed(inGroup = false) {
  useCanvasStore.setState({ nodes: [
    { id: 'g', type: 'groupNode', position: { x: 0, y: 0 }, data: { storyGroup: true } },
    ...['a', 'b'].map((id) => ({ id, type: 'videoNode', selected: true,
      ...(inGroup ? { parentId: 'g' } : {}), position: { x: 0, y: 0 }, data: {} })),
  ] as CanvasNode[], edges: [] });
}

it('clicking the unavailable story creation entry explains the one-group limit', () => {
  seed();
  const before = useCanvasStore.getState().nodes;
  const view = render(<MultiSelectionToolbar />);
  fireEvent.click(view.getByRole('button', { name: 'canvas.multiSelect.group' }));
  fireEvent.click(view.getByRole('button', { name: 'canvas.story.messages.createStoryGroup' }));
  expect(warning).toHaveBeenCalledWith('canvas.story.messages.storyGroupLimit');
  expect(useCanvasStore.getState().nodes).toBe(before);
});

it('copying a selected whole story group explains why it was rejected', () => {
  seed(true);
  const before = useCanvasStore.getState().nodes;
  const view = render(<MultiSelectionToolbar />);
  fireEvent.click(view.getByRole('button', { name: 'canvas.multiSelect.duplicate' }));
  expect(warning).toHaveBeenCalledWith('canvas.story.messages.storyGroupLimit');
  expect(useCanvasStore.getState().nodes).toBe(before);
});
