const VALID_SKILL_SLUG = /^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$/;

function asciiSlug(value: string): string {
  return value
    .normalize("NFKD")
    .toLowerCase()
    .replace(/\.[a-z0-9]{1,8}$/i, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80)
    .replace(/-+$/g, "");
}

function stableHash(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36).padStart(7, "0").slice(0, 7);
}

export function normalizeSkillSlug(value: string, ...fallbacks: string[]): string {
  const direct = asciiSlug(value);
  if (VALID_SKILL_SLUG.test(direct)) return direct;

  for (const fallback of fallbacks) {
    const candidate = asciiSlug(fallback);
    if (VALID_SKILL_SLUG.test(candidate) && candidate !== "skill" && candidate !== "zip") return candidate;
  }

  const seed = [value, ...fallbacks].join("|") || "skill";
  return `skill-${stableHash(seed)}`;
}

export function isValidSkillSlug(value: string): boolean {
  return VALID_SKILL_SLUG.test(value);
}
