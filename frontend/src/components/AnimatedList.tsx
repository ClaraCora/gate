import type { ReactNode } from "react";

interface AnimatedListProps<T> {
  items: T[];
  itemKey: (item: T) => string | number;
  renderItem: (item: T) => ReactNode;
  className?: string;
  empty?: ReactNode;
}

export function AnimatedList<T>({
  items,
  itemKey,
  renderItem,
  className = "",
  empty = null,
}: AnimatedListProps<T>) {
  if (items.length === 0) return <>{empty}</>;
  return (
    <div className={`animated-list ${className}`.trim()}>
      {items.map((item, index) => (
        <div
          className="animated-list__item"
          key={itemKey(item)}
          style={{ animationDelay: `${Math.min(index * 24, 120)}ms` }}
        >
          {renderItem(item)}
        </div>
      ))}
    </div>
  );
}
