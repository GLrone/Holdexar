export interface IslandMotionRect {
  left: number
  top: number
  width: number
  height: number
  borderRadius: string
}

export const islandSwapDuration = 320
export const islandSwapEasing = 'cubic-bezier(0.22, 1, 0.36, 1)'

export function createRectKeyframes(from: IslandMotionRect, to: IslandMotionRect): Keyframe[] {
  return [from, to].map((rect) => ({
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
    borderRadius: rect.borderRadius,
    opacity: 1,
  }))
}
