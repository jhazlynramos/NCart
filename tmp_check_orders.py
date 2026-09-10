import os
from pathlib import Path
import mysql.connector
from dotenv import load_dotenv
load_dotenv(Path(r'c:\Users\Jhonalyn Ann Ramos\Visual Code Files\NCart System\.env'))
config = {
    'host': os.getenv('NCART_DB_HOST', '127.0.0.1'),
    'port': int(os.getenv('NCART_DB_PORT', '3306')),
    'user': os.getenv('NCART_DB_USER', 'root'),
    'password': os.getenv('NCART_DB_PASSWORD', 'NewPassword123!'),
    'database': os.getenv('NCART_DB_NAME', 'ncart'),
}
conn = mysql.connector.connect(**config)
cur = conn.cursor(dictionary=True)
cur.execute('SHOW CREATE TABLE orders')
print(cur.fetchone())
cur.execute('SHOW COLUMNS FROM orders')
print(cur.fetchall())
conn.close()
