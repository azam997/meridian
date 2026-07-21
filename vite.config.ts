import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    watch: {
      // Build-output trees that must never trigger dev reloads: running
      // `npm run release` (PyInstaller + cargo release) while `tauri dev`
      // is up otherwise spams page reloads until the dev app dies.
      ignored: [
        '**/src-tauri/target/**',
        '**/src-tauri/binaries/**',
        '**/python/build/**',
        '**/python/dist/**',
        '**/python/**/__pycache__/**',
      ],
    },
  },
})
