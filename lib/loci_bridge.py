#!/usr/bin/env python3
"""
LOCI API Bridge - C++ Execution Context Aggregator
====================================================
Works alongside the LOCI HTTP API (https://mcp.auroralabs.com/mcp/v1).

Tailored for C++ engineering workflows:
- Tracks compilation commands, flags, optimization levels
- Monitors binary artifacts (.o, .so, executables)
- Detects source-to-binary relationships
- Provides C++ specific heuristic warnings (memory, perf, UB)
- Aggregates context for LOCI binary-level analysis tools
"""

import asyncio
import json
import logging
import os
import signal
import sys
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import hashlib
import argparse

# Force UTF-8 for all Python I/O and any child Python process we spawn.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BridgeConfig:
    mcp_server_url: str = "https://mcp.auroralabs.com/mcp/v1"
    mcp_server_name: str = "loci"
    poll_interval: float = 2.0
    batch_size: int = 10
    analysis_timeout: float = 30.0
    enabled: bool = True

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "BridgeConfig":
        cfg = cls()
        if config_path and config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
                for key, value in data.items():
                    if hasattr(cfg, key) and not key.startswith("_"):
                        setattr(cfg, key, value)
        return cfg


# ---------------------------------------------------------------------------
# Action Models
# ---------------------------------------------------------------------------

@dataclass
class EngineeringAction:
    event: str
    session_id: str
    tool_name: str
    action_type: str
    cwd: str
    timestamp: str
    tool_input: dict = field(default_factory=dict)
    tool_response: Optional[dict] = None
    files_involved: list = field(default_factory=list)
    cpp_context: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> "EngineeringAction":
        return cls(
            event=data.get("event", ""),
            session_id=data.get("session_id", ""),
            tool_name=data.get("tool_name", ""),
            action_type=data.get("action_type", "unknown"),
            cwd=data.get("cwd", ""),
            timestamp=data.get("timestamp", ""),
            tool_input=data.get("tool_input", {}),
            tool_response=data.get("tool_response"),
            files_involved=data.get("files_involved", []),
            cpp_context=data.get("cpp_context", {}),
        )

    def to_loci_context(self) -> dict:
        ctx = {
            "source": "claude-code",
            "session_id": self.session_id,
            "action_type": self.action_type,
            "tool": self.tool_name,
            "timestamp": self.timestamp,
            "files": self.files_involved,
            "cpp_context": self.cpp_context,
        }
        if self.tool_name == "Bash":
            ctx["command"] = self.tool_input.get("command", "")
            ctx["description"] = self.tool_input.get("description", "")
        elif self.tool_name in ("Write", "Edit"):
            ctx["file_path"] = self.tool_input.get("file_path", "")
            ctx["change_type"] = "edit" if self.tool_name == "Edit" else "write"
            content = self.tool_input.get("content", "") or self.tool_input.get("new_string", "")
            ctx["content_lines"] = content.count("\n") + 1
            ctx["content_hash"] = hashlib.sha256(content.encode()).hexdigest()[:16]
        return ctx


@dataclass
class LociInsight:
    file: str
    severity: str
    category: str
    message: str
    active: bool = True
    details: dict = field(default_factory=dict)
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# C++ Local Heuristic Analyzer
# ---------------------------------------------------------------------------

class CppAnalyzer:
    """C++ specific heuristic analysis — supplements LOCI API results."""

    PERF_PATTERNS = [
        (r'\bvirtual\b.*\b(update|tick|render|process|compute|calculate)\b',
         "warning", "performance",
         "Virtual dispatch in likely hot-path function — LOCI can verify branch prediction impact on binary"),

        (r'(for|while)\s*\([^)]*\)\s*\{[^}]*(new\s|malloc|alloc|push_back|emplace_back)',
         "warning", "memory",
         "Heap allocation inside loop — may cause cache thrashing and memory fragmentation"),

        (r'std::endl',
         "info", "performance",
         "std::endl flushes buffer each time — use '\\n' for better I/O throughput"),

        (r'(for|while)\s*\([^)]*\)\s*\{[^}]*(throw|try|catch)',
         "warning", "performance",
         "Exception handling in loop — exception tables add binary size and branch overhead"),
    ]

    COMPILE_WARNINGS = {
        # "missing_optimization": {
        #     "check": lambda flags: not any(f.startswith("-O") for f in flags),
        #     "severity": "warning",
        #     "category": "performance",
        #     "message": "No optimization flag — binary analysis requires -O2/-O3 for meaningful results",
        # },
        # "debug_in_perf": {
        #     "check": lambda flags: "-g" in flags and "-O0" in flags,
        #     "severity": "info",
        #     "category": "performance",
        #     "message": "Debug build (-g -O0) — performance analysis needs release flags (-O2/-O3)",
        # },
        # "no_march": {
        #     "check": lambda flags: not any(f.startswith("-march") for f in flags),
        #     "severity": "info",
        #     "category": "optimization",
        #     "message": "No -march flag — binary won't use CPU-specific instructions (AVX/SSE)",
        # },
    }

    @classmethod
    def analyze(cls, actions: list[EngineeringAction]) -> list[LociInsight]:
        insights = []
        now = datetime.now(timezone.utc).isoformat()

        for action in actions:
            if action.action_type in ("cpp_source_modification", "assembly_modification"):
                content = action.tool_input.get("content", "") or action.tool_input.get("new_string", "")
                file_path = action.tool_input.get("file_path", "")

                for pattern, severity, category, message in cls.PERF_PATTERNS:
                    if re.search(pattern, content, re.MULTILINE | re.DOTALL):
                        insights.append(LociInsight(
                            file=file_path, severity=severity, category=category,
                            message=message, timestamp=now))

                if re.search(r'reinterpret_cast|const_cast.*mutable', content):
                    insights.append(LociInsight(
                        file=file_path, severity="warning", category="safety",
                        message="Unsafe cast detected — verify in binary with LOCI", timestamp=now))

                array_match = re.findall(r'\b\w+\s+\w+\[(\d+)\]', content)
                for size in array_match:
                    if int(size) > 10000:
                        insights.append(LociInsight(
                            file=file_path, severity="warning", category="memory",
                            message=f"Large stack array [{size}] — risk of stack overflow", timestamp=now))

                if len(content) > 8000:
                    insights.append(LociInsight(
                        file=file_path, severity="info", category="complexity",
                        message=f"Large source file ({len(content)} chars) — consider splitting", timestamp=now))

            if action.action_type in ("cpp_compile", "cpp_build", "cpp_link"):
                flags = action.cpp_context.get("compiler_flags", [])
                for name, check in cls.COMPILE_WARNINGS.items():
                    if check["check"](flags):
                        insights.append(LociInsight(
                            file=action.cpp_context.get("output_binary", ""),
                            severity=check["severity"], category=check["category"],
                            message=check["message"], timestamp=now))
                    else:
                        # Flag is present — resolve any prior warning of this type
                        insights.append(LociInsight(
                            file=action.cpp_context.get("output_binary", ""),
                            severity=check["severity"], category=check["category"],
                            message=check["message"], timestamp=now, active=False))

            if action.action_type == "shell_command":
                cmd = action.tool_input.get("command", "")
                if "rm -rf" in cmd:
                    insights.append(LociInsight(
                        file="", severity="critical", category="safety",
                        message=f"Recursive delete: {cmd[:80]}", timestamp=now))

            if action.action_type == "binary_execution":
                cmd = action.tool_input.get("command", "")
                insights.append(LociInsight(
                    file=cmd.split()[0] if cmd else "",
                    severity="info", category="execution",
                    message=f"Binary executed: {cmd[:80]} — use LOCI for execution-path profiling",
                    timestamp=now))

        return insights


# ---------------------------------------------------------------------------
# Bridge Server
# ---------------------------------------------------------------------------

class LociBridge:
    def __init__(self, state_dir: Path, session_id: str, config: BridgeConfig):
        self.state_dir = state_dir
        self.session_id = session_id
        self.config = config
        self.logger = logging.getLogger("loci-bridge")
        self.queue_dir = state_dir / "queue"
        self.context_file = state_dir / "loci-context.json"
        self.warnings_file = state_dir / "loci-warnings.json"
        self.metrics_file = state_dir / "loci-metrics.json"
        self._running = True
        self._process_signal = asyncio.Event()

        self.session_context = {
            "session_id": session_id,
            "mcp_server": config.mcp_server_url,
            "environment": "cpp",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "actions": [],
            "file_timeline": {},
            "action_counts": {},
            "total_actions": 0,
            "binaries_produced": [],
            "compilation_history": [],
            "source_files_modified": [],
        }

        self.metrics = {
            "actions_processed": 0,
            "insights_generated": 0,
            "warnings_active": 0,
            "compilations_tracked": 0,
            "binaries_tracked": 0,
            "session_start": datetime.now(timezone.utc).isoformat(),
            "last_analysis": None,
        }

    async def run(self):
        self.logger.info(f"LOCI C++ Bridge started for session {self.session_id[:8]}")
        self.logger.info(f"LOCI API endpoint: {self.config.mcp_server_url}")
        self._write_warnings([])
        self._write_context()

        while self._running:
            try:
                try:
                    await asyncio.wait_for(self._process_signal.wait(), timeout=self.config.poll_interval)
                    self._process_signal.clear()
                except asyncio.TimeoutError:
                    pass

                actions = self._read_queue()
                if actions:
                    for action in actions:
                        loci_ctx = action.to_loci_context()
                        self.session_context["actions"].append(loci_ctx)

                        for f in action.files_involved:
                            if f not in self.session_context["file_timeline"]:
                                self.session_context["file_timeline"][f] = []
                            self.session_context["file_timeline"][f].append({
                                "action": action.action_type,
                                "tool": action.tool_name,
                                "timestamp": action.timestamp,
                            })

                        at = action.action_type
                        self.session_context["action_counts"][at] = self.session_context["action_counts"].get(at, 0) + 1
                        self.session_context["total_actions"] += 1

                        # Track C++ artifacts
                        if at in ("cpp_compile", "cpp_build", "cpp_link"):
                            binary = action.cpp_context.get("output_binary", "")
                            if binary and binary not in self.session_context["binaries_produced"]:
                                self.session_context["binaries_produced"].append(binary)
                                self.metrics["binaries_tracked"] += 1
                            self.session_context["compilation_history"].append({
                                "timestamp": action.timestamp,
                                "flags": action.cpp_context.get("compiler_flags", []),
                                "optimization": action.cpp_context.get("optimization_level", ""),
                                "output": binary,
                                "sources": [f for f in action.files_involved
                                            if f.endswith(('.cpp', '.c', '.cxx', '.cc'))],
                            })
                            self.metrics["compilations_tracked"] += 1

                        if at == "cpp_source_modification":
                            fp = action.tool_input.get("file_path", "")
                            if fp and fp not in self.session_context["source_files_modified"]:
                                self.session_context["source_files_modified"].append(fp)

                    self._write_context()

                    insights = CppAnalyzer.analyze(actions)
                    if insights:
                        self._update_warnings(insights)
                        self.metrics["insights_generated"] += len(insights)

                    self.metrics["actions_processed"] += len(actions)
                    self.metrics["last_analysis"] = datetime.now(timezone.utc).isoformat()
                    self._write_metrics()

            except Exception as e:
                self.logger.error(f"Bridge loop error: {e}")
                await asyncio.sleep(5)

        self.logger.info("LOCI C++ Bridge shutting down")

    def _read_queue(self) -> list[EngineeringAction]:
        actions = []
        if not self.queue_dir.exists():
            return actions
        queue_files = sorted(self.queue_dir.glob("*.json"))[:self.config.batch_size]
        for qf in queue_files:
            try:
                with open(qf, encoding="utf-8") as f:
                    data = json.load(f)
                actions.append(EngineeringAction.from_json(data))
                qf.unlink()
            except Exception as e:
                self.logger.warning(f"Failed to read queue file {qf}: {e}")
                qf.unlink()
        return actions

    def _write_context(self):
        with open(self.context_file, "w", encoding="utf-8") as f:
            json.dump(self.session_context, f, indent=2)

    def _update_warnings(self, new_insights: list[LociInsight]):
        existing = []
        if self.warnings_file.exists():
            try:
                with open(self.warnings_file, encoding="utf-8") as f:
                    data = json.load(f)
                    existing = data.get("warnings", [])
            except Exception:
                pass
        for insight in new_insights:
            if not insight.active:
                # Deactivate all existing warnings with the same (category, message)
                for w in existing:
                    if w.get("category") == insight.category and w.get("message") == insight.message:
                        w["active"] = False
                continue
            # Deduplicate: skip if an active warning with the same (category, message) exists
            already = any(
                w.get("category") == insight.category
                and w.get("message") == insight.message
                and w.get("active", True)
                for w in existing
            )
            if not already:
                existing.append(insight.to_dict())
        existing = existing[-50:]
        self.metrics["warnings_active"] = sum(1 for w in existing if w.get("active", True))
        self._write_warnings(existing)

    def _write_warnings(self, warnings: list):
        with open(self.warnings_file, "w", encoding="utf-8") as f:
            json.dump({"warnings": warnings, "updated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)

    def _write_metrics(self):
        with open(self.metrics_file, "w", encoding="utf-8") as f:
            json.dump(self.metrics, f, indent=2)

    def handle_signal(self):
        self._process_signal.set()

    def stop(self):
        self._running = False


def main():
    parser = argparse.ArgumentParser(description="LOCI API Bridge - C++ Context Aggregator")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("LOCI_STATE_DIR") or str(Path.cwd() / ".loci" / "state"),
        help="Directory for persistent state (defaults to $LOCI_STATE_DIR or <cwd>/.loci/state)",
    )
    parser.add_argument("--session", required=True)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[logging.FileHandler(state_dir / "bridge.log")],
    )

    config_path = Path(args.config) if args.config else state_dir.parent / "config" / "loci.json"
    config = BridgeConfig.load(config_path)
    bridge = LociBridge(state_dir, args.session, config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    signal.signal(signal.SIGUSR1, lambda s, f: bridge.handle_signal())
    signal.signal(signal.SIGTERM, lambda s, f: bridge.stop())
    signal.signal(signal.SIGINT, lambda s, f: bridge.stop())

    try:
        loop.run_until_complete(bridge.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
