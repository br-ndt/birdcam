import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  
  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        '/api': 'http://localhost:5000',
        '/clips': 'http://localhost:5000',
        '/stream.mjpg': {
          target: 'http://localhost:5000',
          changeOrigin: true,
        },
      },
      // allow access from your DNS name/IP so you can test from other devices
      allowedHosts: env.VITE_ALLOWED_HOST ? [env.VITE_ALLOWED_HOST, 'localhost'] : ['localhost'],
    },
  }
})