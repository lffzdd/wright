// Build output that is regenerated rather than reviewed line by line.
const generatedPatterns = [/(^|\/)static\/assets\//, /(^|\/)(dist|build)\//, /\.min\.(js|css)$/, /\.map$/];
export const isGenerated = (path: string) => generatedPatterns.some((pattern) => pattern.test(path));

/** Below this many files a flat list reads faster than directory headers. */
export const DIRECTORY_GROUP_MIN = 10;

/** One entry of a long list: a lone file keeps its full path, a directory of two or more gets a header. */
export type ListEntry<T> = { kind: "file"; row: T } | { kind: "dir"; dir: string; rows: T[] };

export const dirOf = (path: string) => {
  const index = path.lastIndexOf("/");
  return index < 0 ? "" : path.slice(0, index + 1);
};

const entryKey = <T extends { path: string }>(entry: ListEntry<T>) => entry.kind === "dir" ? entry.dir : entry.row.path;

function groupByDirectory<T extends { path: string }>(rows: T[]): ListEntry<T>[] {
  const byDir = new Map<string, T[]>();
  for (const row of rows) byDir.set(dirOf(row.path), [...(byDir.get(dirOf(row.path)) ?? []), row]);
  const entries = [...byDir].flatMap(([dir, items]): ListEntry<T>[] => dir && items.length >= 2
    ? [{ kind: "dir", dir, rows: items }]
    : items.map((row) => ({ kind: "file", row })));
  return entries.sort((a, b) => entryKey(a).localeCompare(entryKey(b)));
}

/**
 * Splits a group's rows into build output (folded when there are two or more) and the rest.
 * A long rest is grouped by directory; `entries` is empty when the flat `visible` list is used.
 */
export function layoutRows<T extends { path: string }>(rows: T[]) {
  const generated = rows.filter((row) => isGenerated(row.path));
  // A lone generated file is not worth a fold.
  const folded = generated.length >= 2 ? generated : [];
  const visible = rows.filter((row) => !folded.includes(row));
  const entries = visible.length >= DIRECTORY_GROUP_MIN ? groupByDirectory(visible) : [];
  const grouped = entries.some((entry) => entry.kind === "dir");
  return { folded, visible, entries: grouped ? entries : [] };
}
