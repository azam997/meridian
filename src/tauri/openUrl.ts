// Open a URL in the system default browser via the Rust `open_url` command
// (app-native commands bypass JS capability scoping — see lib.rs). Fails in
// plain-browser dev; callers fall back to showing the link or window.open.

import { invoke } from '@tauri-apps/api/core';

export const openUrl = (url: string): Promise<void> =>
  invoke<void>('open_url', { url });
