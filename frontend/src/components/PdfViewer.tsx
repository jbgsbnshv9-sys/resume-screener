import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

export interface Highlight {
  page: number;
  bbox: [number, number, number, number]; // PDF points, top-left origin (as PyMuPDF reports them)
  kind: "evidence" | "hidden";
  active?: boolean;
  label?: string;
}

interface PageImage {
  width: number;
  height: number;
  scale: number;
  url: string;
}

const MAX_SCALE = 1.6;

export function PdfViewer({ data, highlights }: { data: ArrayBuffer; highlights: Highlight[] }) {
  const [pages, setPages] = useState<PageImage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const doc = await pdfjs.getDocument({ data: data.slice(0) }).promise;
        const out: PageImage[] = [];
        for (let i = 1; i <= doc.numPages; i++) {
          const page = await doc.getPage(i);
          // Fit the panel width; highlights use the same scale.
          const available = (container.current?.clientWidth ?? 800) - 4;
          const scale = Math.min(MAX_SCALE, available / page.getViewport({ scale: 1 }).width);
          const viewport = page.getViewport({ scale });
          const canvas = document.createElement("canvas");
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          await page.render({ canvas, viewport }).promise;
          out.push({ width: viewport.width, height: viewport.height, scale, url: canvas.toDataURL() });
        }
        if (!cancelled) setPages(out);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [data]);

  useEffect(() => {
    const el = container.current?.querySelector(".hl.active");
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlights]);

  if (error) return <div className="error">Could not render document: {error}</div>;
  return (
    <div className="pdf" ref={container}>
      {!pages.length && <div className="muted">Rendering document…</div>}
      {pages.map((p, i) => (
        <div key={i} className="pdf-page" style={{ width: p.width, height: p.height }}>
          <img src={p.url} width={p.width} height={p.height} alt={`Page ${i + 1}`} />
          {highlights
            .filter((h) => h.page === i)
            .map((h, j) => {
              const [x0, y0, x1, y1] = h.bbox;
              return (
                <div
                  key={j}
                  className={`hl ${h.kind}${h.active ? " active" : ""}`}
                  title={h.label}
                  style={{ left: x0 * p.scale, top: y0 * p.scale, width: Math.max(4, (x1 - x0) * p.scale), height: Math.max(4, (y1 - y0) * p.scale) }}
                />
              );
            })}
        </div>
      ))}
    </div>
  );
}
