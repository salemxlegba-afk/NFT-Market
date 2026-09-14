import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { spawn, ChildProcess } from 'child_process';
import { defineConfig, Plugin } from 'vite';

function discordBotPlugin(): Plugin {
  let botProcess: ChildProcess | null = null;
  return {
    name: 'discord-bot-runner',
    configureServer() {
      const token = process.env.DISCORD_TOKEN?.trim();
      if (!token) {
        console.log('[Discord Bot] DISCORD_TOKEN is not configured in environment secrets.');
        return;
      }
      console.log('[Discord Bot] Starting python3 bot_runner.py...');
      botProcess = spawn('python3', ['bot_runner.py'], {
        stdio: 'inherit',
        env: process.env,
      });
      botProcess.on('exit', (code) => {
        console.log(`[Discord Bot] Process exited with code ${code}`);
      });
      process.on('exit', () => {
        if (botProcess) botProcess.kill();
      });
    },
  };
}

export default defineConfig(() => {
  return {
    plugins: [react(), tailwindcss(), discordBotPlugin()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      // HMR is disabled in AI Studio via DISABLE_HMR env var.
      // Do not modifyâfile watching is disabled to prevent flickering during agent edits.
      hmr: process.env.DISABLE_HMR !== 'true',
      // Disable file watching when DISABLE_HMR is true to save CPU during agent edits.
      watch: process.env.DISABLE_HMR === 'true' ? null : {},
      proxy: {
        '/api/wallet': {
          target: 'http://127.0.0.1:5000',
          changeOrigin: true,
        },
        '/wallet/verify': {
          target: 'http://127.0.0.1:5000',
          changeOrigin: true,
        },
      },
    },
  };
});
