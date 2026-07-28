/**
 * 测点历史数据的展示形态判定与预处理(纯函数,便于单测)。
 *
 * 字符串测点画不了折线 —— 按数据实际形态自动选展示:
 * - numeric:≥80% 可解析为有限数 → 常规折线(既有行为,完全兼容);
 * - categorical:离散取值 ≤ MAX_CATEGORY_LEVELS → 阶梯状态图(Y 轴即状态文本);
 * - text:离散取值更多的自由文本 → 变化记录列表(连续相同值去重)。
 */

export type SeriesKind = 'numeric' | 'categorical' | 'text';

/** 阶梯状态图最多容纳的离散状态数;超过则退化为变化记录列表。 */
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
  const n = parseFloat(String(value));
  return Number.isFinite(n) ? n : NaN;
}

/** 判定序列展示形态。空序列按 numeric(渲染空态,行为同旧)。 */
export function classifySeries(rows: RawRow[]): SeriesKind {
  if (rows.length === 0) return 'numeric';
  let numeric = 0;
  const distinct = new Set<string>();
  for (const r of rows) {
    if (Number.isFinite(toFiniteNumber(r.value))) numeric += 1;
    else distinct.add(String(r.value));
  }
  if (numeric / rows.length >= 0.8) return 'numeric';
  // 非数值为主:全部值(含数值串)一起数离散度
  const allDistinct = new Set(rows.map((r) => String(r.value)));
  return allDistinct.size <= MAX_CATEGORY_LEVELS ? 'categorical' : 'text';
}

export interface CategoricalPoint {
  ts: number;
  label: string;
  /** 状态在 levels 里的序号(画阶梯用)。 */
  levelIndex: number;
  /** 原始状态文本(tooltip 用)。 */
  text: string;
}

export interface CategoricalSeries {
  /** 出现过的状态,按首次出现顺序。 */
  levels: string[];
  points: CategoricalPoint[];
}

export function buildCategoricalSeries(rows: RawRow[]): CategoricalSeries {
  const levels: string[] = [];
  const index = new Map<string, number>();
  const points: CategoricalPoint[] = [];
  for (const r of rows) {
    const text = String(r.value);
    let li = index.get(text);
    if (li === undefined) {
      li = levels.length;
      levels.push(text);
      index.set(text, li);
    }
    points.push({ ts: r.ts, label: r.label, levelIndex: li, text });
  }
  return { levels, points };
}

export interface ChangeEvent {
  ts: number;
  label: string;
  value: string;
}

/** 连续相同值去重成「变化事件」,新的在前;cap 限制返回条数。 */
export function buildChangeEvents(rows: RawRow[], cap = 200): ChangeEvent[] {
  const events: ChangeEvent[] = [];
  let prev: string | null = null;
  for (const r of rows) {
    const v = String(r.value);
    if (v !== prev) {
      events.push({ ts: r.ts, label: r.label, value: v });
      prev = v;
    }
  }
  return events.reverse().slice(0, cap);
}
