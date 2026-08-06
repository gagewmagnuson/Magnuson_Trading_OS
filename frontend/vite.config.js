import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  server: {
    proxy: {
      // during dev, forward /v1/* API calls to the FastAPI server on :8000
      '/v1': 'http://localhost:8000',
    },
  },
  // build output goes to dist/ (FastAPI serves this in production)
  build: { outDir: 'dist' },
})