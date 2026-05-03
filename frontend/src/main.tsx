/**
 * React entrypoint.
 *
 * Wires up the QueryClient (server-state per docs/RULES.md §2) and renders the App
 * shell into the #root element declared in index.html.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import ReactDOM from 'react-dom/client';

import App from '@/App';
import { ThemeProvider } from '@/components/ThemeProvider';

import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Sane defaults — feature code can override per-query as needed.
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found in index.html.');
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <App />
      </ThemeProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
