import os
import argparse
import shutil
from pathlib import Path
import pandas as pd
from datetime import timedelta
from amazonorders.session import AmazonSession
from amazonorders.transactions import AmazonTransactions

REPORT_COLUMNS = ['Status', 'Card Date', 'Card Amount', 'Amazon Order ID', 'Amazon Date', 'Amazon Total', 'Notes']


def load_dotenv():
    """Loads key/value pairs from supported env files into environment variables."""
    cwd = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    candidates = [
        cwd / '.env',
        script_dir / '.env',
    ]

    loaded_paths = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in loaded_paths or not path.exists():
            continue

        loaded_paths.add(resolved)
        with open(path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue

                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')

                if key and key not in os.environ:
                    os.environ[key] = value


def fetch_amazon_transactions(email, password, days):
    """Fetches charge-level transactions using the amazon-orders library."""
    print(f'[*] Authenticating and fetching Amazon transactions for last {days} days...')

    # Using browser extra in case of bot challenge detection
    session = AmazonSession(email, password)
    session.login()

    amazon_transactions = AmazonTransactions(session)
    raw_transactions = amazon_transactions.get_transactions(days=days)

    transactions_list = []
    for t in raw_transactions:
        try:
            total = float(t.grand_total)
        except ValueError, TypeError:
            total = 0.0

        # Amazon transaction charges are typically negative and refunds positive.
        # Flip sign so this aligns with statement convention (charges positive, credits negative).
        normalized_amount = -total

        transactions_list.append(
            {
                'order_id': t.order_number,
                'transaction_date': pd.to_datetime(t.completed_date),
                'amount': normalized_amount,
                'seller': t.seller if hasattr(t, 'seller') else None,
            }
        )

    return pd.DataFrame(transactions_list, columns=['order_id', 'transaction_date', 'amount', 'seller'])


def load_statement(csv_path):
    """Loads and filters the Discover statement."""
    print(f'[*] Loading statement from {csv_path}...')
    df = pd.read_csv(csv_path)

    # Harmonize Discover statement columns
    df['Trans. Date'] = pd.to_datetime(df['Trans. Date'])
    df['Post Date'] = pd.to_datetime(df['Post Date'])
    df['Amount'] = pd.to_numeric(df['Amount'])

    # Filter for Amazon transactions only
    amazon_mask = df['Description'].str.contains('AMAZON|AMZN|KINDLE', case=False, na=False)
    return df[amazon_mask].copy()


def resolve_statement_path(path_arg):
    """Resolves statement CSV from CLI arg or newest repo CSV (excluding generated report)."""
    if path_arg:
        csv_path = Path(path_arg).expanduser()
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        if not csv_path.exists():
            raise FileNotFoundError(f'Statement file not found: {csv_path}')
        return csv_path

    repo_root = Path(__file__).resolve().parent
    synced_recent_activity = sync_latest_recent_activity(repo_root)
    if synced_recent_activity is not None:
        return synced_recent_activity

    candidates = [p for p in repo_root.glob('*.csv') if p.name.lower() != 'reconciliation_report.csv']
    if not candidates:
        raise FileNotFoundError('No statement CSV found in repo root. Provide a filename or add a CSV file.')

    return max(candidates, key=lambda p: p.stat().st_mtime)


def sync_latest_recent_activity(repo_root):
    """Copies newest Discover-Statement CSV from Downloads into repo root."""
    downloads_dir = Path.home() / 'Downloads'
    candidates = sorted(
        downloads_dir.glob('Discover-Statement-*'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None

    latest = candidates[0]
    for existing in repo_root.glob('Discover-Statement-*'):
        if existing.name != latest.name:
            existing.unlink(missing_ok=True)

    destination = repo_root / latest.name
    shutil.copy2(latest, destination)
    print(f'[*] Synced latest Discover activity file: {destination.name}')
    return destination


def reconcile(statement_df, amazon_df, date_tolerance_days=4):
    """Compares statement charges against Amazon charge-level transactions."""
    print('[*] Reconciling statements against order history...')

    matched_statement_indices = set()
    matched_amazon_indices = set()

    results = []

    # 1. Look for Exact Price and Close Date Matches
    for idx, card_row in statement_df.iterrows():
        card_amt = card_row['Amount']
        card_date = card_row['Trans. Date']

        # Look for matching Amazon transaction amount within date tolerance
        potential_matches = amazon_df[
            ((amazon_df['amount'] - card_amt).abs() < 0.01)
            & (amazon_df['transaction_date'] <= card_date)
            & (amazon_df['transaction_date'] >= card_date - timedelta(days=date_tolerance_days))
            & (~amazon_df.index.isin(matched_amazon_indices))
        ]

        if not potential_matches.empty:
            # Pick the closest date match
            best_match = potential_matches.sort_values(by='transaction_date', ascending=False).iloc[0]

            results.append(
                {
                    'Status': 'MATCHED',
                    'Card Date': card_date.strftime('%Y-%m-%d'),
                    'Card Amount': card_amt,
                    'Amazon Order ID': best_match['order_id'],
                    'Amazon Date': best_match['transaction_date'].strftime('%Y-%m-%d'),
                    'Amazon Total': best_match['amount'],
                    'Notes': (
                        'Matched transaction'
                        + (f' | Seller: {best_match["seller"]}' if best_match.get('seller') else '')
                    ),
                }
            )
            matched_statement_indices.add(idx)
            matched_amazon_indices.add(best_match.name)

    # 2. Check for Duplicates on the Credit Card Side
    # Look for items with the same amount posted on the same day or consecutive days
    statement_df['is_dup'] = statement_df.duplicated(subset=['Amount', 'Trans. Date'], keep=False)

    # 3. Handle remaining unmatched items
    unmatched_card = statement_df[~statement_df.index.isin(matched_statement_indices)]
    for idx, card_row in unmatched_card.iterrows():
        notes = (
            'Potential Duplicate Charge' if card_row['is_dup'] else 'No matching order amount found (Split shipment?)'
        )
        results.append(
            {
                'Status': 'UNMATCHED_CARD_CHARGE',
                'Card Date': card_row['Trans. Date'].strftime('%Y-%m-%d'),
                'Card Amount': card_row['Amount'],
                'Amazon Order ID': 'N/A',
                'Amazon Date': 'N/A',
                'Amazon Total': 0.0,
                'Notes': f'{notes} | Desc: {card_row["Description"]}',
            }
        )

    # 4. Check for Amazon transactions never matched to statement rows
    # unmatched_transactions = amazon_df[~amazon_df.index.isin(matched_amazon_indices)]
    # for idx, tx_row in unmatched_transactions.iterrows():
    #     results.append(
    #         {
    #             'Status': 'UNMATCHED_AMAZON_TRANSACTION',
    #             'Card Date': 'N/A',
    #             'Card Amount': 0.0,
    #             'Amazon Order ID': tx_row['order_id'],
    #             'Amazon Date': tx_row['transaction_date'].strftime('%Y-%m-%d'),
    #             'Amazon Total': tx_row['amount'],
    #             'Notes': 'Amazon transaction has no direct matching statement row',
    #         }
    #     )

    return pd.DataFrame(results, columns=REPORT_COLUMNS)


if __name__ == '__main__':
    load_dotenv()

    parser = argparse.ArgumentParser(description='Reconcile credit card statement vs Amazon Orders.')
    parser.add_argument(
        'statement_csv',
        nargs='?',
        help='Path to statement CSV. If omitted, uses newest CSV in repo root excluding reconciliation_report.csv.',
    )
    args = parser.parse_args()
    statement_path = resolve_statement_path(args.statement_csv)

    email = os.getenv('AMAZON_USERNAME')
    password = os.getenv('AMAZON_PASSWORD')
    if not email or not password:
        raise ValueError('Missing AMAZON_USERNAME or AMAZON_PASSWORD. Set them in your environment or .env file.')

    # 1. Load statement
    card_df = load_statement(statement_path)
    if card_df.empty:
        print('[*] No Amazon-related rows found in statement. Writing empty reconciliation report.')
        report = pd.DataFrame(columns=REPORT_COLUMNS)
        report.to_csv('reconciliation_report.csv', index=False)
        print("\n[*] Reconciliation complete! Report saved to 'reconciliation_report.csv'.")
        print(report.to_string(index=False))
        raise SystemExit(0)

    post_dates = card_df['Post Date'].dropna()
    if post_dates.empty:
        raise ValueError('No valid Post Date values found in statement rows selected for reconciliation.')

    statement_post_start = post_dates.min().normalize() - timedelta(days=2)
    statement_post_end = post_dates.max().normalize() + timedelta(days=2)
    fetch_days = max((pd.Timestamp.now().normalize() - statement_post_start).days + 1, 1)

    # 2. Fetch Amazon transactions (charge-level, handles split shipments)
    try:
        amazon_df = fetch_amazon_transactions(email, password, days=fetch_days)
    except Exception as e:
        raise RuntimeError(f'Could not fetch live Amazon data: {e}') from e
    amazon_df = amazon_df[
        (amazon_df['transaction_date'] >= statement_post_start) & (amazon_df['transaction_date'] <= statement_post_end)
    ].copy()

    # 3. Execute
    report = reconcile(card_df, amazon_df)

    # 4. Save and view results
    report.to_csv('reconciliation_report.csv', index=False)
    print("\n[*] Reconciliation complete! Report saved to 'reconciliation_report.csv'.")
    print(report.to_string())
