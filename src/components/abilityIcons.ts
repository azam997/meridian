// Icon URL helpers for the path the backend resolves for every ability it can
// name (AbilityMetaJson.iconPath / CastEvent.iconPath).
//
// The sidecar resolves each ability's icon (bundled map → disk cache → XIVAPI)
// and ships the relative path, so there's no hand-maintained name→URL table on
// the frontend to drift out of sync.
//
// Two sources, tried in order by AbilityIcon:
//   1. local — under Tauri, the warmed disk cache served by the `aicon://`
//      protocol (src-tauri/src/icons.rs). Instant, offline, no xivapi hit.
//   2. xivapi — the remote URL, used as a fallback (cold cache / browser dev).
// When both fail, callers fall back to the procedural glyph tile.

import { isTauri } from '@tauri-apps/api/core';

const XIVAPI = 'https://xivapi.com';

/** Remote XIVAPI URL for a relative icon path. The fallback source. */
export const iconUrlFromPath = (
  path: string | undefined | null,
): string | undefined => (path ? `${XIVAPI}${path}` : undefined);

/** Flatten a relative XIVAPI icon path the same way the backend disk cache does
 *  (icon_cache.py::_local_path): `/i/003000/003501.png` → `003000_003501.png`. */
export const flatIconName = (path: string): string =>
  path.replace(/^\/+/, '').replace(/\//g, '_').replace(/^i_/, '');

/** Local `aicon://` URL for a relative icon path — the warmed disk cache served
 *  by the Tauri protocol handler. `undefined` outside Tauri (browser/dev mock),
 *  so those paths use the xivapi URL instead.
 *
 *  Windows serves custom schemes at `http://<scheme>.localhost/…`; macOS/Linux
 *  use `<scheme>://localhost/…`. Windows is the only shipped target. */
export const localIconUrl = (
  path: string | undefined | null,
): string | undefined =>
  path && isTauri() ? `http://aicon.localhost/${flatIconName(path)}` : undefined;
