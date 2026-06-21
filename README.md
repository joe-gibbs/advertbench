# AdvertBench

AdvertBench ranks AI-generated image ad sets by Elo voting.

## Ad sizes

Default sizes (as used by Meta):

- `1080x1080` feed square
- `1440x1800` feed vertical, 4:5
- `1200x628` feed landscape, 1.91:1
- `1080x1920` story/reel, 9:16

Edit `config/advertbench.json` to change models, per-model settings, ad prompts, or sizes.

## Local setup

```bash
cp .env.example .env
docker compose up -d
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python backend/manage.py migrate
python backend/manage.py seed
python -m uvicorn backend.app.main:app --reload --port 8000
```

Open `http://localhost:8000`.

## Generate a run

Runs are generated from the command line:

```bash
python backend/manage.py generate productivity_app
```

Omit the ad key to generate every ad listed in `config/advertbench.json`:

```bash
python backend/manage.py generate
```

Recent runs can be inspected with:

```bash
python backend/manage.py runs
```

`OPENROUTER_API_KEY` and `E2B_API_KEY` are required for generation.

## Real generation

With keys configured, each model in `config/advertbench.json` runs as an agent through OpenRouter. The agent has the following tools:

- `bash`: run a bash command inside the E2B sandbox
- `view_image`: inspect a generated image, only when the model supports image input
- `final`: declare completion

A model that has not produced every required PNG after `GENERATION_MAX_TURNS` is recorded as a failed `output_set` and the run continues with the next model. If the model supports image input, the agent will use the `view_image` tool to check composition, legibility, and sizing before finalizing.

Required environment variables:

```bash
OPENROUTER_API_KEY=...
E2B_API_KEY=...
GENERATION_MAX_TURNS=100
APP_BASE_URL=https://your-render-url.onrender.com
```

The leaderboard aggregates by model and shows successful and failed generation counts from `output_sets.status`.