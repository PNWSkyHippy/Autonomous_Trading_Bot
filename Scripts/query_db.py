import sqlite3
import argparse
import json

def run_query(db_path, query):
    try:
        # Connect to the SQLite database
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            # Fetch results as a list of dictionaries for easy PS parsing
            columns = [column[0] for column in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]
            # Print as JSON for PowerShell to convert back to objects
            print(json.dumps(results))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Path to SQLite DB")
    parser.add_argument("--sql", required=True, help="SQL Query to run")
    args = parser.parse_args()
    
    run_query(args.db, args.sql)
