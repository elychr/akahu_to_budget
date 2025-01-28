"""Module for handling transaction processing and syncing."""
from datetime import datetime, timedelta
import decimal
import decimal
import logging
import pandas as pd
import requests
from actual.queries import (
    create_transaction, get_ruleset, reconcile_transaction,
    get_categories, get_payees
)
from sqlalchemy import text
from typing import Dict, Optional

from modules.account_fetcher import get_akahu_balance, get_actual_balance

def get_cached_names(actual) -> tuple[Dict[str, str], Dict[str, str]]:
    """Get cached category and payee names.
    
    Args:
        actual: Actual instance with active session
        
    Returns:
        Tuple of (category_names, payee_names) dictionaries mapping IDs to names
        
    Raises:
        RuntimeError: If there is any error accessing the Actual Budget database
    """
    try:
        # Get all categories using the API
        categories = get_categories(actual.session)
        category_names = {cat.id: cat.name for cat in categories} if categories else {}
        category_names[None] = "Uncategorized"
        if not categories:
            logging.info("No categories found in Actual Budget - this is normal for a new budget")
        
        # Get all payees using the API
        payees = get_payees(actual.session)
        payee_names = {payee.id: payee.name for payee in payees} if payees else {}
        if not payees:
            logging.info("No payees found in Actual Budget - this is normal for a new budget")
        
        return category_names, payee_names
        
    except Exception as e:
        logging.error(f"Error accessing Actual Budget database - aborting transaction processing: {str(e)}")
        raise RuntimeError(f"Failed to access Actual Budget database: {str(e)}") from None

def log_balance_comparison(source_name: str, source_balance: float, dest_name: str, dest_balance: float, dest_in_cents: bool = False):
    """Log a comparison between source and destination balances in a consistent format.
    
    Args:
        source_name: Name of the source system (e.g., 'Akahu')
        source_balance: Balance from source system (in dollars)
        dest_name: Name of the destination system (e.g., 'Actual', 'YNAB')
        dest_balance: Balance from destination system (in cents if dest_in_cents=True)
        dest_in_cents: Whether destination balance is in cents (default: False)
    """
    # Convert source balance to cents for comparison
    source_cents = int(decimal.Decimal(str(source_balance)) * 100)
    
    # Use destination balance as-is if it's already in cents
    dest_cents = int(dest_balance) if dest_in_cents else int(decimal.Decimal(str(dest_balance)) * 100)
    
    # Convert both to dollars for display
    source_dollars = decimal.Decimal(source_cents) / 100
    dest_dollars = decimal.Decimal(dest_cents) / 100
    
    logging.info(f"Balances (in dollars) - {source_name}: ${source_dollars:,.2f} | {dest_name}: ${dest_dollars:,.2f}")

def get_all_akahu(akahu_account_id, akahu_endpoint, akahu_headers, last_reconciled_at=None):
    """Fetch all transactions from Akahu for a given account, supporting pagination."""
    query_params = {}
    res = None
    total_txn = 0

    if last_reconciled_at:
        last_reconciled_at_dt = datetime.fromisoformat(last_reconciled_at.replace("Z", "+00:00"))
        start_time = last_reconciled_at_dt - timedelta(weeks=1)
        query_params['start'] = start_time.isoformat().replace("+00:00", "Z")
    else:
        query_params['start'] = "2024-01-01T00:00:00Z"

    next_cursor = 'first time'
    while next_cursor is not None:
        if next_cursor != 'first time':
            query_params['cursor'] = next_cursor

        try:
            response = requests.get(
                f"{akahu_endpoint}/accounts/{akahu_account_id}/transactions",
                params=query_params,
                headers=akahu_headers
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch transactions from Akahu for account {akahu_account_id}: {str(e)}")
            raise RuntimeError(f"Failed to fetch Akahu transactions: {str(e)}") from None

        akahu_txn_json = response.json()
        akahu_txn = pd.DataFrame(akahu_txn_json.get('items', []))
        if res is None:
            res = akahu_txn.copy()
        else:
            res = pd.concat([res, akahu_txn])

        num_txn = akahu_txn.shape[0]
        total_txn += num_txn
        if num_txn == 0 or 'cursor' not in akahu_txn_json or 'next' not in akahu_txn_json['cursor']:
            next_cursor = None
        else:
            next_cursor = akahu_txn_json['cursor']['next']

    if total_txn > 0:
        logging.info(f"Fetched {total_txn} transactions from Akahu.")
    return res

def load_transactions_into_actual(transactions, mapping_entry, actual):
    """Load transactions into Actual Budget using the mapping information."""
    if transactions is None or transactions.empty:
        logging.info("No transactions to load into Actual.")
        return

    actual_account_id = mapping_entry['actual_account_id']
    imported_transactions = []
    
    # Get cached names for rule changes - this will raise RuntimeError if it fails
    category_names, payee_names = get_cached_names(actual)
    
    # Get ruleset - no rules is a valid state for new budgets
    try:
        ruleset = get_ruleset(actual.session)
        if ruleset is None:
            logging.info("No ruleset found in Actual Budget - this is normal for a new budget")
    except Exception as e:
        logging.error(f"Database error while getting ruleset - aborting transaction processing: {str(e)}")
        raise RuntimeError(f"Failed to access Actual Budget database: {str(e)}") from None

    for _, txn in transactions.iterrows():
        transaction_date = txn.get("date")
        payee_name = txn.get("description")
        notes = txn.get('description')
        amount = decimal.Decimal(txn.get("amount"))
        amount = amount.quantize(decimal.Decimal("0.0001"))
        imported_id = txn.get("_id")
        cleared = True

        try:
            parsed_date = datetime.strptime(transaction_date.replace(".000", ""), "%Y-%m-%dT%H:%M:%SZ").date()
            reconciled_transaction = reconcile_transaction(
                actual.session,
                date=parsed_date,
                account=actual_account_id,
                payee=payee_name,
                notes=notes,
                amount=amount,
                imported_id=imported_id,
                cleared=cleared,
                imported_payee=payee_name,
                already_matched=imported_transactions
            )
            
            if ruleset is not None:
                # Store transaction state before running rules
                pre_rules_state = vars(reconciled_transaction).copy()
                ruleset.run(reconciled_transaction)
                
                # Compare states to see if rules modified the transaction
                post_rules_state = vars(reconciled_transaction)
                changes = []
                for key, value in post_rules_state.items():
                    if key in pre_rules_state and pre_rules_state[key] != value:
                        # Format category changes
                        if key == 'category_id':
                            old_name = category_names.get(pre_rules_state[key], "Unknown")
                            new_name = category_names.get(value, "Unknown")
                            changes.append(f"category: {old_name} -> {new_name}")
                        # Format payee changes
                        elif key == 'payee_id':
                            old_name = payee_names.get(pre_rules_state[key], "Unknown")
                            new_name = payee_names.get(value, "Unknown")
                            changes.append(f"payee: {old_name} -> {new_name}")
                        # Skip internal fields
                        elif not key.startswith('_'):
                            changes.append(f"{key}: {pre_rules_state[key]} -> {value}")
                if changes:
                    logging.info(f"Rules modified transaction {imported_id}:")
                    for change in changes:
                        logging.info(f"  {change}")
                else:
                    logging.debug(f"Rules did not modify transaction {imported_id}")

        except Exception as e:
            logging.error(f"Failed to reconcile transaction {imported_id} into Actual for account {actual_account_id}: {str(e)}")
            raise RuntimeError(f"Failed to process transaction into Actual: {str(e)}") from None

        if reconciled_transaction.changed():
            imported_transactions.append(reconciled_transaction)
            logging.info(f"Created new transaction on {parsed_date} at {payee_name} for ${amount}")
            if notes != payee_name:
                logging.debug(f"Transaction notes: {notes}")
        else:
            logging.debug(f"Transaction already exists on {parsed_date} at {payee_name} for ${amount}")

    mapping_entry['actual_synced_datetime'] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Commit all changes to Actual
    try:
        actual.commit()
        logging.info("Successfully committed changes to Actual Budget")
    except Exception as e:
        logging.error(f"Failed to commit changes to Actual Budget: {str(e)}")
        raise RuntimeError(f"Failed to commit changes to Actual: {str(e)}") from None
    
    return len(imported_transactions)

def handle_tracking_account_actual(mapping_entry, actual):
    """Handle tracking accounts by checking and adjusting balances."""
    akahu_account_id = mapping_entry['akahu_id']
    actual_account_id = mapping_entry['actual_account_id']
    akahu_account_name = mapping_entry['akahu_name']

    try:
        # Get balances
        akahu_balance_dollars = mapping_entry['akahu_balance']
        actual_balance_cents = get_actual_balance(actual, actual_account_id)
        
        # Log balance comparison
        log_balance_comparison('Akahu', akahu_balance_dollars, 'Actual', actual_balance_cents, dest_in_cents=True)
        
        # Convert Akahu balance to cents for comparison
        akahu_balance_cents = int(decimal.Decimal(str(akahu_balance_dollars)) * 100)

        if akahu_balance_cents != actual_balance_cents:
            # Calculate adjustment in dollars since Actual expects dollars
            adjustment_dollars = (akahu_balance_cents - actual_balance_cents) / 100

            transaction_date = datetime.utcnow().date()
            payee_name = "Balance Adjustment"
            notes = f"Adjusted from ${decimal.Decimal(actual_balance_cents)/100:,.2f} to ${decimal.Decimal(akahu_balance_cents)/100:,.2f} to reconcile tracking account"

            # Use the imported create_transaction function with the session directly
            # Note: Actual expects amounts in cents
            create_transaction(
                actual.session,
                date=transaction_date,
                account=actual_account_id,
                payee=payee_name,
                notes=notes,
                amount=adjustment_dollars,  # Actual expects dollars
                imported_id=f"adjustment_{datetime.utcnow().isoformat()}",
                cleared=True,
                imported_payee=payee_name
            )
            
            # Commit the adjustment transaction
            try:
                actual.commit()
                logging.info(f"Created and committed balance adjustment transaction of ${adjustment_dollars:,.2f}")
                return 1
            except Exception as e:
                logging.error(f"Failed to commit balance adjustment to Actual Budget: {str(e)}")
                raise RuntimeError(f"Failed to commit balance adjustment to Actual: {str(e)}") from None
        return 0

    except Exception as e:
        logging.error(f"Error handling tracking account {akahu_account_name}: {str(e)}")
        raise

def get_payee_name(row):
    """Extract the payee name from the given row, prioritizing the merchant name if available."""
    try:
        res = None
        if "merchant" in row and not pd.isna(row["merchant"]):
            if "name" in row["merchant"]:
                res = row['merchant']['name']
        if res is None:
            res = row['description']
    except (TypeError, ValueError) as e:
        logging.error(f"Error extracting payee name from row: {e}, row: {row}")
        res = "Unknown"
    return res

def convert_to_nzt(date_str):
    """Convert a given date string to New Zealand Time (NZT)."""
    try:
        if date_str is None:
            logging.warning("Input date string is None.")
            return None
        # Remove milliseconds if present before parsing
        date_str = date_str.replace(".000Z", "Z")
        utc_time = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
        nzt_time = utc_time + timedelta(hours=13)
        return nzt_time.strftime("%Y-%m-%d")
    except ValueError as e:
        logging.error(f"Error converting date string to NZT: {e}, date_str: {date_str}")
        return None

def clean_txn_for_ynab(akahu_txn, ynab_account_id):
    """Clean and transform Akahu transactions to prepare them for YNAB import."""
    akahu_txn['payee_name'] = akahu_txn.apply(get_payee_name, axis=1)
    akahu_txn['memo'] = akahu_txn['description']
    akahu_txn_useful = akahu_txn[['_id', 'date', 'amount', 'memo', 'payee_name']].rename(columns={'_id': 'id'}, errors='ignore')
    akahu_txn_useful['amount'] = akahu_txn_useful['amount'].apply(lambda x: str(int(x * 1000)))
    akahu_txn_useful['cleared'] = 'cleared'
    akahu_txn_useful['date'] = akahu_txn_useful.apply(lambda row: convert_to_nzt(row['date']), axis=1)
    akahu_txn_useful['import_id'] = akahu_txn_useful['id']
    akahu_txn_useful['flag_color'] = 'red'
    akahu_txn_useful['account_id'] = ynab_account_id

    return akahu_txn_useful

def get_ynab_transactions(ynab_budget_id, ynab_endpoint, ynab_headers):
    """Fetch all transactions from YNAB for a given budget."""
    uri = f"{ynab_endpoint}budgets/{ynab_budget_id}/transactions"
    try:
        response = requests.get(uri, headers=ynab_headers)
        response.raise_for_status()
        return response.json().get('data', {}).get('transactions', [])
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching transactions from YNAB: {e}")
        if response is not None:
            logging.error(f"API response content: {response.text}")
        raise

def load_transactions_into_ynab(akahu_txn, ynab_budget_id, ynab_account_id, ynab_endpoint, ynab_headers):
    """Save transactions from Akahu to YNAB."""
    uri = f"{ynab_endpoint}budgets/{ynab_budget_id}/transactions"
    transactions_list = akahu_txn.to_dict(orient='records')

    ynab_api_payload = {
        "transactions": transactions_list
    }
    
    try:
        response = requests.post(uri, headers=ynab_headers, json=ynab_api_payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to post transactions to YNAB for account {ynab_account_id}: {str(e)}")
        raise RuntimeError(f"Failed to load transactions into YNAB: {str(e)}") from None

    ynab_response = response.json()
    if 'duplicate_import_ids' in ynab_response['data'] and len(ynab_response['data']['duplicate_import_ids']) > 0:
        dup_str = f"Skipped {len(ynab_response['data']['duplicate_import_ids'])} duplicates"
    else:
        dup_str = "No duplicates"

    if len(ynab_response['data']['transactions']) == 0:
        logging.info(f"No new transactions loaded to YNAB - {dup_str}")
    else:
        logging.info(f"Successfully loaded {len(ynab_response['data']['transactions'])} transactions to YNAB - {dup_str}")

    return len(ynab_response['data']['transactions'])

def create_adjustment_txn_ynab(ynab_budget_id, ynab_account_id, akahu_balance, ynab_balance, ynab_endpoint, ynab_headers):
    """Create an adjustment transaction in YNAB to reconcile the balance."""
    try:
        balance_difference = akahu_balance - ynab_balance
        if balance_difference == 0:
            logging.info("No adjustment needed; balances are already in sync.")
            return
        
        uri = f"{ynab_endpoint}budgets/{ynab_budget_id}/transactions"
        transaction = {
            "transaction": {
                "account_id": ynab_account_id,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "amount": int(balance_difference),  # Already in milliunits for YNAB
                "payee_name": "Balance Adjustment",
                "memo": f"Adjusted from ${ynab_balance/1000:.2f} to ${akahu_balance/1000:.2f} based on retrieved balance",
                "flag_color": "red",
                "cleared": "cleared",
                "approved": True
            }
        }
        
        response = requests.post(uri, headers=ynab_headers, json=transaction)
        response.raise_for_status()
        logging.info(f"Created YNAB balance adjustment transaction of ${balance_difference:,.2f}")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to create balance adjustment transaction: {e}")
        raise