// Reveal a file in Explorer (selected in its folder) via the Rust
// `reveal_path` command — Submit Feedback hands the user the exported
// diagnostics zip this way. Fails in plain-browser dev; callers swallow the
// rejection and show the path as text instead.

import { invoke } from '@tauri-apps/api/core';

export const revealPath = (path: string): Promise<void> =>
  invoke<void>('reveal_path', { path });
