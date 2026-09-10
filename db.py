import os
from contextlib import contextmanager
from pathlib import Path

import mysql.connector
from mysql.connector import pooling


def load_local_environment():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"\''))


load_local_environment()


DATABASE_CONFIG = {
    "host": os.getenv("NCART_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("NCART_DB_PORT", "3306")),
    "user": os.getenv("NCART_DB_USER", "root"),
    "password": os.getenv("NCART_DB_PASSWORD", "NewPassword123!"),
    "database": os.getenv("NCART_DB_NAME", "ncart"),
}

_pool = None


def initialize_database():
    server_config = {key: value for key, value in DATABASE_CONFIG.items() if key != "database"}
    try:
        connection = mysql.connector.connect(**server_config)
    except mysql.connector.Error as error:
        if error.errno == 1045:
            raise RuntimeError(
                "MySQL rejected the credentials. Set NCART_DB_PASSWORD in .env "
                "to the password for NCART_DB_USER."
            ) from error
        raise
    cursor = connection.cursor()
    try:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                cursor.execute(statement)
        try:
            cursor.execute("ALTER TABLE listings ADD COLUMN user_id BIGINT UNSIGNED NULL AFTER id")
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise
        try:
            cursor.execute("ALTER TABLE orders ADD COLUMN payment_method VARCHAR(40) NOT NULL DEFAULT 'Cash on Delivery' AFTER total_price")
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN reply_to_id BIGINT UNSIGNED NULL AFTER body")
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN read_at TIMESTAMP NULL DEFAULT NULL AFTER body")
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise
        try:
            cursor.execute("ALTER TABLE favorites ADD COLUMN user_id BIGINT UNSIGNED NULL AFTER id")
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise
        cursor.execute(
            "UPDATE favorites SET user_id = (SELECT id FROM users ORDER BY id LIMIT 1) WHERE user_id IS NULL"
        )
        try:
            cursor.execute("ALTER TABLE favorites DROP FOREIGN KEY fk_favorites_listing")
        except mysql.connector.Error as error:
            if error.errno != 1091:
                raise
        try:
            cursor.execute("ALTER TABLE favorites DROP INDEX uq_favorites_listing")
        except mysql.connector.Error as error:
            if error.errno != 1091:
                raise
        try:
            cursor.execute("ALTER TABLE favorites ADD UNIQUE KEY uq_favorites_user_listing (user_id, listing_id)")
        except mysql.connector.Error as error:
            if error.errno != 1061:
                raise
        try:
            cursor.execute(
                "ALTER TABLE favorites ADD CONSTRAINT fk_favorites_listing "
                "FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE CASCADE"
            )
        except mysql.connector.Error as error:
            if error.errno != 1826:
                raise
        try:
            cursor.execute(
                "ALTER TABLE favorites ADD CONSTRAINT fk_favorites_user "
                "FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE"
            )
        except mysql.connector.Error as error:
            if error.errno != 1826:
                raise
        cursor.execute(
            """
            UPDATE listings
            SET user_id = (SELECT id FROM users ORDER BY id LIMIT 1)
            WHERE user_id IS NULL
            """
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()


def get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="ncart_pool",
            pool_size=5,
            **DATABASE_CONFIG,
        )
    return _pool


@contextmanager
def get_connection():
    connection = get_pool().get_connection()
    try:
        yield connection
    finally:
        connection.close()


def create_user(full_name, email, password_hash):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (full_name, email, password_hash) VALUES (%s, %s, %s)",
                (full_name, email, password_hash),
            )
            connection.commit()
        finally:
            cursor.close()


def find_user_by_email(email):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, full_name, email, password_hash FROM users WHERE email = %s",
                (email,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()


def find_user_by_id(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, full_name, email FROM users WHERE id = %s",
                (user_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()


def update_user_password(email, password_hash):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (password_hash, email),
            )
            connection.commit()
        finally:
            cursor.close()


def create_listing(user_id, title, category, item_condition, description, price, image_url=""):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO listings
                    (user_id, title, category, item_condition, description, price, image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, title, category, item_condition, description, price, image_url or None),
            )
            connection.commit()
            return cursor.lastrowid
        finally:
            cursor.close()


def update_listing(user_id, listing_id, title, category, item_condition, description, price, image_url=None):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1 FROM listings WHERE id = %s AND user_id = %s", (listing_id, user_id))
            if cursor.fetchone() is None:
                return False
            cursor.execute(
                """
                UPDATE listings
                SET title = %s, category = %s, item_condition = %s,
                    description = %s, price = %s,
                    image_url = COALESCE(%s, image_url)
                WHERE id = %s
                """,
                (title, category, item_condition, description, price, image_url, listing_id),
            )
            connection.commit()
            return True
        finally:
            cursor.close()


def find_listings(user_id=None):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            query = """
                SELECT l.id, l.user_id AS seller_id, l.title, l.category, l.item_condition,
                      l.description, l.price, l.image_url, l.created_at,
                      COALESCE(u.full_name, 'NCart seller') AS seller_name
                  FROM listings l
                  LEFT JOIN users u ON u.id = l.user_id
            """
            parameters = ()
            if user_id is not None:
                query += " WHERE user_id = %s"
                parameters = (user_id,)
            query += " ORDER BY created_at DESC, id DESC"
            cursor.execute(query, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()


def add_favorite(user_id, listing_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT IGNORE INTO favorites (user_id, listing_id) VALUES (%s, %s)",
                (user_id, listing_id),
            )
            connection.commit()
            return cursor.rowcount
        finally:
            cursor.close()


def remove_favorite(user_id, listing_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("DELETE FROM favorites WHERE user_id = %s AND listing_id = %s", (user_id, listing_id))
            connection.commit()
            return cursor.rowcount
        finally:
            cursor.close()


def create_order(buyer_id, listing_id, payment_method="Cash on Delivery"):
    allowed_methods = {"Cash on Delivery", "GCash", "Bank Transfer"}
    normalized_method = str(payment_method or "Cash on Delivery").strip()
    if normalized_method not in allowed_methods:
        normalized_method = "Cash on Delivery"

    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, user_id AS seller_id, title, price FROM listings WHERE id = %s",
                (listing_id,),
            )
            listing = cursor.fetchone()
            if listing is None:
                return None, "Listing not found."
            if int(listing["seller_id"]) == int(buyer_id):
                return None, "You cannot order your own listing."
            cursor.execute(
                "INSERT INTO orders (buyer_id, seller_id, listing_id, total_price, payment_method, status) VALUES (%s, %s, %s, %s, %s, 'pending')",
                (buyer_id, listing["seller_id"], listing_id, listing["price"], normalized_method),
            )
            connection.commit()
            return cursor.lastrowid, None
        finally:
            cursor.close()


def find_orders_for_user(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                  SELECT o.id, o.listing_id, o.buyer_id, o.seller_id, o.total_price, o.payment_method, o.status, o.created_at,
                      CASE WHEN o.status = 'pending' AND o.created_at >= NOW() - INTERVAL 24 HOUR THEN 1 ELSE 0 END AS can_cancel,
                       l.title AS listing_title, l.image_url AS listing_image,
                       seller.full_name AS seller_name
                FROM orders o
                JOIN listings l ON l.id = o.listing_id
                JOIN users seller ON seller.id = o.seller_id
                WHERE o.buyer_id = %s
                ORDER BY o.created_at DESC, o.id DESC
                """,
                (user_id,),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def find_sales_for_user(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT o.id, o.listing_id, o.buyer_id, o.seller_id, o.total_price, o.payment_method, o.status, o.created_at,
                       l.title AS listing_title, l.image_url AS listing_image,
                       buyer.full_name AS buyer_name
                FROM orders o
                JOIN listings l ON l.id = o.listing_id
                JOIN users buyer ON buyer.id = o.buyer_id
                WHERE o.seller_id = %s
                ORDER BY o.created_at DESC, o.id DESC
                """,
                (user_id,),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def update_order_status(seller_id, order_id, status):
    allowed_statuses = {"pending", "shipped", "out_for_delivery", "received"}
    if status not in allowed_statuses:
        return False, "Invalid order status."
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE orders
                SET status = %s
                WHERE id = %s AND seller_id = %s AND status <> 'cancelled'
                """,
                (status, order_id, seller_id),
            )
            if cursor.rowcount == 0:
                return False, "Order not found or it cannot be updated."
            connection.commit()
            return True, None
        finally:
            cursor.close()


def find_order_status_notification(order_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT o.id, o.status, l.title AS listing_title,
                       buyer.email AS buyer_email, buyer.full_name AS buyer_name
                FROM orders o
                JOIN listings l ON l.id = o.listing_id
                JOIN users buyer ON buyer.id = o.buyer_id
                WHERE o.id = %s
                """,
                (order_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()


def cancel_order(buyer_id, order_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT status FROM orders WHERE id = %s AND buyer_id = %s",
                (order_id, buyer_id),
            )
            order = cursor.fetchone()
            if order is None:
                return False, "Order not found."
            if order["status"] != "pending":
                return False, "This order can no longer be cancelled."
            cursor.execute(
                """
                UPDATE orders
                SET status = 'cancelled'
                WHERE id = %s AND buyer_id = %s AND status = 'pending'
                  AND created_at >= NOW() - INTERVAL 24 HOUR
                """,
                (order_id, buyer_id),
            )
            if cursor.rowcount == 0:
                return False, "The 24-hour cancellation period has expired."
            connection.commit()
            return True, None
        finally:
            cursor.close()


def find_favorite_ids(user_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT listing_id FROM favorites WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()


def find_favorite_listings(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT l.id, l.user_id AS seller_id, l.title, l.category, l.item_condition,
                       l.description, l.price, l.image_url, l.created_at
                FROM favorites f
                JOIN listings l ON l.id = f.listing_id
                WHERE f.user_id = %s
                ORDER BY f.created_at DESC, f.id DESC
                """,
                (user_id,),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def create_message(listing_id, sender_id, recipient_id, body, reply_to_id=None):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO messages (listing_id, sender_id, recipient_id, body, reply_to_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (listing_id, sender_id, recipient_id, body, reply_to_id),
            )
            connection.commit()
            return cursor.lastrowid
        finally:
            cursor.close()


def find_messages(user_id, listing_id, other_user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                  SELECT m.id, m.listing_id, m.sender_id, m.recipient_id,
                      m.body, m.reply_to_id, m.created_at, u.full_name AS sender_name,
                      replied.body AS replied_body, replied_sender.full_name AS replied_sender_name,
                      l.user_id AS listing_seller_id,
                      l.title AS listing_title, l.category AS listing_category,
                      l.price AS listing_price, l.image_url AS listing_image
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                                    LEFT JOIN messages replied ON replied.id = m.reply_to_id
                                    LEFT JOIN users replied_sender ON replied_sender.id = replied.sender_id
                  JOIN listings l ON l.id = m.listing_id
                WHERE m.listing_id = %s
                  AND ((m.sender_id = %s AND m.recipient_id = %s)
                    OR (m.sender_id = %s AND m.recipient_id = %s))
                ORDER BY m.created_at ASC, m.id ASC
                """,
                (listing_id, user_id, other_user_id, other_user_id, user_id),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def find_user_thread(user_id, other_user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                  SELECT m.id, m.listing_id, m.sender_id, m.recipient_id,
                      m.body, m.reply_to_id, m.created_at, u.full_name AS sender_name,
                      replied.body AS replied_body, replied_sender.full_name AS replied_sender_name,
                       l.user_id AS listing_seller_id,
                       l.title AS listing_title, l.category AS listing_category,
                       l.price AS listing_price, l.image_url AS listing_image
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                JOIN listings l ON l.id = m.listing_id
                LEFT JOIN messages replied ON replied.id = m.reply_to_id
                LEFT JOIN users replied_sender ON replied_sender.id = replied.sender_id
                WHERE (m.sender_id = %s AND m.recipient_id = %s)
                   OR (m.sender_id = %s AND m.recipient_id = %s)
                ORDER BY m.created_at ASC, m.id ASC
                """,
                (user_id, other_user_id, other_user_id, user_id),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def find_user_messages(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                  SELECT m.id, m.listing_id, m.sender_id, m.recipient_id, m.body,
                       m.created_at, l.title AS listing_title,
                       l.category AS listing_category, l.price AS listing_price,
                       l.image_url AS listing_image,
                                             (SELECT COUNT(*) FROM messages unread
                                                WHERE unread.sender_id = other.id
                                                    AND unread.recipient_id = %s
                                                    AND unread.read_at IS NULL) AS unread_count,
                       other.id AS other_user_id, other.full_name AS other_name
                FROM messages m
                JOIN listings l ON l.id = m.listing_id
                JOIN users other ON other.id = IF(m.sender_id = %s, m.recipient_id, m.sender_id)
                                WHERE (m.sender_id = %s OR m.recipient_id = %s)
                                    AND NOT EXISTS (
                                        SELECT 1 FROM messages newer
                                        WHERE ((newer.sender_id = m.sender_id AND newer.recipient_id = m.recipient_id)
                                                OR (newer.sender_id = m.recipient_id AND newer.recipient_id = m.sender_id))
                                            AND (newer.created_at > m.created_at
                                                OR (newer.created_at = m.created_at AND newer.id > m.id))
                                    )
                ORDER BY m.created_at DESC, m.id DESC
                """,
                (user_id, user_id, user_id, user_id),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def count_unread_messages(user_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM messages WHERE recipient_id = %s AND read_at IS NULL",
                (user_id,),
            )
            return cursor.fetchone()[0]
        finally:
            cursor.close()


def mark_messages_read(user_id, listing_id, other_user_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE messages
                SET read_at = CURRENT_TIMESTAMP
                                WHERE (%s IS NULL OR listing_id = %s)
                                    AND recipient_id = %s AND sender_id = %s
                  AND read_at IS NULL
                """,
                (listing_id, listing_id, user_id, other_user_id),
            )
            connection.commit()
        finally:
            cursor.close()
