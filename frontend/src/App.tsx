import { useEffect, useState } from "react";
import { api } from "./api";
import QueryExplorer from "./components/QueryExplorer";
import ReviewQueue from "./components/ReviewQueue";
import AliasManager from "./components/AliasManager";

type Tab = "query" | "review" | "aliases";

const TABS: { id: Tab; label: string }[] = [
  { id: "query", label: "Query explorer" },
  { id: "review", label: "Review queue" },
  { id: "aliases", label: "Alias manager" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("query");
  const [graph, setGraph] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    api
      .health()
      .then((h) => setGraph(h.graph))
      .catch(() => setOffline(true));
  }, []);

  return (
    <div className="app">
      <header>
        <h1>Graphify</h1>
        <span className={`status ${offline ? "bad" : "ok"}`}>
          {offline ? "API offline" : graph ? `graph: ${graph}` : "connecting…"}
        </span>
      </header>

      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={tab === t.id ? "active" : ""}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <main>
        {tab === "query" && <QueryExplorer />}
        {tab === "review" && <ReviewQueue />}
        {tab === "aliases" && <AliasManager />}
      </main>
    </div>
  );
}
