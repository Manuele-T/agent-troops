// Same-origin by default: the Vite dev server proxies /agents and /events
// to the backend (see vite.config.ts), and in production the backend serves the
// built bundle. Override with VITE_API_BASE only for split deployments.
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "";
