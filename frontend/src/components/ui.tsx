import type { ReactNode } from "react";

export function TierBadge({ tier, hidden }: { tier: string | null; hidden?: boolean }) {
  if (hidden) return <span className="badge muted-badge">hidden</span>;
  if (!tier) return <span className="badge muted-badge">no tier</span>;
  return <span className={`badge tier-${tier}`}>{tier}</span>;
}

export function FlagChips({ flags }: { flags: string[] }) {
  if (!flags.length) return null;
  return (
    <span className="chips">
      {flags.map((f) => (
        <span key={f} className="chip">{f.replaceAll("_", " ")}</span>
      ))}
    </span>
  );
}

export function Panel({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="panel">
      <header>
        <h2>{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function fmt(x: number | null | undefined, digits = 3): string {
  return x === null || x === undefined ? "n/a" : x.toFixed(digits);
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
