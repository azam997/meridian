//! `aicon://` URI-scheme protocol — serves ability icons the Python sidecar has
//! already warmed into `~/.fflogs_efficiency_analyzer/icons/`.
//!
//! Local-first icons: the webview reads PNGs straight from disk instead of
//! hitting xivapi.com at render time. The frontend (`abilityIcons.ts` +
//! `AbilityIcon.tsx`) requests `http://aicon.localhost/<flat>.png` (Windows form
//! of a custom scheme; `aicon://localhost/<flat>.png` on macOS/Linux) and, on a
//! 404 here, falls back to the xivapi URL and finally a glyph tile. The flat
//! name matches Python's `icon_cache._local_path` (`/i/003000/003501.png` ->
//! `003000_003501.png`).
//!
//! No capability entry is needed (app-registered schemes work like commands),
//! and CSP is currently `null` in tauri.conf.json so the `<img>` loads freely.
//! If CSP is ever enabled, add this scheme to `img-src`.

use std::borrow::Cow;
use std::path::PathBuf;

use tauri::http::{Request, Response, StatusCode};
use tauri::{Manager, Runtime, UriSchemeContext};

fn icons_dir<R: Runtime>(ctx: &UriSchemeContext<'_, R>) -> Option<PathBuf> {
    let home = ctx.app_handle().path().home_dir().ok()?;
    Some(home.join(".fflogs_efficiency_analyzer").join("icons"))
}

fn not_found() -> Response<Cow<'static, [u8]>> {
    Response::builder()
        .status(StatusCode::NOT_FOUND)
        .body(Cow::Borrowed(&[][..]))
        .unwrap()
}

pub fn serve<R: Runtime>(
    ctx: UriSchemeContext<'_, R>,
    request: Request<Vec<u8>>,
) -> Response<Cow<'static, [u8]>> {
    // e.g. aicon://localhost/003000_003501.png -> path "/003000_003501.png".
    let name = request.uri().path().trim_start_matches('/');

    // Path-traversal guard: flat [A-Za-z0-9_.-] names only (separators are
    // rejected, so no escaping the icons dir), and reject any "..".
    let safe = !name.is_empty()
        && !name.contains("..")
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'));
    if !safe {
        return not_found();
    }

    let Some(dir) = icons_dir(&ctx) else {
        return not_found();
    };

    match std::fs::read(dir.join(name)) {
        Ok(bytes) => Response::builder()
            .status(StatusCode::OK)
            .header("Content-Type", "image/png")
            .header("Cache-Control", "max-age=31536000, immutable")
            .body(Cow::Owned(bytes))
            .unwrap(),
        Err(_) => not_found(),
    }
}
