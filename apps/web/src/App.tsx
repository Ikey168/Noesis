import { useTicker } from "./lib/queries";
import { useCanvases } from "./genui/canvases";
import Sidebar from "./components/Sidebar";
import TopBar from "./components/TopBar";
import BreakingTicker from "./components/BreakingTicker";
import Canvas from "./genui/Canvas";

// The app is a single generative surface: nothing is rendered until an
// intent is submitted through the composer (or a sidebar suggestion) — the
// startup screen is intentionally empty except for the prompt.
export default function App() {
  const manager = useCanvases();
  const { data: ticker } = useTicker();
  const hasIntent = manager.active.intent.trim().length > 0;

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      <Sidebar
        canvases={manager.canvases}
        activeId={manager.active.id}
        onSelect={manager.setActive}
        onRemove={manager.remove}
        ingestRate="64"
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onIntent={manager.open} />
        {hasIntent ? <BreakingTicker text={ticker} /> : null}
        <main className="min-h-0 flex-1">
          <Canvas key={manager.active.id} canvas={manager.active} onIntent={manager.open} />
        </main>
      </div>
    </div>
  );
}
