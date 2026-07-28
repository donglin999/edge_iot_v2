import { describe, expect, it } from 'vitest';

import {
  buildCategoricalSeries,
  buildChangeEvents,
  classifySeries,
  type RawRow,
} from './pointChartDisplay';

const row = (value: unknown, ts = 0): RawRow => ({
  ts, label: `t${ts}`, value, quality: 'good',
});

describe('classifySeries', () => {
  it('数值为主 → numeric(既有折线,兼容不变)', () => {
    expect(classifySeries([row(1), row(2.5), row('3.14')])).toBe('numeric');
  });
  it('少量离散字符串 → categorical', () => {
    expect(classifySeries([row('运行'), row('停机'), row('运行')])).toBe('categorical');
  });
  it('高离散自由文本 → text', () => {
    const rows = Array.from({ length: 20 }, (_, i) => row(`报警码-${i}`, i));
    expect(classifySeries(rows)).toBe('text');
  });
  it('混入两成以内坏值仍按 numeric', () => {
    const rows = [row(1), row(2), row(3), row(4), row('N/A')];
    expect(classifySeries(rows)).toBe('numeric');
  });
  it('空序列按 numeric(走空态)', () => {
    expect(classifySeries([])).toBe('numeric');
  });
});

describe('buildCategoricalSeries', () => {
  it('状态按首次出现顺序编号,点带原文', () => {
    const s = buildCategoricalSeries([row('停机', 1), row('运行', 2), row('停机', 3)]);
    expect(s.levels).toEqual(['停机', '运行']);
    expect(s.points.map((p) => p.levelIndex)).toEqual([0, 1, 0]);
    expect(s.points[1].text).toBe('运行');
  });
});

describe('buildChangeEvents', () => {
  it('连续相同值合并,新变化在前', () => {
    const events = buildChangeEvents([
      row('A', 1), row('A', 2), row('B', 3), row('B', 4), row('C', 5),
    ]);
    expect(events.map((e) => e.value)).toEqual(['C', 'B', 'A']);
  });
  it('cap 生效', () => {
    const rows = Array.from({ length: 300 }, (_, i) => row(`v${i}`, i));
    expect(buildChangeEvents(rows, 200)).toHaveLength(200);
  });
});
