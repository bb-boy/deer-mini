/** Keep API requests on the same deployment path as the built frontend. */
export function apiUrl(path: string): string {
  return `${import.meta.env.BASE_URL.replace(/\/+$/, "")}${path}`;
}
