import argparse
import asyncio

from psycopg import connect

from app.db import close_pool, connection, open_pool
from app.generation import generate_run
from app.model_config import load_advert_config, sync_models_from_config
from app.settings import ROOT_DIR, get_settings


def migrate() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required")

    migrations_dir = ROOT_DIR / "db" / "migrations"
    files = sorted(migrations_dir.glob("*.sql"))
    with connect(settings.database_url) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version text PRIMARY KEY,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        for file in files:
            applied = conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (file.name,)).fetchone()
            if applied:
                continue
            with conn.transaction():
                conn.execute(file.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (file.name,))
            print(f"applied {file.name}")


def seed() -> None:
    open_pool()
    try:
        sync_models_from_config()
        print("seeded models from config/advertbench.json")
    finally:
        close_pool()


def generate(ad_key: str | None) -> None:
    open_pool()
    try:
        config = load_advert_config()
        ads = config.ads
        if ad_key:
            ads = [ad for ad in config.ads if ad.key == ad_key]
            if not ads:
                available = ", ".join(ad.key for ad in config.ads)
                raise RuntimeError(f"Unknown ad key '{ad_key}'. Available ads: {available}")

        for ad in ads:
            run_id = asyncio.run(generate_run(ad.prompt))
            print(f"generated {ad.key} run {run_id}")
    finally:
        close_pool()


def runs(limit: int) -> None:
    open_pool()
    try:
        with connection() as conn:
            rows = conn.execute(
                """
                SELECT
                  gr.id,
                  gr.status,
                  gr.generated_with,
                  gr.created_at,
                  gr.completed_at,
                  count(os.id)::int AS output_sets,
                  count(os.id) FILTER (WHERE os.status = 'completed')::int AS completed_sets,
                  count(os.id) FILTER (WHERE os.status = 'failed')::int AS failed_sets,
                  left(gr.prompt, 80) AS prompt
                FROM generation_runs gr
                LEFT JOIN output_sets os ON os.run_id = gr.id
                GROUP BY gr.id
                ORDER BY gr.created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        for row in rows:
            print(
                f"{row['created_at']} {row['status']} "
                f"sets={row['completed_sets']}/{row['output_sets']} failed={row['failed_sets']} "
                f"id={row['id']} prompt={row['prompt']}"
            )
    finally:
        close_pool()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python backend/manage.py")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("migrate")
    subcommands.add_parser("seed")

    generate_parser = subcommands.add_parser("generate")
    generate_parser.add_argument("ad_key", nargs="?", help="Configured ad key. Omit to generate all ads in config/advertbench.json.")

    runs_parser = subcommands.add_parser("runs")
    runs_parser.add_argument("--limit", type=int, default=25)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.command == "migrate":
        migrate()
    elif args.command == "seed":
        seed()
    elif args.command == "generate":
        generate(args.ad_key)
    elif args.command == "runs":
        runs(args.limit)
