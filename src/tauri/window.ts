// Window control helpers. In a browser (npm run dev without Tauri) these
// silently no-op; under Tauri they drive @tauri-apps/api/window.

import { isTauri } from '@tauri-apps/api/core';

export const minimizeWindow = async (): Promise<void> => {
  if (!isTauri()) return;
  const { getCurrentWindow } = await import('@tauri-apps/api/window');
  await getCurrentWindow().minimize();
};

export const toggleMaximize = async (): Promise<void> => {
  if (!isTauri()) return;
  const { getCurrentWindow } = await import('@tauri-apps/api/window');
  await getCurrentWindow().toggleMaximize();
};

export const closeWindow = async (): Promise<void> => {
  if (!isTauri()) return;
  const { getCurrentWindow } = await import('@tauri-apps/api/window');
  await getCurrentWindow().close();
};
