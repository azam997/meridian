; Meridian NSIS installer hooks (wired via bundle.windows.nsis.installerHooks).
;
; Why: the updater's client-side teardown (seal + drain, run by the OLD app)
; cannot be trusted to have killed the sidecar tree by the time this installer
; copies files. Any surviving sim-pool worker still maps _internal/*.dll and
; turns the copy into "error opening file for writing" retry dialogs
; (observed live on the 0.9.0 -> 1.0.0 update, with both builds carrying the
; client-side fix). This installer belongs to the NEW version, so killing here
; protects every update INTO this build regardless of the old client's state.

!macro NSIS_HOOK_PREINSTALL
  ; Sidecar main process and its sim-pool workers share this image name
  ; (PyInstaller onedir: workers spawn via sys.executable). /T takes each
  ; tree; nsExec keeps the console window hidden. Exit code is irrelevant —
  ; "no such process" (128) is the happy path on a fresh install.
  nsExec::Exec 'taskkill /F /T /IM "fflogs-efficiency-analyzer-sidecar.exe"'
  Pop $0
  ; TerminateProcess is asynchronous; give the kernel a beat to tear the
  ; processes down and release the DLL handles before the copy starts.
  Sleep 800
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; Same locks would otherwise survive into uninstall's file removal.
  nsExec::Exec 'taskkill /F /T /IM "fflogs-efficiency-analyzer-sidecar.exe"'
  Pop $0
  Sleep 800
!macroend
