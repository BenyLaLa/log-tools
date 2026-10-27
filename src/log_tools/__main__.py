from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import load_settings


def main():
    parser = argparse.ArgumentParser(description="Local log investigation MCP")
    parser.add_argument("--config", default=os.environ.get("LOG_TOOLS_CONFIG", "config.json"))
    parser.add_argument(
        "--check", action="store_true", help="Validate configuration without connecting"
    )
    parser.add_argument(
        "--demo", action="store_true", help="Import sample logs and run an offline search"
    )
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.check:
        print(
            json.dumps(
                {
                    "ok": True,
                    "server_aliases": list(settings.servers),
                    "cache_dir": str(settings.cache_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.demo:
        from .service import LogService

        service = LogService(settings)
        try:
            sample = Path(args.config).resolve().parent / "examples" / "logs" / "order.log"
            dataset = service.import_local([str(sample)])
            result = service.query.search(dataset["dataset_id"], trace_id="demo-001")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            service.close()
    else:
        from .server import build_server

        build_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
