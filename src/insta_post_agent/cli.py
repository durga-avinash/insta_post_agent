from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .pipeline import run_daily


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a sourced, review-first daily Instagram news carousel")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root (contains config/sources.json)")
    parser.add_argument("--output-root", type=Path, default=Path("output"))
    parser.add_argument("--history-path", type=Path, default=Path("state/news_history.sqlite3"))
    args = parser.parse_args(argv)
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / "daily-news.log", encoding="utf-8")],
    )
    try:
        summary = run_daily(root=args.root, output_root=args.output_root, history_path=args.history_path)
    except Exception:
        logging.getLogger(__name__).exception("Daily batch failed")
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary["workflow_complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
