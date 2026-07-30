import type {
  GovernancePatch,
  GovernanceTaskKind,
} from "@/lib/types";

export function parseGovernanceTags(value: string): string[] {
  const tags: string[] = [];
  const seen = new Set<string>();

  for (const part of value.split(/[,，;；\n]+/)) {
    const tag = part.trim().replace(/^#+/, "").trim();
    const key = tag.toLocaleLowerCase();
    if (!tag || seen.has(key)) continue;
    seen.add(key);
    tags.push(tag);
    if (tags.length === 30) break;
  }
  return tags;
}

export function governanceTagsError(tags: string[]): string | null {
  if (tags.some((tag) => tag.length > 64)) {
    return "单个标签不能超过 64 个字符";
  }
  return null;
}

export function governanceApplyPatch(
  kind: GovernanceTaskKind,
  input: {
    title: string;
    tags: string[];
    category: string;
  },
): GovernancePatch {
  if (kind === "deletion") return { state: "archived" };
  return {
    state: "ready",
    title: input.title.trim(),
    tags: input.tags,
    category: input.category.trim() || null,
  };
}
