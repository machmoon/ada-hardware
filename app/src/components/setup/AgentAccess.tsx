import { Header } from "@/components/Header";
import { CommandLine } from "@/components/CommandLine";

/** The same engine, from a terminal or from another agent over MCP. */
const MCP_JSON = JSON.stringify(
  { mcpServers: { ada: { command: ".venv/bin/silkscreen-mcp", args: [] } } },
  null,
  0
);

export const AgentAccess = ({ className }: { className?: string }) => (
  <div id="agent-access" className={`space-y-3 ${className ?? ""}`} data-testid="agent-access">
    <Header
      title="Use Ada elsewhere"
      description="The same engine from a terminal or another agent. Run these from the Ada checkout."
      isMainTitle
    />
    <div className="space-y-1">
      <p className="text-xs font-medium">Terminal</p>
      <CommandLine command={'silkscreen "a 3.3 V LDO board" -o out/board.kicad_pcb'} />
    </div>
    <div className="space-y-1">
      <p className="text-xs font-medium">Claude Code</p>
      <CommandLine command="claude mcp add ada -- .venv/bin/silkscreen-mcp" />
    </div>
    <div className="space-y-1">
      <p className="text-xs font-medium">Any MCP client</p>
      <CommandLine command={MCP_JSON} />
      <p className="text-[11px] text-muted-foreground">
        Tools: generate_board, validate_circuit, build_board, simulate_circuit.
      </p>
    </div>
  </div>
);
