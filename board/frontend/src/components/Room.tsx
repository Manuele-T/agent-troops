import type { AgentView } from "../fleet";
import Character from "./Character";

// Character is a single shared PixiJS canvas for the whole room (never one per agent),
// so it takes the full agents list.
export default function Room({
  agents,
  selectedId,
  onSelect,
}: {
  agents: AgentView[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return <Character agents={agents} selectedId={selectedId} onSelect={onSelect} />;
}
