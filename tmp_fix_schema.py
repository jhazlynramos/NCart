import os
import mysql.connector
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))
config = {
    'host': os.getenv('NCART_DB_HOST', '127.0.0.1'),
    'port': int(os.getenv('NCART_DB_PORT', '3306')),
    'user': os.getenv('NCART_DB_USER', 'root'),
    'password': os.getenv('NCART_DB_PASSWORD', 'NewPassword123!'),
    'database': os.getenv('NCART_DB_NAME', 'ncart'),
}

conn = mysql.connector.connect(**config)
cur = conn.cursor()
try:
    try:
        cur.execute("ALTER TABLE orders ADD COLUMN payment_method VARCHAR(40) NOT NULL DEFAULT 'Cash on Delivery' AFTER total_price")
    except mysql.connector.Error as exc:
        if exc.errno != 1060:
            raise
    cur.execute("SHOW COLUMNS FROM orders")
    print(cur.fetchall())
    conn.commit()
finally:
    cur.close()
    conn.close()
