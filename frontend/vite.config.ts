import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import type { ServerResponse } from 'node:http';

/**
 * Forgetting to start the backend is the easiest mistake to make in this project,
 * and by default it is also the hardest to diagnose: the dev proxy answers with a
 * bare 500 and an empty body, which reaches the browser as "Request failed with
 * status code 500" and says nothing about what is actually wrong.
 *
 * So answer in the same shape the API uses for its own errors. The UI reads
 * `detail` either way, and the message names the thing that is missing.
 */
function unreachable(res: ServerResponse): void {
  if (typeof res.writeHead !== 'function' || res.headersSent) return;
  res.writeHead(503, { 'Content-Type': 'application/json' });
  res.end(
    JSON.stringify({
      success: false,
      error: 'The API is not running.',
      detail:
        'Cannot reach the backend on port 8000. Start it from the project root with ' +
        '`.venv/bin/uvicorn backend.main:app --reload --port 8000`, or run `./dev.sh`, ' +
        'which starts the API and this interface together.',
    }),
  );
}

/** The API and the generated overlay images both live on the backend. */
const backend = () => ({
  target: 'http://localhost:8000',
  configure: (proxy: { on: (event: string, handler: (...args: never[]) => void) => void }) => {
    proxy.on('error', ((_error: Error, _request: unknown, res: ServerResponse) => {
      unreachable(res);
    }) as never);
  },
});

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': backend(),
      '/storage': backend(),
    },
  },
});
