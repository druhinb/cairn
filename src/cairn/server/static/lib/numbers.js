function rangeText({ min, max }) {
  if (min != null && max != null) return `Enter a whole number from ${min} to ${max}`;
  return min != null ? `Enter a whole number of at least ${min}` : `Enter a whole number of at most ${max}`;
}

/**
 * The whole number typed into a number field, or the reason the field refuses it.
 * @param {HTMLInputElement} input
 * @param {{min?: number, max?: number, nullable?: boolean}} spec  an empty nullable field reads as null
 */
export function readNumber(input, spec) {
  const raw = input.value.trim();
  if (input.validity.badInput) return { error: "Enter a number" };
  if (raw === "") return spec.nullable ? { value: null } : { error: "Enter a number" };
  const n = Number(raw);
  if (!Number.isInteger(n)) return { error: "Enter a whole number" };
  if ((spec.min != null && n < spec.min) || (spec.max != null && n > spec.max)) return { error: rangeText(spec) };
  return { value: n };
}
