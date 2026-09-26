import { toast } from "../components/toast.js";

/**
 * Copy text to the clipboard and say so in a toast.
 * @param {string} text @param {string} noun what was copied, e.g. "the posting id"
 */
export async function copyText(text, noun) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`Copied ${noun}`);
  } catch {
    toast(`Couldn't copy. Select ${noun} and copy it yourself`, { tone: "error" });
  }
}
