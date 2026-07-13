/**
 * Virtualized grid for the realtime point-value cards.
 *
 * The plain AntD `<Row>` renders one DOM subtree per matching point, which
 * collapses the page once a filter matches thousands of points. This
 * component uses `react-window`'s `FixedSizeGrid` so only the visible cards
 * are mounted, keeping scroll/refresh smooth regardless of point count.
 *
 * Column count is derived from the measured container width so the layout
 * still adapts responsively (like the original AntD grid breakpoints).
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { FixedSizeGrid, GridChildComponentProps } from 'react-window';
import type { PointLatestValue } from '../services/dataApi';

interface VirtualPointGridProps {
  points: PointLatestValue[];
  renderCard: (point: PointLatestValue) => React.ReactNode;
  /** Minimum width (px) of a single card column. */
  minColumnWidth?: number;
  /** Height (px) of a single card row. */
  rowHeight?: number;
  /** Height (px) of the scrollable viewport. */
  height?: number;
  /** Gap (px) between cards. */
  gap?: number;
}

// Per-cell data passed via react-window's `itemData`. Threading everything the
// cell needs through here keeps the `Cell` component identity stable across
// parent renders, so react-window doesn't remount every visible cell each time
// the parent re-renders.
interface CellData {
  points: PointLatestValue[];
  columnCount: number;
  gap: number;
  renderCard: (point: PointLatestValue) => React.ReactNode;
}

// Hoisted out of the parent render so its component identity never changes.
const Cell: React.FC<GridChildComponentProps<CellData>> = ({
  columnIndex,
  rowIndex,
  style,
  data,
}) => {
  const { points, columnCount, gap, renderCard } = data;
  const index = rowIndex * columnCount + columnIndex;
  if (index >= points.length) {
    return <div style={style} />;
  }
  return (
    <div
      style={{
        ...style,
        left: Number(style.left) + gap / 2,
        top: Number(style.top) + gap / 2,
        width: Number(style.width) - gap,
        height: Number(style.height) - gap,
      }}
    >
      {renderCard(points[index])}
    </div>
  );
};

const VirtualPointGrid: React.FC<VirtualPointGridProps> = ({
  points,
  renderCard,
  minColumnWidth = 260,
  rowHeight = 152,
  height = 620,
  gap = 12,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  // Track the container width so the column count stays responsive.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => setWidth(el.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const columnCount = Math.max(1, Math.floor(width / minColumnWidth)) || 1;
  const columnWidth = width > 0 ? width / columnCount : minColumnWidth;
  const rowCount = Math.ceil(points.length / columnCount);

  const itemData = useMemo<CellData>(
    () => ({ points, columnCount, gap, renderCard }),
    [points, columnCount, gap, renderCard],
  );

  return (
    <div ref={containerRef} style={{ width: '100%' }}>
      {width > 0 && (
        <FixedSizeGrid<CellData>
          columnCount={columnCount}
          columnWidth={columnWidth}
          rowCount={rowCount}
          rowHeight={rowHeight}
          height={Math.min(height, rowCount * rowHeight)}
          width={width}
          itemData={itemData}
        >
          {Cell}
        </FixedSizeGrid>
      )}
    </div>
  );
};

export default VirtualPointGrid;
