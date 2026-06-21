from psycopg import errors

from .db import connection, transaction
from .elo import calculate_elo, pair_key
from .model_config import load_advert_config
from .openrouter import openrouter_model_capabilities_sync


def _shape_set(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "modelSlug": row["model_slug"],
        "modelName": row["model_name"],
        "rating": row["rating"],
        "wins": row["wins"],
        "losses": row["losses"],
        "matches": row["matches"],
        "assets": row["assets"],
    }


def _format_model_settings(metadata: dict | None) -> str:
    settings = (metadata or {}).get("settings") or {}
    if not settings:
        return "default"

    parts = []
    reasoning = settings.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        parts.append(f"reasoning: {reasoning['effort']}")

    for key in ("verbosity", "temperature", "top_p", "max_tokens"):
        if key in settings:
            parts.append(f"{key}: {settings[key]}")

    extras = [key for key in settings if key not in {"reasoning", "verbosity", "temperature", "top_p", "max_tokens"}]
    parts.extend(f"{key}: {settings[key]}" for key in extras)
    return "; ".join(parts) if parts else "custom"


def get_sets_by_ids(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT
              os.id,
              m.slug AS model_slug,
              m.display_name AS model_name,
              os.rating,
              os.wins,
              os.losses,
              os.matches,
              jsonb_agg(
                jsonb_build_object(
                  'key', aa.size_key,
                  'label', aa.label,
                  'width', aa.width,
                  'height', aa.height,
                  'publicPath', aa.public_path
                )
                ORDER BY aa.width * aa.height
              ) AS assets
            FROM output_sets os
            JOIN models m ON m.id = os.model_id
            JOIN ad_assets aa ON aa.output_set_id = os.id
            WHERE os.id = ANY(%s::uuid[])
            GROUP BY os.id, m.slug, m.display_name
            ORDER BY array_position(%s::uuid[], os.id)
            """,
            (ids, ids),
        ).fetchall()
    return [_shape_set(row) for row in rows]


def get_next_comparison(voter_hash: str) -> dict | None:
    config = load_advert_config()
    min_assets = len(config.ad_sizes)
    with connection() as conn:
        row = conn.execute(
            """
            WITH ready_sets AS (
              SELECT os.id, os.model_id, os.rating
              FROM output_sets os
              JOIN ad_assets aa ON aa.output_set_id = os.id
              WHERE os.status = 'completed'
              GROUP BY os.id
              HAVING count(*) >= %s
            )
            SELECT a.id AS a_id, b.id AS b_id
            FROM ready_sets a
            JOIN ready_sets b ON a.id < b.id AND a.model_id <> b.model_id
            WHERE NOT EXISTS (
              SELECT 1
              FROM votes v
              WHERE v.voter_hash = %s
                AND v.pair_key = concat(least(a.id::text, b.id::text), ':', greatest(a.id::text, b.id::text))
            )
            ORDER BY abs(a.rating - b.rating), random()
            LIMIT 1
            """,
            (min_assets, voter_hash),
        ).fetchone()

    if not row:
        return None

    a_id = str(row["a_id"])
    b_id = str(row["b_id"])
    return {"pairKey": pair_key(a_id, b_id), "sets": get_sets_by_ids([a_id, b_id])}


def get_leaderboard() -> list[dict]:
    try:
        capabilities = openrouter_model_capabilities_sync()
    except Exception:
        capabilities = {}

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT
              m.id AS model_id,
              m.display_name AS model_name,
              m.slug AS model_slug,
              m.metadata AS model_metadata,
              COALESCE(round(avg(os.rating) FILTER (WHERE os.status = 'completed')), 1200)::int AS rating,
              COALESCE(sum(os.wins) FILTER (WHERE os.status = 'completed'), 0)::int AS wins,
              COALESCE(sum(os.losses) FILTER (WHERE os.status = 'completed'), 0)::int AS losses,
              COALESCE(sum(os.matches) FILTER (WHERE os.status = 'completed'), 0)::int AS matches,
              count(os.id) FILTER (WHERE os.status = 'completed')::int AS successful_generations,
              count(os.id) FILTER (WHERE os.status = 'failed')::int AS failed_generations
            FROM models m
            LEFT JOIN output_sets os ON os.model_id = m.id
            GROUP BY m.id, m.display_name, m.slug
            ORDER BY rating DESC, matches DESC, successful_generations DESC, m.display_name
            LIMIT 10
            """
        ).fetchall()
    return [
        {
            "id": str(row["model_id"]),
            "modelName": row["model_name"],
            "modelSlug": row["model_slug"],
            "modelConfig": _format_model_settings(row["model_metadata"]),
            "releaseDate": (row["model_metadata"] or {}).get("releaseDate")
            or capabilities.get(row["model_slug"], {}).get("releaseDate"),
            "supportsImages": capabilities.get(row["model_slug"], {}).get("supportsImages"),
            "rating": row["rating"],
            "wins": row["wins"],
            "losses": row["losses"],
            "matches": row["matches"],
            "successfulGenerations": row["successful_generations"],
            "failedGenerations": row["failed_generations"],
        }
        for row in rows
    ]


def record_vote(
    *,
    winner_set_id: str,
    loser_set_id: str,
    voter_hash: str,
    ip_hash: str,
    user_agent_hash: str,
    idempotency_key: str | None,
) -> dict:
    vote_pair_key = pair_key(winner_set_id, loser_set_id)

    with transaction() as conn:
        counts = conn.execute(
            """
            SELECT
              count(*) FILTER (WHERE voter_hash = %s AND created_at > now() - interval '1 minute') AS voter_minute,
              count(*) FILTER (WHERE ip_hash = %s AND created_at > now() - interval '1 minute') AS ip_minute,
              count(*) FILTER (WHERE voter_hash = %s AND created_at > now() - interval '1 day') AS voter_day
            FROM vote_events
            WHERE created_at > now() - interval '1 day'
              AND (voter_hash = %s OR ip_hash = %s)
            """,
            (voter_hash, ip_hash, voter_hash, voter_hash, ip_hash),
        ).fetchone()

        if int(counts["voter_minute"]) >= 20 or int(counts["ip_minute"]) >= 80 or int(counts["voter_day"]) >= 500:
            conn.execute(
                """
                INSERT INTO vote_events (event_type, voter_hash, ip_hash, user_agent_hash, accepted, reason)
                VALUES ('vote', %s, %s, %s, false, 'rate_limited')
                """,
                (voter_hash, ip_hash, user_agent_hash),
            )
            return {"accepted": False, "reason": "rate_limited"}

        sets = conn.execute(
            """
            SELECT id, model_id, rating
            FROM output_sets
            WHERE id IN (%s, %s) AND status = 'completed'
            FOR UPDATE
            """,
            (winner_set_id, loser_set_id),
        ).fetchall()

        if len(sets) != 2:
            return {"accepted": False, "reason": "invalid_sets"}

        winner = next((row for row in sets if str(row["id"]) == winner_set_id), None)
        loser = next((row for row in sets if str(row["id"]) == loser_set_id), None)
        if not winner or not loser or winner["model_id"] == loser["model_id"]:
            return {"accepted": False, "reason": "invalid_pair"}

        elo = calculate_elo(winner["rating"], loser["rating"])

        try:
            conn.execute(
                """
                INSERT INTO votes (
                  pair_key,
                  winner_set_id,
                  loser_set_id,
                  voter_hash,
                  ip_hash,
                  user_agent_hash,
                  idempotency_key,
                  winner_rating_before,
                  loser_rating_before,
                  winner_rating_after,
                  loser_rating_after,
                  k_factor
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    vote_pair_key,
                    winner_set_id,
                    loser_set_id,
                    voter_hash,
                    ip_hash,
                    user_agent_hash,
                    idempotency_key,
                    winner["rating"],
                    loser["rating"],
                    elo["winner_rating"],
                    elo["loser_rating"],
                    elo["k_factor"],
                ),
            )
        except errors.UniqueViolation:
            conn.execute(
                """
                INSERT INTO vote_events (event_type, voter_hash, ip_hash, user_agent_hash, accepted, reason)
                VALUES ('vote', %s, %s, %s, false, 'duplicate_pair')
                """,
                (voter_hash, ip_hash, user_agent_hash),
            )
            return {"accepted": False, "reason": "duplicate_pair"}

        conn.execute(
            """
            UPDATE output_sets
            SET rating = %s, matches = matches + 1, wins = wins + 1
            WHERE id = %s
            """,
            (elo["winner_rating"], winner_set_id),
        )
        conn.execute(
            """
            UPDATE output_sets
            SET rating = %s, matches = matches + 1, losses = losses + 1
            WHERE id = %s
            """,
            (elo["loser_rating"], loser_set_id),
        )
        conn.execute(
            """
            INSERT INTO vote_events (event_type, voter_hash, ip_hash, user_agent_hash, accepted)
            VALUES ('vote', %s, %s, %s, true)
            """,
            (voter_hash, ip_hash, user_agent_hash),
        )

    return {
        "accepted": True,
        "winnerRating": elo["winner_rating"],
        "loserRating": elo["loser_rating"],
    }
