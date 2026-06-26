import json
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

PRICING = {
    "gemini-1.5-flash": {"input_per_1m": 0.075, "output_per_1m": 0.30},
    "gemini-1.5-pro": {"input_per_1m": 1.25, "output_per_1m": 5.00},
    "gemini-2.0-flash": {"input_per_1m": 0.10, "output_per_1m": 0.40},
    "gemini-2.0-flash-lite": {"input_per_1m": 0.075, "output_per_1m": 0.30},
    "gemini-2.0-pro": {"input_per_1m": 1.25, "output_per_1m": 5.00},
    "gemini-2.5-flash": {"input_per_1m": 0.15, "output_per_1m": 0.60},
    "gemini-2.5-pro": {"input_per_1m": 1.25, "output_per_1m": 10.00},
}


def _lookup_pricing(model: str) -> dict:
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if key in model:
            return PRICING[key]
    return {"input_per_1m": 0.0, "output_per_1m": 0.0}


class TokenTracker:
    def __init__(self, project_name: str, output_dir: Path):
        self.project_name = project_name
        # output_dir is already outputs/<project> — don't add project_name again
        self.log_dir = output_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "token_usage.jsonl"
        self._session_records: list[dict] = []

    def record(
        self,
        phase: str,
        file: str,
        model: str,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
    ):
        pricing = _lookup_pricing(model)
        cost = (prompt_tokens / 1_000_000) * pricing["input_per_1m"] + \
               (completion_tokens / 1_000_000) * pricing["output_per_1m"]

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "project": self.project_name,
            "phase": phase,
            "file": file,
            "model": model,
            "provider": provider,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": round(cost, 6),
        }
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        self._session_records.append(entry)

    def _load_all_records(self) -> list[dict]:
        if not self.log_file.exists():
            return []
        records = []
        with open(self.log_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records

    def _aggregate(self, records: list[dict]) -> dict:
        stats: dict[str, dict] = {}
        for r in records:
            m = r["model"]
            if m not in stats:
                stats[m] = {"calls": 0, "prompt": 0, "completion": 0, "cost": 0.0}
            stats[m]["calls"] += 1
            stats[m]["prompt"] += r["prompt_tokens"]
            stats[m]["completion"] += r["completion_tokens"]
            stats[m]["cost"] += r["cost_usd"]
        return stats

    def print_summary(self, console: Console):
        stats = self._aggregate(self._session_records)
        all_records = self._load_all_records()
        cumulative_cost = sum(r["cost_usd"] for r in all_records)

        table = Table(title=f"Token Usage — {self.project_name} (this session)", show_lines=True)
        table.add_column("Model", style="cyan")
        table.add_column("Calls", justify="right")
        table.add_column("Prompt Tokens", justify="right")
        table.add_column("Completion Tokens", justify="right")
        table.add_column("Cost (USD)", justify="right", style="green")

        total_calls = total_prompt = total_completion = 0
        total_cost = 0.0
        for model, s in stats.items():
            table.add_row(model, str(s["calls"]), f"{s['prompt']:,}", f"{s['completion']:,}", f"${s['cost']:.4f}")
            total_calls += s["calls"]
            total_prompt += s["prompt"]
            total_completion += s["completion"]
            total_cost += s["cost"]

        table.add_section()
        table.add_row(
            "[bold]Session Total[/bold]",
            f"[bold]{total_calls}[/bold]",
            f"[bold]{total_prompt:,}[/bold]",
            f"[bold]{total_completion:,}[/bold]",
            f"[bold]${total_cost:.4f}[/bold]",
        )
        table.add_row(
            "[dim]Cumulative (all sessions)[/dim]", "", "", "",
            f"[dim]${cumulative_cost:.4f}[/dim]",
        )
        console.print(table)

    def write_summary(self, session_label: str):
        if not self._session_records:
            return
        stats = self._aggregate(self._session_records)
        all_records = self._load_all_records()
        cumulative_cost = sum(r["cost_usd"] for r in all_records)
        total_cost = sum(s["cost"] for s in stats.values())

        lines = [
            "# Token Usage Summary",
            "",
            f"**Session**: {session_label}  ",
            f"**Project**: {self.project_name}  ",
            f"**Date**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}  ",
            "",
            "| Model | Calls | Prompt Tokens | Completion Tokens | Cost (USD) |",
            "|---|---|---|---|---|",
        ]
        for model, s in stats.items():
            lines.append(f"| {model} | {s['calls']} | {s['prompt']:,} | {s['completion']:,} | ${s['cost']:.4f} |")
        lines += [
            f"| **Session Total** | | | | **${total_cost:.4f}** |",
            f"| *Cumulative (all sessions)* | | | | *${cumulative_cost:.4f}* |",
        ]

        summary_path = self.log_dir / "session_summary.md"
        with open(summary_path, "w") as f:
            f.write("\n".join(lines) + "\n")
