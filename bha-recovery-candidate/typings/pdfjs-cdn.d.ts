declare module "https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.min.mjs" {
  export const GlobalWorkerOptions: { workerSrc: string };
  export class PDFDataRangeTransport {
    constructor(length: number, initialData: Uint8Array, progressiveDone: boolean);
    length: number;
    onDataRange(begin: number, data: Uint8Array): void;
    abort(): void;
  }
  export function getDocument(options: {
    range: PDFDataRangeTransport;
    disableStream: boolean;
    disableAutoFetch: boolean;
    disableRange: boolean;
    rangeChunkSize: number;
    cMapUrl: string;
    cMapPacked: boolean;
    standardFontDataUrl: string;
    wasmUrl: string;
  }): {
    promise: Promise<{
      numPages: number;
      getPage(page: number): Promise<{
        getViewport(options: { scale: number }): { width: number; height: number; [key: string]: unknown };
        render(options: {
          canvasContext: CanvasRenderingContext2D;
          viewport: { width: number; height: number; [key: string]: unknown };
        }): { promise: Promise<void>; cancel(): void };
      }>;
    }>;
    destroy(): Promise<void>;
  };
}
