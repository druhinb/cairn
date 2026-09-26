import { toast } from "../components/toast.js";

/**
 * A query string from params, skipping null, undefined and "" values and repeating
 * array values (`status=a&status=b`).
 * @param {Record<string, any>} params @returns {string}
 */
export function qs(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item != null && item !== "") search.append(key, String(item));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/**
 * Call the JSON API. A failure shows a toast and rejects with an Error carrying
 * `status` and the JSON error body as `body`; `quiet` skips the toast for callers
 * that report the error themselves.
 * `keepalive` lets the request outlive the page, and aborting `signal` rejects with
 * the fetch's AbortError and no toast. A FormData body goes as multipart.
 * Every request carries the x-cairn header, without which the server refuses a change.
 * @param {string} path
 * @param {{method?: string, body?: any, quiet?: boolean, keepalive?: boolean, signal?: AbortSignal}} [options]
 * @returns {Promise<any>}
 */
export async function api(path, { method = "GET", body, quiet = false, keepalive = false, signal } = {}) {
  const init = { method, headers: { "x-cairn": "1" }, keepalive, signal };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (cause) {
    if (signal?.aborted) throw cause;
    const error = new Error("Cairn isn't responding. Reopen the app");
    error.status = 0;
    error.cause = cause;
    if (!quiet) toast(error.message, { tone: "error" });
    throw error;
  }
  const isJson = (response.headers.get("content-type") || "").includes("json");
  let payload = null;
  try {
    payload = isJson ? await response.json() : await response.text();
  } catch {
    // a malformed or cut-off body; the status line below still names the failure
  }
  if (!response.ok || payload === null) {
    const error = new Error(payload?.error || (response.ok ? "Cairn sent a reply it couldn't read. Try again"
      : "something went wrong. Try again"));
    error.status = response.status;
    error.body = isJson ? payload : null;
    if (!quiet) toast(error.message, { tone: "error" });
    throw error;
  }
  return payload;
}
