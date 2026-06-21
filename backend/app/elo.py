def calculate_elo(winner_rating: int, loser_rating: int, k_factor: int = 32) -> dict[str, int | float]:
    expected_winner = 1 / (1 + 10 ** ((loser_rating - winner_rating) / 400))
    expected_loser = 1 / (1 + 10 ** ((winner_rating - loser_rating) / 400))
    return {
        "winner_rating": round(winner_rating + k_factor * (1 - expected_winner)),
        "loser_rating": round(loser_rating + k_factor * (0 - expected_loser)),
        "expected_winner": expected_winner,
        "expected_loser": expected_loser,
        "k_factor": k_factor,
    }


def pair_key(a: str, b: str) -> str:
    return ":".join(sorted([a, b]))
