import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8710',
        changeOrigin: true,
        headers: { Origin: 'http://127.0.0.1:8710' },
      },
      '/__codereader_session': {
        target: 'http://127.0.0.1:8710',
        changeOrigin: true,
        rewrite: () => '/',
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 900,
  },
})
