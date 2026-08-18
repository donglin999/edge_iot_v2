import {
  buildCategoricalSeries,
  buildChangeEvents,
  classifySeries,
  type RawRow,
} from './pointChartDisplay';

const row = (value: unknown, ts = 0): RawRow => ({
  ts,
  label: `t${ts}`,
  value,
  quality: 'good',
});

describe('classifySeries', () => {
  it('keeps numeric and numeric-looking values on a line series', () => {
    expect(classifySeries([row(1), row(2.5), row('3.14')])).toBe('numeric');
  });

  it('uses a categorical series for a small set of text states', () => {
    expect(classifySeries([row('运行'), row('停机'), row('运行')])).toBe('categorical');
  });

  it('uses a change list for high-cardinality text', () => {
    const rows = Array.from({ length: 20 }, (_, index) => row(`报警码-${index}`, index));
    expect(classifySeries(rows)).toBe('text');
  });

  it('tolerates up to twenty percent invalid values in a numeric series', () => {
    expect(classifySeries([row(1), row(2), row(3), row(4), row('N/A')])).toBe('numeric');
  });

  it('keeps an empty series on the existing numeric empty state', () => {
    expect(classifySeries([])).toBe('numeric');
  });
});

describe('buildCategoricalSeries', () => {
  it('numbers states by first appearance and preserves their labels', () => {
    const series = buildCategoricalSeries([row('停机', 1), row('运行', 2), row('停机', 3)]);
    expect(series.levels).toEqual(['停机', '运行']);
    expect(series.points.map((point) => point.levelIndex)).toEqual([0, 1, 0]);
    expect(series.points[1]?.text).toBe('运行');
  });
});

describe('buildChangeEvents', () => {
  it('deduplicates adjacent values and returns newest changes first', () => {
    const events = buildChangeEvents([
      row('A', 1),
      row('A', 2),
      row('B', 3),
      row('B', 4),
      row('C', 5),
    ]);
    expect(events.map((event) => event.value)).toEqual(['C', 'B', 'A']);
  });

  it('applies the requested result cap', () => {
    const rows = Array.from({ length: 300 }, (_, index) => row(`v${index}`, index));
    expect(buildChangeEvents(rows, 200)).toHaveLength(200);
  });
});
