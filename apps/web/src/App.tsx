import { useUiTelemetry } from "./lib/queries";
import { useCanvases } from "./genui/canvases";
import Sidebar from "./components/Sidebar";
import TopBar from "./components/TopBar";
import BreakingTicker from "./components/BreakingTicker";
import Canvas from "./genui/Canvas";
import SavedCanvasView from "./genui/SavedCanvasView";

// A read-only shared canvas is opened via ?shared=<token> (M8): the whole app
// becomes the shared view — no composer, no sidebar — so a recipient sees only
// the canvas and cannot edit it.
function sharedToken(): string | null {
  try {
    return new URLSearchParams(window.location.search).get("shared");
  } catch {
    return null;
  }
}

function SharedApp({ token }: { token: string }) {
  return (
    <div className="flex h-screen w-full flex-col overflow-hidden bg-background text-foreground">
      <div className="flex items-center gap-3 border-b px-5 py-3">
        <div className="glow-text font-grotesk text-base font-bold tracking-tight">Noesis</div>
        <span className="font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60">
          SHARED CANVAS
        </span>
      </div>
      <main className="min-h-0 flex-1">
        <SavedCanvasView sharedToken={token} />
      </main>
    </div>
  );
}

// The app is a single generative surface: nothing is rendered until an
// intent is submitted through the composer (or a sidebar suggestion) — the
// startup screen is intentionally empty except for the prompt.
export default function App() {
  const manager = useCanvases();
  const { data: telemetry } = useUiTelemetry();
  const hasIntent = manager.active.intent.trim().length > 0;

  const token = sharedToken();
  if (token) return <SharedApp token={token} />;

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      <Sidebar
        canvases={manager.canvases}
        activeId={manager.active.id}
        onSelect={manager.setActive}
        onOpenSaved={manager.openSaved}
        onRemove={manager.remove}
        ingestRate="64"
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onIntent={manager.open} />
        {/* Pack-provided signal strip (BREAKING for news, NEW IN LIBRARY
            for the corpus fallback) — absent when no pack supplies one. */}
        {hasIntent && telemetry.ticker ? (
          <BreakingTicker label={telemetry.ticker.label} text={telemetry.ticker.text} />
        ) : null}
        <main className="min-h-0 flex-1">
          <Canvas key={manager.active.id} canvas={manager.active} onIntent={manager.open} />
        </main>
      </div>
    </div>
  );
}
