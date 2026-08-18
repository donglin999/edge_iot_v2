/** Framework-neutral display classification for historical point values. */
export type SeriesKind = 'numeric' | 'categorical' | 'text';

export const MAX_CATEGORY_LEVELS = 8;

export interface RawRow {
  ts: number;
  label: string;
  value: unknown;
  quality: string;
}

export function toFiniteNumber(value: unknown): number {
  if (typeof value === 'number') return value;
  if (typeof value === 'boolean') return value ? 1 : 0;
  const parsed = Number.parseFloat(String(value));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

export function classifySeries(rows: RawRow[]): SeriesKind {
  if (rows.length === 0) return 'numeric';
  let numeric = 0;
  for (const row of rows) {
    if (Number.isFinite(toFiniteNumber(row.value))) numeric += 1;
  }
  if (numeric / rows.length >= 0.8) return 'numeric';
  const distinct = new Set(rows.map((row) => String(row.value)));
  return distinct.size <= MAX_CATEGORY_LEVELS ? 'categorical' : 'text';
}

export interface CategoricalPoint {
  ts: number;
  label: string;
  levelIndex: number;
  text: string;
}

export interface CategoricalSeries {
  levels: string[];
  points: CategoricalPoint[];
}

export function buildCategoricalSeries(rows: RawRow[]): CategoricalSeries {
  const levels: string[] = [];
  const indexes = new Map<string, number>();
  const points: CategoricalPoint[] = [];
  for (const row of rows) {
    const text = String(row.value);
    let levelIndex = indexes.get(text);
    if (levelIndex === undefined) {
      levelIndex = levels.length;
      levels.push(text);
      indexes.set(text, levelIndex);
    }
    points.push({ ts: row.ts, label: row.label, levelIndex, text });
  }
  return { levels, points };
}

export interface ChangeEvent {
  ts: number;
  label: string;
  value: string;
}

export function buildChangeEvents(rows: RawRow[], cap = 200): ChangeEvent[] {
  const events: ChangeEvent[] = [];
  let previous: string | null = null;
  for (const row of rows) {
    const value = String(row.value);
    if (value !== previous) {
      events.push({ ts: row.ts, label: row.label, value });
      previous = value;
    }
  }
  return events.reverse().slice(0, cap);
}
