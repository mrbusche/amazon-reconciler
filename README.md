# amazon-reconciler

Reconciles Amazon-related charges from a Discover CSV statement against charge-level Amazon transactions and writes a reconciliation report.

## What it does

- Loads a Discover statement CSV and keeps rows where `Description` contains `AMAZON`, `AMZN`, or `KINDLE`.
- Fetches Amazon charge-level transactions using `amazon-orders`.
- Matches by amount with a date tolerance window, then flags unmatched card charges.
- Writes results to `reconciliation_report.csv`.

## Amazon fetch date window

Amazon transactions are scoped to the statement posting window with a buffer:

- **Start:** 2 days before the first card `Post Date`
- **End:** 2 days after the last card `Post Date`

The script first fetches enough recent Amazon history to include that window, then filters transactions to this exact date range.

## Setup

Create a `.env` file in the repo root (or next to `main.py`):

```env
AMAZON_USERNAME=you@example.com
AMAZON_PASSWORD=your_password
```

## Input statement format

The CSV is expected to include at least:

- `Trans. Date`
- `Post Date`
- `Description`
- `Amount`

## Run

```bash
uv run python main.py
```

Current script behavior reads `Discover-Statement-20260704.csv` by default.
