export interface SourceFileLike {
  path: string;
}

export type SourceTreeRow<T extends SourceFileLike = SourceFileLike> =
  | { kind: 'directory'; path: string; label: string; depth: number }
  | { kind: 'file'; path: string; label: string; depth: number; file: T };

export function buildSourceFileTree<T extends SourceFileLike>(files: readonly T[]): SourceTreeRow<T>[] {
  const rows: SourceTreeRow<T>[] = [];
  const directories = new Set<string>();

  for (const file of [...files].sort((left, right) => left.path.localeCompare(right.path))) {
    const parts = file.path.split('/');
    for (let index = 0; index < parts.length - 1; index += 1) {
      const path = parts.slice(0, index + 1).join('/');
      if (directories.has(path)) continue;
      directories.add(path);
      rows.push({ kind: 'directory', path, label: parts[index], depth: index });
    }
    rows.push({
      kind: 'file',
      path: file.path,
      label: parts.at(-1) ?? file.path,
      depth: Math.max(0, parts.length - 1),
      file
    });
  }

  return rows;
}

export function visibleSourceTreeRows<T extends SourceFileLike>(
  rows: readonly SourceTreeRow<T>[],
  collapsedDirectories: ReadonlySet<string>
): SourceTreeRow<T>[] {
  if (collapsedDirectories.size === 0) return [...rows];

  return rows.filter((row) => {
    const parts = row.path.split('/');
    const parentCount = Math.max(0, parts.length - 1);
    for (let length = 1; length <= parentCount; length += 1) {
      if (collapsedDirectories.has(parts.slice(0, length).join('/'))) return false;
    }
    return true;
  });
}
