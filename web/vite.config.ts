import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        headers: {
          Host: '127.0.0.1:8765',
          Origin: 'http://127.0.0.1:8765',
        },
      },
      '/health': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
