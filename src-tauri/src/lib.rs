mod icons;

use tauri_plugin_opener::OpenerExt;

/// Open a URL in the default browser. Called from Rust so the opener plugin
/// bypasses the JS-side capability scope — no need to enumerate OAuth URLs
/// in capabilities. (Used by the FFLogs sign-in flow.)
#[tauri::command]
async fn open_url(app: tauri::AppHandle, url: String) -> Result<(), String> {
  app
    .opener()
    .open_url(url, None::<&str>)
    .map_err(|e| e.to_string())
}

/// Reveal a file in Explorer (select it in its folder). Used by Submit
/// Feedback to hand the user the exported diagnostics zip for manual attach.
#[tauri::command]
async fn reveal_path(app: tauri::AppHandle, path: String) -> Result<(), String> {
  app
    .opener()
    .reveal_item_in_dir(path)
    .map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_opener::init())
    .plugin(tauri_plugin_updater::Builder::new().build())
    .plugin(tauri_plugin_process::init())
    // Serve warmed ability icons from disk (local-first; see icons.rs).
    .register_uri_scheme_protocol("aicon", icons::serve)
    .invoke_handler(tauri::generate_handler![open_url, reveal_path])
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
