#!/usr/bin/env python3
"""
LOCI Task Tracker
=================
Tracks the full execution graph of Claude Code engineering sessions.
Builds a dependency-aware task tree that LOCI can analyze for:
- Execution path optimization
- Regression detection between sessions
- Power/performance impact estimation

Run as a standalone query tool:
  python3 task_tracker.py --state-dir ./state --status
  python3 task_tracker.py --state-dir ./state --graph
  python3 task_tracker.py --state-dir ./state --diff <session1> <session2>
"""

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import argparse

# Force UTF-8 for all Python I/O and any child Python process we spawn.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


@dataclass
class TaskNode:
    """A node in the execution graph."""
    id: str
    action_type: str
    tool_name: str
    timestamp: str
    files: list = field(default_factory=list)
    parent_id: Optional[str] = None
    children: list = field(default_factory=list)
    status: str = "pending"  # pending, running, completed, failed
    duration_ms: Optional[float] = None
    loci_insights: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class ExecutionGraph:
    """Full execution graph for a session."""
    session_id: str
    root_nodes: list = field(default_factory=list)
    all_nodes: dict = field(default_factory=dict)
    file_timeline: dict = field(default_factory=dict)  # file -> [action_ids]
    action_sequence: list = field(default_factory=list)

    def add_action(self, action: dict) -> TaskNode:
        """Add an engineering action to the graph."""
        node_id = f"{action['timestamp']}_{action['tool_name']}"
        node = TaskNode(
            id=node_id,
            action_type=action.get("action_type", "unknown"),
            tool_name=action.get("tool_name", ""),
            timestamp=action.get("timestamp", ""),
            files=action.get("files_involved", []),
            metadata={
                "event": action.get("event", ""),
                "cwd": action.get("cwd", ""),
            },
        )

        # Link to parent based on file dependencies
        for file in node.files:
            if file in self.file_timeline:
                parent_id = self.file_timeline[file][-1]
                if parent_id in self.all_nodes:
                    node.parent_id = parent_id
                    self.all_nodes[parent_id].children.append(node_id)
                    break

        if not node.parent_id:
            self.root_nodes.append(node_id)

        # Update file timeline
        for file in node.files:
            if file not in self.file_timeline:
                self.file_timeline[file] = []
            self.file_timeline[file].append(node_id)

        self.all_nodes[node_id] = node
        self.action_sequence.append(node_id)
        return node

    def get_execution_path(self, file: str) -> list[TaskNode]:
        """Get the full execution path for a specific file."""
        if file not in self.file_timeline:
            return []
        return [self.all_nodes[nid] for nid in self.file_timeline[file] if nid in self.all_nodes]

    def get_hot_files(self, threshold: int = 3) -> list[tuple[str, int]]:
        """Files that were touched most frequently (likely hot paths)."""
        return sorted(
            [(f, len(ids)) for f, ids in self.file_timeline.items()],
            key=lambda x: x[1],
            reverse=True,
        )[:20]

    def to_loci_format(self) -> dict:
        """Export execution graph in LOCI-compatible format."""
        return {
            "session_id": self.session_id,
            "graph": {
                "nodes": {
                    nid: {
                        "type": n.action_type,
                        "tool": n.tool_name,
                        "files": n.files,
                        "parent": n.parent_id,
                        "children": n.children,
                        "insights": n.loci_insights,
                    }
                    for nid, n in self.all_nodes.items()
                },
                "roots": self.root_nodes,
                "sequence": self.action_sequence,
            },
            "file_timeline": self.file_timeline,
            "hot_files": self.get_hot_files(),
            "stats": {
                "total_actions": len(self.all_nodes),
                "unique_files": len(self.file_timeline),
                "max_depth": self._max_depth(),
            },
        }

    def _max_depth(self) -> int:
        """Calculate maximum depth of execution graph."""
        def depth(nid, visited=None):
            if visited is None:
                visited = set()
            if nid in visited or nid not in self.all_nodes:
                return 0
            visited.add(nid)
            node = self.all_nodes[nid]
            if not node.children:
                return 1
            return 1 + max(depth(c, visited) for c in node.children)

        if not self.root_nodes:
            return 0
        return max(depth(r) for r in self.root_nodes)

    def print_tree(self, indent: int = 0) -> str:
        """Pretty-print the execution graph."""
        lines = []

        def walk(nid, level):
            if nid not in self.all_nodes:
                return
            node = self.all_nodes[nid]
            prefix = "  " * level + ("|- " if level > 0 else "")
            files_str = ", ".join(Path(f).name for f in node.files[:3])
            if len(node.files) > 3:
                files_str += f" +{len(node.files) - 3}"

            icon = {
                "code_modification": "[M]",
                "code_analysis": "[R]",
                "build_command": "[B]",
                "test_execution": "[T]",
                "shell_command": "[S]",
                "agent_delegation": "[A]",
                "config_modification": "[C]",
                "deployment": "[D]",
            }.get(node.action_type, "[?]")

            lines.append(f"{prefix}{icon} {node.tool_name}: {files_str or node.action_type}")

            for cid in node.children:
                walk(cid, level + 1)

        for root in self.root_nodes:
            walk(root, 0)

        return "\n".join(lines)


class SessionDiffer:
    """Compare two session execution graphs for regression detection."""

    @staticmethod
    def diff(graph1: ExecutionGraph, graph2: ExecutionGraph) -> dict:
        files1 = set(graph1.file_timeline.keys())
        files2 = set(graph2.file_timeline.keys())

        return {
            "new_files": list(files2 - files1),
            "removed_files": list(files1 - files2),
            "common_files": list(files1 & files2),
            "action_count_delta": len(graph2.all_nodes) - len(graph1.all_nodes),
            "hot_files_change": {
                "before": graph1.get_hot_files(5),
                "after": graph2.get_hot_files(5),
            },
        }


def load_graph_from_log(state_dir: Path, session_id: Optional[str] = None) -> ExecutionGraph:
    """Build execution graph from action log."""
    log_file = state_dir / "loci-actions.log"
    graph = ExecutionGraph(session_id=session_id or "unknown")

    if not log_file.exists():
        return graph

    with open(log_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                action = json.loads(line)
                if session_id and action.get("session_id") != session_id:
                    continue
                graph.add_action(action)
            except json.JSONDecodeError:
                continue

    return graph


def main():
    parser = argparse.ArgumentParser(description="LOCI Task Tracker")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("LOCI_STATE_DIR") or str(Path.cwd() / ".loci" / "state"),
        help="State directory (defaults to $LOCI_STATE_DIR or <cwd>/.loci/state)",
    )
    parser.add_argument("--session", default=None, help="Filter by session ID")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--graph", action="store_true", help="Print execution graph tree")
    parser.add_argument("--export", action="store_true", help="Export LOCI-format JSON")
    parser.add_argument("--hot-files", action="store_true", help="Show hot files")
    parser.add_argument("--diff", nargs=2, metavar="SESSION", help="Diff two sessions")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)

    if args.diff:
        g1 = load_graph_from_log(state_dir, args.diff[0])
        g2 = load_graph_from_log(state_dir, args.diff[1])
        result = SessionDiffer.diff(g1, g2)
        print(json.dumps(result, indent=2))
        return

    graph = load_graph_from_log(state_dir, args.session)

    if args.status:
        print(f"Session: {graph.session_id}")
        print(f"Total actions: {len(graph.all_nodes)}")
        print(f"Unique files: {len(graph.file_timeline)}")
        print(f"Max depth: {graph._max_depth()}")
        print(f"Root tasks: {len(graph.root_nodes)}")
        return

    if args.graph:
        tree = graph.print_tree()
        print(tree if tree else "(no actions recorded)")
        return

    if args.export:
        print(json.dumps(graph.to_loci_format(), indent=2, default=str))
        return

    if args.hot_files:
        for file, count in graph.get_hot_files():
            print(f"  {count:3d}x  {file}")
        return

    # Default: show brief status
    print(f"Actions: {len(graph.all_nodes)} | Files: {len(graph.file_timeline)} | Depth: {graph._max_depth()}")


if __name__ == "__main__":
    main()
