export const DEFAULT_SIDEBAR_PERCENT = 38;

export function sidebarSizeBounds(containerWidth: number) {
  const width = Math.max(1, containerWidth);
  const sidebarMinimumPixels = Math.min(300, width * 0.4);
  const mainMinimumPixels = Math.min(420, width * 0.5);
  const minimum = Math.max(22, (sidebarMinimumPixels / width) * 100);
  const maximum = Math.min(68, 100 - ((mainMinimumPixels + 8) / width) * 100);
  return {
    minimum,
    maximum: Math.max(minimum, maximum)
  };
}

export function clampSidebarSize(value: number, containerWidth: number) {
  const { minimum, maximum } = sidebarSizeBounds(containerWidth);
  return Math.min(maximum, Math.max(minimum, value));
}
