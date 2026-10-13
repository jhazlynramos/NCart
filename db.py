import os
import hmac
import logging
import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
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



def get_database_config():
    is_railway = bool(os.getenv("RAILWAY_ENVIRONMENT"))

    if is_railway:
        config = {
            "host": os.getenv("NCART_DB_HOST") or os.getenv("MYSQLHOST"),
            "port": int(os.getenv("NCART_DB_PORT") or os.getenv("MYSQLPORT") or "3306"),
            "user": os.getenv("NCART_DB_USER") or os.getenv("MYSQLUSER"),
            "password": os.getenv("NCART_DB_PASSWORD") or os.getenv("MYSQLPASSWORD"),
            "database": os.getenv("NCART_DB_NAME") or os.getenv("MYSQLDATABASE"),
        }

        missing = [
            key for key, value in config.items()
            if value is None or value == ""
        ]
        if missing:
            raise RuntimeError(
                "Missing Railway database environment variables: "
                + ", ".join(missing)
            )

        return config

    return {
        "host": os.getenv("NCART_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("NCART_DB_PORT", "3306")),
        "user": os.getenv("NCART_DB_USER", "root"),
        "password": os.getenv("NCART_DB_PASSWORD", ""),
        "database": os.getenv("NCART_DB_NAME", "ncart"),
    }


DATABASE_CONFIG = get_database_config()


_pool = None
logger = logging.getLogger(__name__)
_admin_access_attempts = {}
_admin_access_attempts_lock = threading.Lock()


def _verified_account_sql(user_alias):
    return f"""
        CASE
          WHEN {user_alias}.status = 'active'
           AND {user_alias}.approval_status = 'approved'
           AND {user_alias}.account_status IN ('active', 'verified')
           AND (
             {user_alias}.confirmed_by IS NOT NULL
             OR {user_alias}.verification_reviewed_by IS NOT NULL
           )
          THEN 1 ELSE 0
        END
    """


def normalize_name_part(value):
    return " ".join(str(value or "").split())


def compose_full_name(first_name, middle_name=None, last_name=None):
    return " ".join(
        name
        for name in (
            normalize_name_part(first_name),
            normalize_name_part(middle_name),
            normalize_name_part(last_name),
        )
        if name
    )


def split_unambiguous_legacy_name(full_name):
    parts = normalize_name_part(full_name).split()
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return None, None, None




def initialize_database():
    server_config = {
        key: value
        for key, value in get_database_config().items()
        if key != "database"
    }

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
        schema = Path(__file__).with_name("schema.sql").read_text(
            encoding="utf-8"
        )

        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                cursor.execute(statement)

        # Fix cart_items foreign key constraint
        database_name = get_database_config()["database"]

        cursor.execute(
            """
            ALTER TABLE cart_items
            MODIFY COLUMN listing_id BIGINT UNSIGNED NULL
            """
        )

        cursor.execute(
            """
            SELECT CONSTRAINT_NAME, DELETE_RULE
            FROM information_schema.REFERENTIAL_CONSTRAINTS
            WHERE CONSTRAINT_SCHEMA = %s
              AND TABLE_NAME = 'cart_items'
              AND CONSTRAINT_NAME = 'fk_cart_items_listing'
            """,
            (database_name,),
        )
        cart_listing_rule = cursor.fetchone()

        if cart_listing_rule and cart_listing_rule[1] != "SET NULL":
            cursor.execute(
                """
                ALTER TABLE cart_items
                DROP FOREIGN KEY fk_cart_items_listing
                """
            )
            cart_listing_rule = None

        if not cart_listing_rule:
            cursor.execute(
                """
                SELECT CONSTRAINT_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE CONSTRAINT_SCHEMA = %s
                  AND TABLE_NAME = 'cart_items'
                  AND COLUMN_NAME = 'listing_id'
                  AND REFERENCED_TABLE_NAME = 'listings'
                """,
                (database_name,),
            )
            existing_listing_fk = cursor.fetchone()

            if not existing_listing_fk:
                cursor.execute(
                    """
                    ALTER TABLE cart_items
                    ADD CONSTRAINT fk_cart_items_listing
                    FOREIGN KEY (listing_id)
                    REFERENCES listings (id)
                    ON DELETE SET NULL
                    """
                )



        # Add image support to messages
        try:
            cursor.execute(
                """
                ALTER TABLE messages
                ADD COLUMN image_url VARCHAR(500) NULL AFTER body
                """
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        # Add profile image support to users
        try:
            cursor.execute(
                """
                ALTER TABLE users
                ADD COLUMN profile_image_url VARCHAR(500) NULL
                AFTER password_hash
                """
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        # Add user profile and account fields
        for statement in (
            "ALTER TABLE users ADD COLUMN cover_image_url VARCHAR(500) NULL AFTER profile_image_url",
            "ALTER TABLE users ADD COLUMN username VARCHAR(32) NULL AFTER cover_image_url",
            "ALTER TABLE users ADD COLUMN first_name VARCHAR(120) NULL AFTER full_name",
            "ALTER TABLE users ADD COLUMN middle_name VARCHAR(120) NULL AFTER first_name",
            "ALTER TABLE users ADD COLUMN last_name VARCHAR(120) NULL AFTER middle_name",
        ):
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                if error.errno != 1060:
                    raise

        cursor.execute(
            "UPDATE users SET username = CONCAT('user', id) WHERE username IS NULL"
        )

        try:
            cursor.execute(
                "ALTER TABLE users ADD UNIQUE KEY uq_users_username (username)"
            )
        except mysql.connector.Error as error:
            if error.errno != 1061:
                raise

        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN bio VARCHAR(500) NULL AFTER profile_image_url"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN location VARCHAR(160) NULL AFTER bio"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user' AFTER location"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        cursor.execute(
            "ALTER TABLE users MODIFY COLUMN role VARCHAR(20) NOT NULL DEFAULT 'buyer'"
        )

        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN account_status VARCHAR(20) NOT NULL DEFAULT 'active' AFTER role"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        cursor.execute(
            "ALTER TABLE users MODIFY COLUMN account_status VARCHAR(32) NOT NULL DEFAULT 'active'"
        )

        for statement in (
            "ALTER TABLE users ADD COLUMN student_number VARCHAR(100) NULL AFTER account_status",
            "ALTER TABLE users ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active' AFTER account_status",
            "ALTER TABLE users ADD COLUMN confirmed_by BIGINT UNSIGNED NULL AFTER status",
            "ALTER TABLE users ADD COLUMN confirmed_at TIMESTAMP NULL DEFAULT NULL AFTER confirmed_by",
            "ALTER TABLE users ADD COLUMN email_verified_at TIMESTAMP NULL DEFAULT NULL AFTER password_hash",
            "ALTER TABLE users ADD COLUMN approval_status VARCHAR(20) NOT NULL DEFAULT 'approved' AFTER account_status",
            "ALTER TABLE users ADD COLUMN approved_by BIGINT UNSIGNED NULL AFTER approval_status",
            "ALTER TABLE users ADD COLUMN approved_at TIMESTAMP NULL DEFAULT NULL AFTER approved_by",
            "ALTER TABLE users ADD COLUMN approval_reviewed_by BIGINT UNSIGNED NULL AFTER approved_at",
            "ALTER TABLE users ADD COLUMN approval_reviewed_at TIMESTAMP NULL DEFAULT NULL AFTER approval_reviewed_by",
            "ALTER TABLE users ADD COLUMN rejection_reason VARCHAR(500) NULL AFTER approval_reviewed_at",
            "ALTER TABLE users ADD COLUMN student_id_number VARCHAR(100) NULL AFTER account_status",
            "ALTER TABLE users ADD COLUMN student_id_image_path VARCHAR(255) NULL AFTER student_id_number",
            "ALTER TABLE users ADD COLUMN verification_submitted_at TIMESTAMP NULL DEFAULT NULL AFTER student_id_image_path",
            "ALTER TABLE users ADD COLUMN verification_reviewed_at TIMESTAMP NULL DEFAULT NULL AFTER verification_submitted_at",
            "ALTER TABLE users ADD COLUMN verification_reviewed_by BIGINT UNSIGNED NULL AFTER verification_reviewed_at",
            "ALTER TABLE users ADD COLUMN verification_rejection_reason VARCHAR(500) NULL AFTER verification_reviewed_by",
            "ALTER TABLE users ADD COLUMN admin_authorization_details VARCHAR(500) NULL AFTER verification_rejection_reason",
            "ALTER TABLE users ADD COLUMN admin_request_submitted_at TIMESTAMP NULL DEFAULT NULL AFTER admin_authorization_details",
            "ALTER TABLE users ADD COLUMN admin_request_status VARCHAR(20) NOT NULL DEFAULT 'none' AFTER admin_request_submitted_at",
            "ALTER TABLE users ADD COLUMN admin_request_reviewed_at TIMESTAMP NULL DEFAULT NULL AFTER admin_request_submitted_at",
            "ALTER TABLE users ADD COLUMN admin_request_reviewed_by BIGINT UNSIGNED NULL AFTER admin_request_reviewed_at",
            "ALTER TABLE users ADD COLUMN admin_request_rejection_reason VARCHAR(500) NULL AFTER admin_request_reviewed_by",
            "ALTER TABLE users ADD COLUMN admin_otp_delivery_status VARCHAR(20) NULL AFTER admin_request_rejection_reason",
        ):
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                if error.errno != 1060:
                    raise

        cursor.execute(
            """
            UPDATE users
            SET admin_request_status = 'pending',
                admin_otp_delivery_status = 'not_sent'
            WHERE admin_request_submitted_at IS NOT NULL
              AND admin_request_status = 'none'
              AND role <> 'admin'
              AND status = 'pending'
              AND account_status = 'pending'
              AND approval_status = 'pending'
            """
        )

        for statement in (
            "ALTER TABLE users ADD UNIQUE KEY uq_users_student_number (student_number)",
            "ALTER TABLE users ADD CONSTRAINT fk_users_confirmed_by FOREIGN KEY (confirmed_by) REFERENCES users (id) ON DELETE SET NULL",
            "ALTER TABLE admin_registration_otps ADD COLUMN account_type VARCHAR(20) NOT NULL DEFAULT 'admin' AFTER password_hash",
            "ALTER TABLE admin_registration_otps ADD COLUMN student_number VARCHAR(100) NULL AFTER account_type",
            "ALTER TABLE admin_registration_otps ADD COLUMN student_id_image_path VARCHAR(255) NULL AFTER student_number",
            "ALTER TABLE admin_registration_otps ADD COLUMN invitation_hash CHAR(64) NULL AFTER student_id_image_path",
            "ALTER TABLE admin_registration_otps ADD COLUMN first_name VARCHAR(120) NULL AFTER full_name",
            "ALTER TABLE admin_registration_otps ADD COLUMN middle_name VARCHAR(120) NULL AFTER first_name",
            "ALTER TABLE admin_registration_otps ADD COLUMN last_name VARCHAR(120) NULL AFTER middle_name",
        ):
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                if error.errno not in {1060, 1061, 1826}:
                    raise

        cursor.execute(
            """
            UPDATE users
            SET first_name = SUBSTRING_INDEX(TRIM(full_name), ' ', 1),
                last_name = SUBSTRING_INDEX(TRIM(full_name), ' ', -1)
            WHERE first_name IS NULL
              AND middle_name IS NULL
              AND last_name IS NULL
              AND TRIM(full_name) REGEXP '^[^[:space:]]+[[:space:]][^[:space:]]+$'
            """
        )

        cursor.execute(
            """
            INSERT IGNORE INTO admin_access_codes
                (target_email, code_hash, status, created_at, expires_at,
                 used_by, used_at)
            SELECT target_email, code_hash,
                CASE WHEN used_at IS NULL THEN 'active' ELSE 'used' END,
                created_at, expires_at, used_by, used_at
            FROM admin_invitations
            """
        )

        try:
            cursor.execute(
                "ALTER TABLE users ADD KEY idx_users_verification_status "
                "(account_status, verification_submitted_at)"
            )
        except mysql.connector.Error as error:
            if error.errno != 1061:
                raise

        try:
            cursor.execute(
                """
                ALTER TABLE users
                ADD CONSTRAINT fk_users_verification_admin
                FOREIGN KEY (verification_reviewed_by)
                REFERENCES users (id) ON DELETE SET NULL
                """
            )
        except mysql.connector.Error as error:
            if error.errno != 1826:
                raise

        for constraint in (
            "ALTER TABLE users ADD CONSTRAINT fk_users_approved_by "
            "FOREIGN KEY (approved_by) REFERENCES users (id) ON DELETE SET NULL",
            "ALTER TABLE users ADD CONSTRAINT fk_users_approval_reviewed_by "
            "FOREIGN KEY (approval_reviewed_by) REFERENCES users (id) ON DELETE SET NULL",
        ):
            try:
                cursor.execute(constraint)
            except mysql.connector.Error as error:
                if error.errno != 1826:
                    raise

        try:
            cursor.execute(
                """
                ALTER TABLE users
                ADD CONSTRAINT fk_users_admin_request_reviewer
                FOREIGN KEY (admin_request_reviewed_by)
                REFERENCES users (id) ON DELETE SET NULL
                """
            )
        except mysql.connector.Error as error:
            if error.errno != 1826:
                raise

        try:
            cursor.execute(
                "ALTER TABLE listings ADD COLUMN user_id BIGINT UNSIGNED NULL AFTER id"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        for column in (
            "ALTER TABLE listings ADD COLUMN quantity INT UNSIGNED NOT NULL DEFAULT 1 AFTER price",
            "ALTER TABLE listings ADD COLUMN is_flagged TINYINT(1) NOT NULL DEFAULT 0 AFTER image_url",
            "ALTER TABLE listings ADD COLUMN is_hidden TINYINT(1) NOT NULL DEFAULT 0 AFTER is_flagged",
        ):
            try:
                cursor.execute(column)
            except mysql.connector.Error as error:
                if error.errno != 1060:
                    raise

        try:
            cursor.execute(
                "ALTER TABLE orders ADD COLUMN payment_method VARCHAR(40) NOT NULL DEFAULT 'Cash on Pick Up' AFTER total_price"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        cursor.execute(
            "ALTER TABLE orders ALTER COLUMN payment_method SET DEFAULT 'Cash on Pick Up'"
        )

        for statement in (
            "ALTER TABLE orders ADD COLUMN quantity INT UNSIGNED NOT NULL DEFAULT 1 AFTER listing_id",
            "ALTER TABLE orders ADD COLUMN stock_deducted TINYINT(1) NOT NULL DEFAULT 0 AFTER quantity",
            "ALTER TABLE orders ADD COLUMN payment_status VARCHAR(32) NOT NULL DEFAULT 'not_required' AFTER payment_method",
            "ALTER TABLE orders ADD COLUMN payment_receipt_path VARCHAR(255) NULL AFTER payment_status",
            "ALTER TABLE orders ADD COLUMN payment_reference_no VARCHAR(100) NULL AFTER payment_receipt_path",
            "ALTER TABLE orders ADD COLUMN payment_submitted_at TIMESTAMP NULL DEFAULT NULL AFTER payment_reference_no",
            "ALTER TABLE orders ADD COLUMN payment_verified_at TIMESTAMP NULL DEFAULT NULL AFTER payment_submitted_at",
            "ALTER TABLE orders ADD COLUMN payment_verified_by BIGINT UNSIGNED NULL AFTER payment_verified_at",
            "ALTER TABLE orders ADD COLUMN payment_rejection_reason VARCHAR(500) NULL AFTER payment_verified_by",
        ):
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                if error.errno != 1060:
                    raise

        try:
            cursor.execute(
                "ALTER TABLE orders ADD KEY idx_orders_seller_payment_status "
                "(seller_id, payment_status)"
            )
        except mysql.connector.Error as error:
            if error.errno != 1061:
                raise

        try:
            cursor.execute(
                """
                ALTER TABLE orders
                ADD CONSTRAINT fk_orders_payment_verified_by
                FOREIGN KEY (payment_verified_by)
                REFERENCES users (id) ON DELETE SET NULL
                """
            )
        except mysql.connector.Error as error:
            if error.errno != 1826:
                raise

        cursor.execute(
            """
            UPDATE orders
            SET payment_status = CASE
                WHEN payment_method IN ('GCash', 'Bank Transfer')
                    AND payment_reference_no IS NOT NULL THEN 'pending_verification'
                WHEN payment_method IN ('GCash', 'Bank Transfer') THEN 'unsubmitted'
                ELSE 'not_required'
            END
            WHERE payment_status = 'not_required'
              AND payment_method IN ('GCash', 'Bank Transfer')
            """
        )

        cursor.execute(
            """
            UPDATE orders
            SET status = CASE status
                WHEN 'pending' THEN 'order_placed'
                WHEN 'paid' THEN 'order_placed'
                WHEN 'shipped' THEN 'preparing_order'
                WHEN 'out_for_delivery' THEN 'ready_for_pickup'
                WHEN 'received' THEN 'completed'
                ELSE status
            END
            WHERE status IN (
                'pending', 'paid', 'shipped', 'out_for_delivery', 'received'
            )
            """
        )

        cursor.execute(
            "ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'order_placed'"
        )

        try:
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN reply_to_id BIGINT UNSIGNED NULL AFTER body"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        try:
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN read_at TIMESTAMP NULL DEFAULT NULL AFTER body"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        try:
            cursor.execute(
                "ALTER TABLE favorites ADD COLUMN user_id BIGINT UNSIGNED NULL AFTER id"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:
                raise

        cursor.execute(
            """
            UPDATE favorites
            SET user_id = (SELECT id FROM users ORDER BY id LIMIT 1)
            WHERE user_id IS NULL
            """
        )

        try:
            cursor.execute(
                "ALTER TABLE favorites DROP FOREIGN KEY fk_favorites_listing"
            )
        except mysql.connector.Error as error:
            if error.errno != 1091:
                raise

        try:
            cursor.execute(
                "ALTER TABLE favorites DROP INDEX uq_favorites_listing"
            )
        except mysql.connector.Error as error:
            if error.errno != 1091:
                raise

        try:
            cursor.execute(
                """
                ALTER TABLE favorites
                ADD UNIQUE KEY uq_favorites_user_listing (user_id, listing_id)
                """
            )
        except mysql.connector.Error as error:
            if error.errno != 1061:
                raise

        try:
            cursor.execute(
                """
                ALTER TABLE favorites
                ADD CONSTRAINT fk_favorites_listing
                FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE CASCADE
                """
            )
        except mysql.connector.Error as error:
            if error.errno != 1826:
                raise

        try:
            cursor.execute(
                """
                ALTER TABLE favorites
                ADD CONSTRAINT fk_favorites_user
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                """
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


def create_user(
    full_name,
    email,
    password_hash,
    student_id_number=None,
    student_id_image_path=None,
    student_number=None,
    first_name=None,
    middle_name=None,
    last_name=None,
):
    if first_name is None and last_name is None:
        first_name, middle_name, last_name = split_unambiguous_legacy_name(full_name)
    composed_name = compose_full_name(first_name, middle_name, last_name)
    if composed_name:
        full_name = composed_name
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO users
                    (full_name, first_name, middle_name, last_name,
                     email, password_hash, role, account_status,
                     student_number, status, student_id_number,
                     student_id_image_path, verification_submitted_at,
                     admin_authorization_details, admin_request_submitted_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'buyer', 'pending_verification',
                        %s, 'pending', %s, %s, CURRENT_TIMESTAMP, NULL, NULL)
                """,
                (
                    full_name,
                    first_name,
                    middle_name,
                    last_name,
                    email,
                    password_hash,
                    student_number,
                    student_id_number,
                    student_id_image_path,
                ),
            )
            user_id = cursor.lastrowid
            cursor.execute(
                "UPDATE users SET username = CONCAT('user', id) WHERE id = %s AND username IS NULL",
                (user_id,),
            )
            connection.commit()
        finally:
            cursor.close()


def create_admin_access_code(code_hash, now, expires_at=None):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO admin_access_codes
                    (code_hash, status, created_at, expires_at)
                VALUES (%s, 'active', %s, %s)
                """,
                (code_hash, now, expires_at),
            )
            connection.commit()
            return int(cursor.lastrowid)
        finally:
            cursor.close()


def create_pending_admin_registration(
    full_name, first_name, middle_name, last_name, email, password_hash
):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT email
                FROM admin_registration_otps
                WHERE email = %s
                FOR UPDATE
                """,
                (email,),
            )
            if cursor.fetchone() is not None:
                connection.rollback()
                return None
            cursor.execute(
                """
                INSERT INTO users
                    (full_name, first_name, middle_name, last_name, email,
                     password_hash, role, account_status, status,
                     approval_status, admin_request_submitted_at,
                     admin_request_status, admin_otp_delivery_status)
                VALUES (%s, %s, %s, %s, %s, %s, 'buyer', 'pending', 'pending',
                        'pending', CURRENT_TIMESTAMP, 'pending', 'not_sent')
                """,
                (
                    full_name,
                    first_name,
                    middle_name,
                    last_name,
                    email,
                    password_hash,
                ),
            )
            user_id = cursor.lastrowid
            cursor.execute(
                "UPDATE users SET username = CONCAT('user', id) WHERE id = %s",
                (user_id,),
            )
            connection.commit()
            return user_id
        finally:
            cursor.close()


def find_valid_admin_access_code(code_hash, target_email, now):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT id
                FROM admin_access_codes
                WHERE code_hash = %s
                  AND (target_email IS NULL OR target_email = %s)
                  AND (expires_at IS NULL OR expires_at > %s)
                  AND status = 'active'
                  AND revoked_at IS NULL
                  AND used_at IS NULL
                """,
                (code_hash, target_email, now),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()


def list_admin_access_codes():
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, status, created_at, expires_at, revoked_at, used_at, used_by
                FROM admin_access_codes
                ORDER BY id DESC
                """
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def revoke_admin_access_code(code_id, now):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE admin_access_codes
                SET status = 'revoked', revoked_at = %s
                WHERE id = %s AND status = 'active'
                  AND revoked_at IS NULL AND used_at IS NULL
                """,
                (now, code_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            cursor.close()


def allow_admin_access_code_attempt(client_ip, now, limit=10, window_seconds=3600):
    with _admin_access_attempts_lock:
        expired_ips = [
            ip
            for ip, attempts in _admin_access_attempts.items()
            if not attempts or now - attempts[-1] >= window_seconds
        ]
        for ip in expired_ips:
            del _admin_access_attempts[ip]
        attempts = _admin_access_attempts.setdefault(client_ip or "unknown", [])
        attempts[:] = [attempt for attempt in attempts if now - attempt < window_seconds]
        if len(attempts) >= limit:
            return max(1, window_seconds - (now - attempts[0]))
        attempts.append(now)
        return 0


def issue_admin_registration_otp(
    email,
    otp_hash,
    now,
    expires_at,
    full_name=None,
    password_hash=None,
    account_type="admin",
    student_number=None,
    student_id_image_path=None,
    invitation_hash=None,
    first_name=None,
    middle_name=None,
    last_name=None,
):
    if first_name is None and last_name is None:
        first_name, middle_name, last_name = split_unambiguous_legacy_name(full_name)
    composed_name = compose_full_name(first_name, middle_name, last_name)
    if composed_name:
        full_name = composed_name
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            if full_name is None or password_hash is None:
                cursor.execute(
                    "SELECT email FROM admin_registration_otps WHERE email = %s",
                    (email,),
                )
                if cursor.fetchone() is None:
                    return "not_found", 0
            else:
                cursor.execute(
                    """
                    INSERT IGNORE INTO admin_registration_otps
                        (email, full_name, first_name, middle_name, last_name,
                         password_hash, account_type,
                         student_number, student_id_image_path, invitation_hash,
                         otp_hash, expires_at,
                         attempt_count, send_count, window_started_at, last_sent_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            0, 0, %s, 0)
                    """,
                    (
                        email,
                        full_name,
                        first_name,
                        middle_name,
                        last_name,
                        password_hash,
                        account_type,
                        student_number,
                        student_id_image_path,
                        invitation_hash,
                        otp_hash,
                        expires_at,
                        now,
                    ),
                )

            cursor.execute(
                "SELECT * FROM admin_registration_otps WHERE email = %s FOR UPDATE",
                (email,),
            )
            record = cursor.fetchone()
            if record is None:
                connection.rollback()
                return "not_found", 0

            registration_type = account_type if full_name is not None else record.get("account_type")
            if registration_type == "admin":
                cursor.execute(
                    """
                    SELECT id
                    FROM users
                    WHERE email = %s
                      AND role <> 'admin'
                      AND status = 'pending'
                      AND account_status = 'pending'
                      AND approval_status = 'pending'
                      AND admin_request_status = 'approved'
                      AND admin_request_reviewed_by IS NOT NULL
                      AND admin_request_reviewed_at IS NOT NULL
                    FOR UPDATE
                    """,
                    (email,),
                )
                if cursor.fetchone() is None:
                    connection.rollback()
                    return "not_approved", 0

            if now - int(record["window_started_at"]) >= 3600:
                record["send_count"] = 0
                record["window_started_at"] = now
            if int(record["send_count"]) >= 5:
                connection.rollback()
                return "rate_limited", 0
            last_sent_at = int(record["last_sent_at"])
            if last_sent_at and now - last_sent_at < 60:
                connection.rollback()
                return "cooldown", 60 - (now - last_sent_at)

            if full_name is not None and password_hash is not None:
                cursor.execute(
                    """
                    UPDATE admin_registration_otps
                    SET full_name = %s, first_name = %s, middle_name = %s,
                        last_name = %s, password_hash = %s,
                        account_type = %s, student_number = %s,
                        student_id_image_path = %s, invitation_hash = %s
                    WHERE email = %s
                    """,
                    (
                        full_name,
                        first_name,
                        middle_name,
                        last_name,
                        password_hash,
                        account_type,
                        student_number,
                        student_id_image_path,
                        invitation_hash,
                        email,
                    ),
                )
            cursor.execute(
                """
                UPDATE admin_registration_otps
                SET otp_hash = %s, expires_at = %s, attempt_count = 0,
                    send_count = %s, window_started_at = %s, last_sent_at = %s
                WHERE email = %s
                """,
                (
                    otp_hash,
                    expires_at,
                    int(record["send_count"]) + 1,
                    record["window_started_at"],
                    now,
                    email,
                ),
            )
            connection.commit()
            return "sent", 0
        finally:
            cursor.close()


def complete_admin_registration(email, otp_hash, now):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT * FROM admin_registration_otps WHERE email = %s FOR UPDATE",
                (email,),
            )
            record = cursor.fetchone()
            if record is None:
                return "not_found", None
            if int(record["attempt_count"]) >= 5:
                connection.rollback()
                return "locked", None
            if int(record["expires_at"]) <= now:
                connection.rollback()
                return "expired", None
            if not hmac.compare_digest(record["otp_hash"], otp_hash):
                attempts = int(record["attempt_count"]) + 1
                cursor.execute(
                    "UPDATE admin_registration_otps SET attempt_count = %s WHERE email = %s",
                    (attempts, email),
                )
                connection.commit()
                return ("locked" if attempts >= 5 else "invalid"), None

            account_type = record.get("account_type")
            if account_type == "admin":
                cursor.execute(
                    """
                    SELECT id
                    FROM users
                    WHERE email = %s
                      AND role <> 'admin'
                      AND status = 'pending'
                      AND account_status = 'pending'
                      AND approval_status = 'pending'
                      AND admin_request_status = 'approved'
                      AND admin_request_reviewed_by IS NOT NULL
                      AND admin_request_reviewed_at IS NOT NULL
                    FOR UPDATE
                    """,
                    (email,),
                )
                approved_application = cursor.fetchone()
                if approved_application is None:
                    connection.rollback()
                    return "not_approved", None

            cursor.execute(
                """
                SELECT id, role, account_status, status, approval_status,
                       admin_request_status
                FROM users WHERE email = %s FOR UPDATE
                """,
                (email,),
            )
            existing_user = cursor.fetchone()
            if account_type == "admin":
                if (
                    existing_user is None
                    or existing_user["role"] == "admin"
                    or existing_user["status"] != "pending"
                    or existing_user["account_status"] != "pending"
                    or existing_user["approval_status"] != "pending"
                    or existing_user["admin_request_status"] != "approved"
                ):
                    connection.rollback()
                    return "not_approved", None
                cursor.execute(
                    """
                    UPDATE users
                    SET role = 'admin',
                        status = 'active',
                        account_status = 'active',
                        approval_status = 'approved',
                        admin_request_status = 'verified',
                        admin_otp_delivery_status = 'verified',
                        email_verified_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND role <> 'admin'
                      AND status = 'pending'
                      AND account_status = 'pending'
                      AND approval_status = 'pending'
                      AND admin_request_status = 'approved'
                    """,
                    (existing_user["id"],),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return "not_approved", None
                user_id = existing_user["id"]
            elif existing_user is None:
                role = "buyer"
                account_status = "pending"
                approval_status = "pending"
                first_name = record.get("first_name")
                middle_name = record.get("middle_name")
                last_name = record.get("last_name")
                if not first_name or not last_name:
                    first_name, middle_name, last_name = split_unambiguous_legacy_name(
                        record["full_name"]
                    )
                cursor.execute(
                    """
                    INSERT INTO users
                        (full_name, first_name, middle_name, last_name,
                         email, password_hash, role, account_status,
                         status, student_number, student_id_number,
                         student_id_image_path, verification_submitted_at,
                         approval_status, email_verified_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s,
                            CASE WHEN %s IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END,
                            %s, CURRENT_TIMESTAMP)
                    """,
                    (
                        record["full_name"],
                        first_name,
                        middle_name,
                        last_name,
                        email,
                        record["password_hash"],
                        role,
                        account_status,
                        account_status,
                        record.get("student_number"),
                        record.get("student_number") if account_type == "student" else None,
                        record.get("student_id_image_path"),
                        record.get("student_id_image_path"),
                        approval_status,
                    ),
                )
                user_id = cursor.lastrowid
                cursor.execute(
                    "UPDATE users SET username = CONCAT('user', id) WHERE id = %s",
                    (user_id,),
                )
            elif (
                existing_user["role"] in {"buyer", "seller", "student", "user"}
                and existing_user["account_status"] == "rejected"
                and record.get("account_type") == "student"
            ):
                first_name = record.get("first_name")
                middle_name = record.get("middle_name")
                last_name = record.get("last_name")
                if not first_name or not last_name:
                    first_name, middle_name, last_name = split_unambiguous_legacy_name(
                        record["full_name"]
                    )
                cursor.execute(
                    """
                    UPDATE users
                    SET full_name = %s, first_name = %s, middle_name = %s,
                        last_name = %s,
                        password_hash = %s,
                        account_status = 'pending',
                        approval_status = 'pending',
                        approved_by = NULL,
                        approved_at = NULL,
                        student_number = %s,
                        student_id_number = %s,
                        student_id_image_path = %s,
                        verification_submitted_at = CURRENT_TIMESTAMP,
                        verification_reviewed_at = NULL,
                        verification_reviewed_by = NULL,
                        verification_rejection_reason = NULL,
                        status = 'pending',
                        confirmed_by = NULL,
                        confirmed_at = NULL,
                        email_verified_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND role IN ('buyer', 'seller', 'student', 'user')
                      AND account_status = 'rejected'
                      AND status != 'pending'
                    """,
                    (
                        record["full_name"],
                        first_name,
                        middle_name,
                        last_name,
                        record["password_hash"],
                        record.get("student_number"),
                        record.get("student_number"),
                        record.get("student_id_image_path"),
                        existing_user["id"],
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return "email_exists", None
                user_id = existing_user["id"]
            else:
                connection.rollback()
                return "email_exists", None
            cursor.execute(
                "DELETE FROM admin_registration_otps WHERE email = %s",
                (email,),
            )
            connection.commit()
            if account_type == "admin":
                return "verified_admin", user_id
            return "verified", user_id
        except mysql.connector.IntegrityError:
            connection.rollback()
            return "registration_conflict", None
        except mysql.connector.Error:
            connection.rollback()
            raise
        finally:
            cursor.close()


def invalidate_admin_registration_otp(email, otp_hash):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE admin_registration_otps
                SET expires_at = 0, attempt_count = 5
                WHERE email = %s AND otp_hash = %s
                """,
                (email, otp_hash),
            )
            connection.commit()
        finally:
            cursor.close()


def find_pending_accounts():
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, full_name, email, student_number, role,
                       CASE WHEN admin_request_submitted_at IS NOT NULL THEN 'Admin'
                            WHEN student_number IS NOT NULL THEN 'Student'
                            ELSE 'Buyer' END AS account_type,
                       created_at,
                       CASE WHEN admin_request_submitted_at IS NOT NULL
                            THEN admin_request_status ELSE status END AS status,
                       admin_request_status, admin_otp_delivery_status
                FROM users
                WHERE role <> 'admin'
                  AND (
                    (admin_request_submitted_at IS NOT NULL
                     AND admin_request_status IN ('pending', 'approved')
                     AND status = 'pending')
                    OR
                    (admin_request_submitted_at IS NULL
                     AND status = 'pending'
                     AND email_verified_at IS NOT NULL)
                  )
                ORDER BY created_at ASC, id ASC
                """
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def claim_admin_registration_otp_delivery(user_id, admin_id, expected_status):
    if expected_status not in {"not_sent", "failed"}:
        raise ValueError("Invalid OTP delivery state.")
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT applicant.id, applicant.email, applicant.full_name,
                       applicant.first_name, applicant.middle_name,
                       applicant.last_name, applicant.password_hash
                FROM users AS applicant
                JOIN users AS reviewer
                  ON reviewer.id = %s
                 AND reviewer.role = 'admin'
                 AND reviewer.status = 'active'
                 AND reviewer.account_status = 'active'
                 AND reviewer.approval_status = 'approved'
                WHERE applicant.id = %s
                  AND applicant.id <> reviewer.id
                  AND applicant.role <> 'admin'
                  AND applicant.status = 'pending'
                  AND applicant.account_status = 'pending'
                  AND applicant.approval_status = 'pending'
                  AND applicant.admin_request_status = 'approved'
                  AND applicant.admin_otp_delivery_status = %s
                FOR UPDATE
                """,
                (admin_id, user_id, expected_status),
            )
            applicant = cursor.fetchone()
            if applicant is None:
                connection.rollback()
                return None
            cursor.execute(
                """
                UPDATE users
                SET admin_otp_delivery_status = 'sending'
                WHERE id = %s
                  AND admin_request_status = 'approved'
                  AND admin_otp_delivery_status = %s
                """,
                (user_id, expected_status),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            return applicant
        finally:
            cursor.close()


def update_admin_otp_delivery_status(email, status, expected_status="sending"):
    if status not in {"sent", "failed"} or expected_status not in {
        "not_sent",
        "sending",
        "sent",
        "failed",
    }:
        raise ValueError("Invalid OTP delivery state.")
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE users
                SET admin_otp_delivery_status = %s
                WHERE email = %s
                  AND role <> 'admin'
                  AND status = 'pending'
                  AND account_status = 'pending'
                  AND approval_status = 'pending'
                  AND admin_request_status = 'approved'
                  AND admin_otp_delivery_status = %s
                """,
                (status, email, expected_status),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            cursor.close()


def find_user_by_student_number(student_number):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, email
                FROM users
                WHERE student_number = %s OR student_id_number = %s
                LIMIT 1
                """,
                (student_number, student_number),
            )
            return cursor.fetchone()
        finally:
            cursor.close()


def review_pending_account(user_id, admin_id, decision):
    if decision not in {"approved", "confirmed", "rejected"}:
        raise ValueError("Invalid account decision.")
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id
                FROM users
                WHERE id = %s
                  AND role = 'admin'
                  AND account_status = 'active'
                  AND status = 'active'
                  AND approval_status = 'approved'
                FOR UPDATE
                """,
                (admin_id,),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                return False

            cursor.execute(
                """
                SELECT id, admin_request_submitted_at, admin_request_status
                FROM users
                WHERE id = %s
                FOR UPDATE
                """,
                (user_id,),
            )
            applicant = cursor.fetchone()
            if applicant is None:
                connection.rollback()
                return False
            if applicant["admin_request_submitted_at"] is not None:
                if (
                    int(user_id) == int(admin_id)
                    or applicant["admin_request_status"] != "pending"
                ):
                    connection.rollback()
                    return False
                if decision in {"approved", "confirmed"}:
                    cursor.execute(
                        """
                        UPDATE users
                        SET admin_request_status = 'approved',
                            admin_request_reviewed_by = %s,
                            admin_request_reviewed_at = CURRENT_TIMESTAMP,
                            admin_request_rejection_reason = NULL,
                            admin_otp_delivery_status = 'not_sent',
                            approved_by = %s,
                            approved_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                          AND role <> 'admin'
                          AND status = 'pending'
                          AND approval_status = 'pending'
                          AND admin_request_status = 'pending'
                        """,
                        (admin_id, admin_id, user_id),
                    )
                    result = "admin_approved"
                else:
                    cursor.execute(
                        """
                        UPDATE users
                        SET admin_request_status = 'rejected',
                            admin_request_reviewed_by = %s,
                            admin_request_reviewed_at = CURRENT_TIMESTAMP,
                            admin_request_rejection_reason =
                                'Your Admin registration has not been approved.',
                            admin_otp_delivery_status = NULL,
                            status = 'rejected',
                            account_status = 'rejected',
                            approval_status = 'rejected',
                            approved_by = NULL,
                            approved_at = NULL,
                            rejection_reason =
                                'Your Admin registration has not been approved.'
                        WHERE id = %s
                          AND role <> 'admin'
                          AND status = 'pending'
                          AND approval_status = 'pending'
                          AND admin_request_status = 'pending'
                        """,
                        (admin_id, user_id),
                    )
                    result = "admin_rejected"
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return result

            if decision in {"approved", "confirmed"}:
                cursor.execute(
                    """
                    UPDATE users
                    SET status = 'active',
                        account_status = 'active',
                        approval_status = 'approved',
                        confirmed_by = %s,
                        confirmed_at = CURRENT_TIMESTAMP,
                        approved_by = %s,
                        approved_at = CURRENT_TIMESTAMP,
                        approval_reviewed_by = %s,
                        approval_reviewed_at = CURRENT_TIMESTAMP,
                        rejection_reason = NULL
                    WHERE id = %s AND role <> 'admin'
                      AND status = 'pending'
                      AND admin_request_submitted_at IS NULL
                    """,
                    (admin_id, admin_id, admin_id, user_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE users
                    SET status = 'rejected',
                        account_status = 'rejected',
                        approval_status = 'rejected',
                        approved_by = NULL,
                        approved_at = NULL,
                        approval_reviewed_by = %s,
                        approval_reviewed_at = CURRENT_TIMESTAMP,
                        rejection_reason = 'Your account has not been approved by the administrator.'
                    WHERE id = %s AND role <> 'admin'
                      AND status = 'pending'
                      AND admin_request_submitted_at IS NULL
                    """,
                    (admin_id, user_id),
                )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
            return "student_approved" if decision in {"approved", "confirmed"} else "student_rejected"
        finally:
            cursor.close()


def resubmit_student_verification(user_id, student_id_number, student_id_image_path):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT student_id_image_path FROM users WHERE id = %s AND account_status = 'rejected'",
                (user_id,),
            )
            user = cursor.fetchone()
            if user is None:
                return False, None
            old_image_path = user["student_id_image_path"]
            cursor.execute(
                """
                UPDATE users
                SET account_status = 'pending_verification',
                    student_id_number = %s,
                    student_id_image_path = %s,
                    verification_submitted_at = CURRENT_TIMESTAMP,
                    verification_reviewed_at = NULL,
                    verification_reviewed_by = NULL,
                    verification_rejection_reason = NULL
                WHERE id = %s AND account_status = 'rejected'
                """,
                (student_id_number, student_id_image_path, user_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False, None
            connection.commit()
            return True, old_image_path
        finally:
            cursor.close()


def find_pending_student_verifications():
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, full_name, email, student_id_number,
                       verification_submitted_at
                FROM users
                WHERE account_status = 'pending_verification'
                   OR (account_status = 'pending' AND status = 'pending'
                       AND student_id_image_path IS NOT NULL)
                ORDER BY verification_submitted_at, id
                """
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def find_student_verification_image(user_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT student_id_image_path
                FROM users
                WHERE id = %s
                  AND (account_status = 'pending_verification'
                       OR (account_status = 'pending' AND status = 'pending'
                           AND student_id_image_path IS NOT NULL))
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()


def review_student_verification(user_id, admin_id, status, rejection_reason):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE users
                SET account_status = %s,
                    status = CASE
                        WHEN status = 'pending' AND %s = 'rejected' THEN 'rejected'
                        ELSE status
                    END,
                    verification_reviewed_at = CURRENT_TIMESTAMP,
                    verification_reviewed_by = %s,
                    verification_rejection_reason = %s
                WHERE id = %s
                  AND role IN ('buyer', 'seller', 'student', 'user')
                  AND (account_status = 'pending_verification'
                       OR (account_status = 'pending' AND status = 'pending'
                           AND student_id_image_path IS NOT NULL))
                """,
                (
                    status,
                    status,
                    admin_id,
                    rejection_reason or None,
                    user_id,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            cursor.close()


def find_user_by_email(email):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, full_name, email, password_hash, role, account_status,
                       student_number, status,
                       approval_status, rejection_reason,
                       verification_rejection_reason, admin_request_rejection_reason,
                       admin_request_submitted_at, admin_request_status,
                       admin_request_reviewed_at, admin_request_reviewed_by,
                       admin_otp_delivery_status
                FROM users WHERE email = %s
                """,
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
                f"""
                SELECT u.id, u.full_name, u.email, u.profile_image_url, u.cover_image_url,
                       u.username, u.bio, u.location, u.role, u.account_status,
                       u.student_number, u.status, u.confirmed_by, u.confirmed_at,
                       u.approval_status, u.rejection_reason,
                       u.verification_rejection_reason,
                       u.email_verified_at, u.created_at,
                       u.verification_submitted_at, u.verification_reviewed_at,
                       {_verified_account_sql("u")} AS verified
                FROM users u WHERE u.id = %s
                """,
                (user_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()


def find_public_profile(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT u.id, u.full_name, u.profile_image_url, u.created_at,
                       {_verified_account_sql("u")} AS verified,
                       (
                         SELECT COUNT(*)
                         FROM listings l
                         WHERE l.user_id = u.id
                       ) AS post_count,
                       (
                         SELECT COUNT(*)
                         FROM orders o
                         WHERE o.seller_id = u.id
                           AND o.status = 'completed'
                           AND o.payment_status IN ('not_required', 'verified')
                       ) AS completed_transaction_count,
                       (
                         SELECT AVG(r.rating)
                         FROM ratings r
                         WHERE r.reviewed_user_id = u.id
                       ) AS average_rating,
                       (
                         SELECT COUNT(*)
                         FROM ratings r
                         WHERE r.reviewed_user_id = u.id
                       ) AS rating_count
                FROM users u
                WHERE u.id = %s
                  AND u.status = 'active'
                  AND u.account_status IN ('active', 'verified')
                """,
                (user_id,),
            )
            profile = cursor.fetchone()
            if profile is None:
                return None
            cursor.execute(
                """
                SELECT l.id, l.title, l.price, l.item_condition,
                       l.image_url, l.quantity, l.created_at
                FROM listings l
                WHERE l.user_id = %s AND l.is_hidden = 0
                ORDER BY l.created_at DESC, l.id DESC
                """,
                (user_id,),
            )
            profile["listings"] = cursor.fetchall()
            cursor.execute(
                """
                SELECT r.rating, r.review, r.created_at
                FROM ratings r
                WHERE r.reviewed_user_id = %s
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT 10
                """,
                (user_id,),
            )
            profile["reviews"] = cursor.fetchall()
            return profile
        finally:
            cursor.close()


def find_public_user_identity(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT u.id, u.full_name, u.profile_image_url,
                       {_verified_account_sql("u")} AS verified
                FROM users u
                WHERE u.id = %s
                """,
                (user_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()


def create_transaction_rating(reviewer_id, transaction_id, rating, review):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT buyer_id, seller_id, status, payment_status
                FROM orders
                WHERE id = %s
                FOR UPDATE
                """,
                (transaction_id,),
            )
            transaction = cursor.fetchone()
            if (
                transaction is None
                or int(transaction["buyer_id"]) != int(reviewer_id)
                or int(transaction["seller_id"]) == int(reviewer_id)
            ):
                return False, "A completed order by your account is required to rate this seller."
            if (
                transaction["status"] != "completed"
                or transaction["payment_status"] not in {"not_required", "verified"}
            ):
                return False, "You can rate the seller after the order is completed."
            cursor.execute(
                "SELECT 1 FROM ratings WHERE transaction_id = %s",
                (transaction_id,),
            )
            if cursor.fetchone() is not None:
                return False, "You have already rated this transaction."
            cursor.execute(
                """
                INSERT INTO ratings
                    (transaction_id, reviewer_id, reviewed_user_id, rating, review)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    transaction_id,
                    reviewer_id,
                    transaction["seller_id"],
                    rating,
                    review,
                ),
            )
            connection.commit()
            cursor.execute(
                "SELECT full_name FROM users WHERE id = %s",
                (reviewer_id,),
            )
            reviewer = cursor.fetchone()
            if reviewer:
                create_notification(
                    int(transaction["seller_id"]),
                    "rating",
                    "New Rating",
                    f"{reviewer[0]} gave you a {int(rating)}-star rating.",
                    "rating",
                    int(transaction_id),
                    f"/users/{int(transaction['seller_id'])}",
                    reviewer_id,
                )
            return True, None
        finally:
            cursor.close()


def update_user_profile(
    user_id, full_name, bio, location, username,
    profile_image_url=None, cover_image_url=None
):
    full_name = normalize_name_part(full_name)
    first_name, middle_name, last_name = split_unambiguous_legacy_name(full_name)
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT full_name FROM users WHERE id = %s FOR UPDATE", (user_id,))
            current_user = cursor.fetchone()
            if current_user is None:
                connection.rollback()
                return False
            if normalize_name_part(current_user[0]) == full_name:
                cursor.execute(
                    """
                    UPDATE users
                    SET bio = %s, location = %s, username = %s,
                        profile_image_url = COALESCE(%s, profile_image_url),
                        cover_image_url = COALESCE(%s, cover_image_url)
                    WHERE id = %s
                    """,
                    (
                        bio or None, location or None, username,
                        profile_image_url, cover_image_url, user_id,
                    ),
                )
                connection.commit()
                return cursor.rowcount > 0
            cursor.execute(
                """
                UPDATE users
                SET full_name = %s, first_name = %s, middle_name = %s,
                    last_name = %s, bio = %s, location = %s,
                    username = %s,
                    profile_image_url = COALESCE(%s, profile_image_url),
                    cover_image_url = COALESCE(%s, cover_image_url)
                WHERE id = %s
                """,
                (
                    full_name, first_name, middle_name, last_name,
                    bio or None, location or None, username,
                    profile_image_url, cover_image_url, user_id,
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
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


def create_listing(user_id, title, category, item_condition, description, price, quantity, image_url=""):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO listings
                    (user_id, title, category, item_condition, description, price, quantity, image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, title, category, item_condition, description or "", price, quantity, image_url or None),
            )
            connection.commit()
            return cursor.lastrowid
        finally:
            cursor.close()


def update_listing(user_id, listing_id, title, category, item_condition, description, price, quantity, image_url=None):
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
                    description = %s, price = %s, quantity = %s,
                    image_url = COALESCE(%s, image_url)
                WHERE id = %s
                """,
                (title, category, item_condition, description or "", price, quantity, image_url, listing_id),
            )
            connection.commit()
            return True
        finally:
            cursor.close()


def find_listings(user_id=None):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            query = f"""
                SELECT l.id, l.user_id AS seller_id, l.title, l.category, l.item_condition,
                      l.description, l.price, l.quantity, l.image_url, l.created_at,
                      COALESCE(u.full_name, 'NCart seller') AS seller_name,
                      u.profile_image_url AS seller_image_url,
                      u.bio AS seller_bio,
                      u.location AS seller_location,
                      {_verified_account_sql("u")} AS seller_verified
                  FROM listings l
                  LEFT JOIN users u ON u.id = l.user_id
            """
            parameters = ()
            query += " WHERE l.is_hidden = 0"
            if user_id is not None:
                query += " AND l.user_id = %s"
                parameters = (user_id,)
            query += " ORDER BY created_at DESC, id DESC"
            cursor.execute(query, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()


def find_marketplace_listings(page, page_size=6, category="", search=""):
    conditions = ["l.is_hidden = 0"]
    parameters = []
    normalized_category = str(category or "").strip()
    normalized_search = str(search or "").strip()
    if normalized_category:
        conditions.append("LOWER(l.category) = LOWER(%s)")
        parameters.append(normalized_category)
    if normalized_search:
        escaped_search = (
            normalized_search.replace("!", "!!")
            .replace("%", "!%")
            .replace("_", "!_")
        )
        search_pattern = f"%{escaped_search}%"
        conditions.append(
            """
            (l.title LIKE %s ESCAPE '!' OR l.category LIKE %s ESCAPE '!'
             OR l.item_condition LIKE %s ESCAPE '!' OR l.description LIKE %s ESCAPE '!'
             OR u.full_name LIKE %s ESCAPE '!')
            """
        )
        parameters.extend([search_pattern] * 5)
    where_clause = " AND ".join(conditions)
    offset = (page - 1) * page_size
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total_count
                FROM listings l
                LEFT JOIN users u ON u.id = l.user_id
                WHERE {where_clause}
                """,
                tuple(parameters),
            )
            total_count = int(cursor.fetchone()["total_count"])
            cursor.execute(
                f"""
                SELECT l.id, l.user_id AS seller_id, l.title, l.category, l.item_condition,
                       l.description, l.price, l.quantity, l.image_url, l.created_at,
                       COALESCE(u.full_name, 'NCart seller') AS seller_name,
                       u.profile_image_url AS seller_image_url,
                       u.bio AS seller_bio,
                       u.location AS seller_location,
                       {_verified_account_sql("u")} AS seller_verified
                FROM listings l
                LEFT JOIN users u ON u.id = l.user_id
                WHERE {where_clause}
                ORDER BY l.created_at DESC, l.id DESC
                LIMIT %s OFFSET %s
                """,
                (*parameters, page_size, offset),
            )
            listings = cursor.fetchall()
            cursor.execute(
                """
                SELECT DISTINCT category
                FROM listings
                WHERE is_hidden = 0 AND category <> ''
                ORDER BY category
                """
            )
            categories = [row["category"] for row in cursor.fetchall()]
            return listings, total_count, categories
        finally:
            cursor.close()


def find_marketplace_listing_by_id(listing_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT l.id, l.user_id AS seller_id, l.title, l.category,
                       l.item_condition, l.description, l.price, l.quantity, l.image_url,
                       l.created_at,
                       COALESCE(u.full_name, 'NCart seller') AS seller_name,
                       u.profile_image_url AS seller_image_url,
                       u.bio AS seller_bio,
                       u.location AS seller_location,
                       {_verified_account_sql("u")} AS seller_verified
                FROM listings l
                LEFT JOIN users u ON u.id = l.user_id
                WHERE l.id = %s AND l.is_hidden = 0
                """,
                (listing_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()


def create_report(reporter_id, listing_id, reason, description):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT l.id, l.user_id, l.title, l.category, l.item_condition,
                       l.description, l.price, l.image_url, l.created_at, l.is_hidden
                FROM listings l
                WHERE l.id = %s
                """,
                (listing_id,),
            )
            listing = cursor.fetchone()
            if listing is None or listing["is_hidden"]:
                return None, "This listing is no longer available."
            if int(listing["user_id"]) == int(reporter_id):
                return None, "You cannot report your own listing."
            cursor.execute(
                """
                INSERT INTO reports
                    (reporter_id, reported_user_id, listing_id, listing_title,
                     listing_image_url, listing_category, listing_condition,
                     listing_description, listing_price, listing_created_at,
                     reason, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    reporter_id, listing["user_id"], listing["id"], listing["title"],
                    listing["image_url"], listing["category"], listing["item_condition"],
                    listing["description"], listing["price"], listing["created_at"],
                    reason, description,
                ),
            )
            connection.commit()
            return cursor.lastrowid, None
        finally:
            cursor.close()


def find_reports_for_user(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, listing_title, reason, description, status,
                       created_at, reviewed_at
                FROM reports
                WHERE reporter_id = %s
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def find_admin_reports(status="", search="", report_date="", reason="", limit=None):
    conditions = []
    parameters = []
    if status:
        conditions.append("r.status = %s")
        parameters.append(status)
    if report_date:
        conditions.append("DATE(r.created_at) = %s")
        parameters.append(report_date)
    if reason:
        conditions.append("r.reason = %s")
        parameters.append(reason)
    if search:
        conditions.append(
            """
            (CAST(r.id AS CHAR) LIKE %s OR reporter.full_name LIKE %s
             OR reported.full_name LIKE %s OR r.listing_title LIKE %s)
            """
        )
        like = f"%{search}%"
        parameters.extend((like, like, like, like))
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT %s"
        parameters.append(limit)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT r.id, r.reporter_id, r.reported_user_id, r.listing_id,
                       r.listing_title, r.reason, r.description, r.status,
                       r.admin_decision, r.admin_note, r.action_taken,
                       r.created_at, r.reviewed_at,
                       reporter.full_name AS reporter_name,
                       reported.full_name AS reported_user_name
                FROM reports r
                LEFT JOIN users reporter ON reporter.id = r.reporter_id
                LEFT JOIN users reported ON reported.id = r.reported_user_id
                {where_clause}
                ORDER BY r.created_at DESC, r.id DESC
                {limit_clause}
                """,
                tuple(parameters),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def find_admin_report_detail(report_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT r.*,
                       reporter.full_name AS reporter_name,
                       reporter.email AS reporter_email,
                       reporter.profile_image_url AS reporter_profile_image_url,
                       reporter.bio AS reporter_bio,
                       reporter.location AS reporter_location,
                       reported.full_name AS reported_user_name,
                       reported.email AS reported_user_email,
                       reported.profile_image_url AS reported_user_profile_image_url,
                       reported.bio AS reported_user_bio,
                       reported.location AS reported_user_location,
                       reported.account_status AS reported_user_account_status,
                       reviewer.full_name AS reviewing_admin_name,
                       listing.title AS current_listing_title,
                       listing.image_url AS current_listing_image_url,
                       listing.price AS current_listing_price,
                       listing.category AS current_listing_category,
                       listing.item_condition AS current_listing_condition,
                       listing.description AS current_listing_description,
                       listing.created_at AS current_listing_created_at,
                       listing.is_flagged AS current_listing_is_flagged,
                       listing.is_hidden AS current_listing_is_hidden,
                       seller.full_name AS current_listing_seller
                FROM reports r
                LEFT JOIN users reporter ON reporter.id = r.reporter_id
                LEFT JOIN users reported ON reported.id = r.reported_user_id
                LEFT JOIN users reviewer ON reviewer.id = r.admin_id
                LEFT JOIN listings listing ON listing.id = r.listing_id
                LEFT JOIN users seller ON seller.id = listing.user_id
                WHERE r.id = %s
                """,
                (report_id,),
            )
            report = cursor.fetchone()
            if report is None:
                return None
            if report["reported_user_id"] is not None:
                cursor.execute(
                    "SELECT COUNT(*) AS previous_reports FROM reports WHERE reported_user_id = %s AND id <> %s",
                    (report["reported_user_id"], report_id),
                )
                report["previous_reports"] = cursor.fetchone()["previous_reports"]
            else:
                report["previous_reports"] = 0
            cursor.execute(
                """
                SELECT h.*, admin.full_name AS admin_name
                FROM report_history h
                LEFT JOIN users admin ON admin.id = h.admin_id
                WHERE h.report_id = %s
                ORDER BY h.reviewed_at DESC, h.id DESC
                """,
                (report_id,),
            )
            report["history"] = cursor.fetchall()
            return report
        finally:
            cursor.close()


def review_report(report_id, admin_id, new_status, admin_note, action_taken):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            cursor.execute(
                """
                SELECT id, reporter_id, reported_user_id, listing_id, status
                FROM reports
                WHERE id = %s
                FOR UPDATE
                """,
                (report_id,),
            )
            report = cursor.fetchone()
            if report is None:
                connection.rollback()
                return False, "Report not found."
            previous_status = report["status"]
            if previous_status in {"approved", "denied"}:
                connection.rollback()
                return False, "This report has already been resolved."
            if new_status == "under_review":
                action_taken = "none"
            if action_taken in {"flag_listing", "hide_listing"} and report["listing_id"] is None:
                connection.rollback()
                return False, "The reported listing is no longer available."
            if action_taken == "suspend_user" and report["reported_user_id"] is None:
                connection.rollback()
                return False, "The reported user is no longer available."
            cursor.execute(
                """
                UPDATE reports
                SET status = %s, admin_id = %s, admin_decision = %s,
                    admin_note = %s, action_taken = %s, reviewed_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (new_status, admin_id, new_status, admin_note, action_taken, report_id),
            )
            if action_taken == "flag_listing" and report["listing_id"] is not None:
                cursor.execute("UPDATE listings SET is_flagged = 1 WHERE id = %s", (report["listing_id"],))
            elif action_taken == "hide_listing" and report["listing_id"] is not None:
                cursor.execute("UPDATE listings SET is_hidden = 1 WHERE id = %s", (report["listing_id"],))
            elif action_taken == "suspend_user" and report["reported_user_id"] is not None:
                cursor.execute(
                    "UPDATE users SET account_status = 'suspended' "
                    "WHERE id = %s AND role IN ('buyer', 'seller', 'student', 'user')",
                    (report["reported_user_id"],),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return False, "Administrator accounts cannot be suspended through report review."
            cursor.execute(
                """
                INSERT INTO report_history
                    (report_id, reporter_id, reported_user_id, listing_id, admin_id,
                     previous_status, new_status, admin_decision, admin_note, action_taken)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    report_id, report["reporter_id"], report["reported_user_id"],
                    report["listing_id"], admin_id, previous_status, new_status,
                    new_status, admin_note, action_taken,
                ),
            )
            connection.commit()
            return True, None
        except mysql.connector.Error:
            connection.rollback()
            raise
        finally:
            cursor.close()


def find_report_summary():
    counts = {}
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM reports
                GROUP BY status
                """
            )
            counts = {row["status"]: int(row["total"]) for row in cursor.fetchall()}
        finally:
            cursor.close()
    return {
        "pending": counts.get("pending", 0),
        "under_review": counts.get("under_review", 0),
        "approved": counts.get("approved", 0),
        "denied": counts.get("denied", 0),
        "recent": find_admin_reports(limit=10),
    }


def add_favorite(user_id, listing_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "INSERT IGNORE INTO favorites (user_id, listing_id) VALUES (%s, %s)",
                (user_id, listing_id),
            )
            created = cursor.rowcount > 0
            if created:
                cursor.execute(
                    """
                    SELECT l.id, l.user_id AS seller_id, l.title, u.full_name AS buyer_name
                    FROM listings l
                    JOIN users u ON u.id = %s
                    WHERE l.id = %s
                    """,
                    (user_id, listing_id),
                )
                favorite = cursor.fetchone()
                if favorite and int(favorite["seller_id"]) != int(user_id):
                    create_notification(
                        int(favorite["seller_id"]),
                        "favorite",
                        "Item Favorited",
                        f"{favorite['buyer_name']} added your item to their favorites.",
                        "favorite",
                        int(favorite["id"]),
                        f"/marketplace?listing={int(favorite['id'])}",
                        user_id,
                    )
            connection.commit()
            return 1 if created else 0
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


def find_cart_items(user_id, cart_item_ids=None):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            parameters = [user_id]
            filter_clause = ""
            if cart_item_ids is not None:
                if not cart_item_ids:
                    return []
                placeholders = ", ".join(["%s"] * len(cart_item_ids))
                filter_clause = f" AND ci.id IN ({placeholders})"
                parameters.extend(cart_item_ids)
            cursor.execute(
                f"""
                SELECT ci.id AS cart_item_id, ci.listing_id, ci.quantity AS cart_quantity,
                       l.title, l.category, l.price, l.image_url, l.quantity AS available_quantity,
                       l.user_id AS seller_id, l.is_hidden,
                       seller.full_name AS seller_name
                FROM cart_items ci
                LEFT JOIN listings l ON l.id = ci.listing_id
                LEFT JOIN users seller ON seller.id = l.user_id
                WHERE ci.user_id = %s{filter_clause}
                ORDER BY ci.updated_at DESC, ci.id DESC
                """,
                tuple(parameters),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def count_cart_items(user_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM cart_items WHERE user_id = %s",
                (user_id,),
            )
            return int(cursor.fetchone()[0])
        finally:
            cursor.close()


def add_cart_item(user_id, listing_id, quantity=1, replace_quantity=False):
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        return None, "Choose a valid quantity."
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            cursor.execute(
                """
                SELECT id, user_id, quantity, is_hidden
                FROM listings
                WHERE id = %s
                FOR UPDATE
                """,
                (listing_id,),
            )
            listing = cursor.fetchone()
            if listing is None or listing["is_hidden"]:
                connection.rollback()
                return None, "This item is no longer available."
            if int(listing["user_id"]) == int(user_id):
                connection.rollback()
                return None, "You cannot add your own item to the cart."
            available_quantity = int(listing["quantity"])
            if available_quantity < 1:
                connection.rollback()
                return None, "This item is out of stock."
            cursor.execute(
                "SELECT id, quantity FROM cart_items WHERE user_id = %s AND listing_id = %s FOR UPDATE",
                (user_id, listing_id),
            )
            existing = cursor.fetchone()
            next_quantity = (
                quantity
                if replace_quantity or existing is None
                else int(existing["quantity"]) + quantity
            )
            if next_quantity > available_quantity:
                connection.rollback()
                return None, f"Only {available_quantity} item(s) are currently available."
            if existing is None:
                cursor.execute(
                    "INSERT INTO cart_items (user_id, listing_id, quantity) VALUES (%s, %s, %s)",
                    (user_id, listing_id, next_quantity),
                )
                cart_item_id = cursor.lastrowid
            else:
                cursor.execute(
                    "UPDATE cart_items SET quantity = %s WHERE id = %s AND user_id = %s",
                    (next_quantity, existing["id"], user_id),
                )
                cart_item_id = existing["id"]
            connection.commit()
            return int(cart_item_id), None
        except mysql.connector.Error:
            connection.rollback()
            raise
        finally:
            cursor.close()


def update_cart_item_quantity(user_id, cart_item_id, quantity):
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        return False, "Choose a quantity of at least 1."
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            cursor.execute(
                """
                SELECT ci.id, l.quantity AS available_quantity, l.is_hidden
                FROM cart_items ci
                LEFT JOIN listings l ON l.id = ci.listing_id
                WHERE ci.id = %s AND ci.user_id = %s
                FOR UPDATE
                """,
                (cart_item_id, user_id),
            )
            row = cursor.fetchone()
            if row is None:
                connection.rollback()
                return False, "Cart item not found."
            if row["is_hidden"] or row["available_quantity"] is None:
                connection.rollback()
                return False, "This item is no longer available."
            available_quantity = int(row["available_quantity"])
            if available_quantity < 1:
                connection.rollback()
                return False, "This item is out of stock."
            if quantity > available_quantity:
                connection.rollback()
                return False, f"Only {available_quantity} item(s) are currently available."
            cursor.execute(
                "UPDATE cart_items SET quantity = %s WHERE id = %s AND user_id = %s",
                (quantity, cart_item_id, user_id),
            )
            connection.commit()
            return True, None
        except mysql.connector.Error:
            connection.rollback()
            raise
        finally:
            cursor.close()


def remove_cart_item(user_id, cart_item_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "DELETE FROM cart_items WHERE id = %s AND user_id = %s",
                (cart_item_id, user_id),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            cursor.close()


ORDER_PAYMENT_METHODS = frozenset({"Cash on Pick Up", "GCash", "Bank Transfer"})


def create_order(
    buyer_id,
    listing_id,
    quantity=1,
    payment_method="Cash on Pick Up",
    payment_reference_no=None,
    payment_receipt_path=None,
    cart_item_id=None,
):
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        return None, "Choose a valid quantity.", None
    normalized_method = str(payment_method or "").strip()
    if normalized_method not in ORDER_PAYMENT_METHODS:
        return None, "Choose a valid payment method.", None
    requires_payment_evidence = normalized_method in {"GCash", "Bank Transfer"}
    normalized_reference = str(payment_reference_no or "").strip()
    normalized_receipt = str(payment_receipt_path or "").strip()
    if requires_payment_evidence and (
        not normalized_reference or len(normalized_reference) > 100 or not normalized_receipt
    ):
        return None, "Please complete the required payment information.", None
    if not requires_payment_evidence and (normalized_reference or normalized_receipt):
        return None, "Payment information is not required for this payment method.", None

    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            if cart_item_id is not None:
                cursor.execute(
                    """
                    SELECT listing_id, quantity
                    FROM cart_items
                    WHERE id = %s AND user_id = %s
                    FOR UPDATE
                    """,
                    (cart_item_id, buyer_id),
                )
                cart_item = cursor.fetchone()
                if cart_item is None or cart_item["listing_id"] is None:
                    connection.rollback()
                    return None, "This cart item is no longer available.", None
                listing_id = int(cart_item["listing_id"])
                quantity = int(cart_item["quantity"])
            cursor.execute(
                """
                SELECT id, user_id AS seller_id, title, price, quantity
                FROM listings
                WHERE id = %s AND is_hidden = 0
                FOR UPDATE
                """,
                (listing_id,),
            )
            listing = cursor.fetchone()
            if listing is None:
                return None, "Listing not found.", None
            if int(listing["seller_id"]) == int(buyer_id):
                return None, "You cannot order your own listing.", None
            available_quantity = int(listing["quantity"])
            if available_quantity == 0:
                return None, "This item is out of stock.", None
            if quantity > available_quantity:
                return None, f"Only {available_quantity} available.", None
            total_price = listing["price"] * quantity
            try:
                cursor.execute(
                    """
                    UPDATE listings
                    SET quantity = quantity - %s
                    WHERE id = %s AND quantity >= %s
                    """,
                    (quantity, listing_id, quantity),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return None, "The requested quantity is no longer available.", None
                cursor.execute(
                    """
                    INSERT INTO orders
                        (buyer_id, seller_id, listing_id, quantity, stock_deducted,
                         total_price, payment_method, payment_status, payment_receipt_path,
                         payment_reference_no, payment_submitted_at, status)
                    VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
                            CASE WHEN %s = 'pending_verification' THEN CURRENT_TIMESTAMP ELSE NULL END,
                            'order_placed')
                    """,
                    (
                        buyer_id, listing["seller_id"], listing_id, quantity,
                        total_price,
                        normalized_method,
                        "pending_verification" if requires_payment_evidence else "not_required",
                        normalized_receipt or None,
                        normalized_reference or None,
                        "pending_verification" if requires_payment_evidence else "not_required",
                    ),
                )
                order_id = cursor.lastrowid
                if requires_payment_evidence:
                    cursor.execute(
                        """
                        INSERT INTO payment_verification_events
                            (order_id, actor_id, event_type, payment_reference_no, payment_amount, receipt_path)
                        VALUES (%s, %s, 'submitted', %s, %s, %s)
                        """,
                        (
                            order_id,
                            buyer_id,
                            normalized_reference,
                            total_price,
                            normalized_receipt,
                        ),
                    )
                if cart_item_id is not None:
                    cursor.execute(
                        "DELETE FROM cart_items WHERE id = %s AND user_id = %s",
                        (cart_item_id, buyer_id),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        return None, "This cart item is no longer available.", None
                connection.commit()
                try:
                    cursor.execute(
                        """
                        SELECT u.full_name, l.title
                        FROM users u
                        JOIN listings l ON l.id = %s
                        WHERE u.id = %s
                        """,
                        (listing_id, buyer_id),
                    )
                    buyer_order = cursor.fetchone()
                    if buyer_order:
                        create_notification(
                            int(listing["seller_id"]),
                            "purchase",
                            "New Purchase",
                            f"{buyer_order['full_name']} purchased your {buyer_order['title']}.",
                            "order",
                            int(order_id),
                            f"/orders?view=sales&order={int(order_id)}",
                            buyer_id,
                        )
                except mysql.connector.Error:
                    logger.exception(
                        "Order %s was created, but its seller notification failed.",
                        order_id,
                    )
            except mysql.connector.IntegrityError as error:
                connection.rollback()
                if requires_payment_evidence and error.errno == 1062:
                    return None, "That payment reference number has already been used.", None
                raise
            except mysql.connector.Error:
                connection.rollback()
                raise
            return order_id, None, total_price
        finally:
            cursor.close()


def find_orders_for_user(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                  SELECT o.id, o.listing_id, o.buyer_id, o.seller_id, o.quantity, o.total_price,
                      o.payment_method, o.payment_status, o.payment_reference_no,
                      o.payment_submitted_at, o.payment_verified_at,
                      o.payment_rejection_reason, o.status, o.created_at,
                      CASE WHEN o.status = 'order_placed'
                                  AND o.payment_status NOT IN ('pending_verification', 'verified')
                                  AND o.created_at >= NOW() - INTERVAL 24 HOUR THEN 1 ELSE 0 END AS can_cancel,
                      CASE
                        WHEN o.status = 'completed'
                         AND o.payment_status IN ('not_required', 'verified')
                         AND NOT EXISTS (
                           SELECT 1 FROM ratings r WHERE r.transaction_id = o.id
                         )
                        THEN 1 ELSE 0
                      END AS can_rate_seller,
                       l.title AS listing_title, l.image_url AS listing_image,
                       seller.full_name AS seller_name,
                       {_verified_account_sql("seller")} AS seller_verified
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


def find_order_total_price(buyer_id, order_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT total_price FROM orders WHERE id = %s AND buyer_id = %s",
                (order_id, buyer_id),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()


def find_sales_for_user(user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT o.id, o.listing_id, o.buyer_id, o.seller_id, o.quantity, o.total_price,
                       o.payment_method, o.payment_status, o.payment_reference_no,
                       o.payment_submitted_at, o.payment_verified_at,
                       o.payment_rejection_reason, o.status, o.created_at,
                       l.title AS listing_title, l.image_url AS listing_image,
                       buyer.full_name AS buyer_name, buyer.email AS buyer_email,
                       {_verified_account_sql("buyer")} AS buyer_verified,
                       CASE WHEN o.status = 'order_placed'
                                  AND o.payment_status NOT IN ('pending_verification', 'verified')
                                  AND o.created_at >= NOW() - INTERVAL 24 HOUR THEN 1 ELSE 0 END AS can_seller_cancel
                FROM orders o
                JOIN listings l ON l.id = o.listing_id AND l.user_id = o.seller_id
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
    allowed_statuses = {
        "order_placed",
        "preparing_order",
        "ready_for_pickup",
        "completed",
        "cancelled",
    }
    if status not in allowed_statuses:
        return False, "Invalid order status.", False

    next_status = {
        "order_placed": "preparing_order",
        "preparing_order": "ready_for_pickup",
        "ready_for_pickup": "completed",
    }
    notifications = {
        "preparing_order": (
            "order_progress",
            "Order Being Prepared",
            "Your order is now being prepared.",
        ),
        "ready_for_pickup": (
            "order_ready",
            "Ready for Pick Up",
            "Your order is ready for pick up.",
        ),
        "completed": (
            "order_complete",
            "Order Completed",
            "Your order has been marked as completed.",
        ),
        "cancelled": (
            "order_cancelled",
            "Order Cancelled",
            "Your order has been cancelled.",
        ),
    }
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT o.buyer_id, o.seller_id, o.listing_id, o.quantity,
                       o.stock_deducted, o.status AS current_status,
                       o.payment_method, o.payment_status, o.created_at,
                       l.title AS listing_title
                FROM orders o
                JOIN listings l ON l.id = o.listing_id AND l.user_id = o.seller_id
                WHERE o.id = %s AND o.seller_id = %s
                FOR UPDATE
                """,
                (order_id, seller_id),
            )
            order = cursor.fetchone()
            if order is None:
                return False, "Order not found.", False

            current_status = order["current_status"]
            if current_status == status:
                return True, None, False

            if status == "cancelled":
                if (
                    current_status != "order_placed"
                    or order["payment_status"] in {"pending_verification", "verified"}
                ):
                    return False, "This order can no longer be cancelled.", False
                cursor.execute(
                    """
                    SELECT id
                    FROM orders
                    WHERE id = %s AND seller_id = %s
                      AND status = 'order_placed'
                      AND payment_status NOT IN ('pending_verification', 'verified')
                      AND created_at >= NOW() - INTERVAL 24 HOUR
                    """,
                    (order_id, seller_id),
                )
                if cursor.fetchone() is None:
                    return False, "The 24-hour cancellation period has expired.", False
            elif next_status.get(current_status) != status:
                return False, "Order status must follow the required progress sequence.", False
            elif (
                order["payment_method"] in {"GCash", "Bank Transfer"}
                and order["payment_status"] != "verified"
            ):
                return False, "The order can progress after its payment is verified.", False

            cursor.execute(
                """
                UPDATE orders
                SET status = %s,
                    stock_deducted = CASE WHEN %s = 'cancelled' THEN 0 ELSE stock_deducted END
                WHERE id = %s AND seller_id = %s AND status = %s
                """,
                (status, status, order_id, seller_id, current_status),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False, "The order status changed before your update was applied.", False

            if status == "cancelled" and order["stock_deducted"]:
                cursor.execute(
                    """
                    UPDATE listings
                    SET quantity = quantity + %s
                    WHERE id = %s AND user_id = %s
                    """,
                    (order["quantity"], order["listing_id"], seller_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False, "The listing could not be updated for this cancellation.", False

            kind, title, message = notifications[status]
            _create_notification_with_cursor(
                cursor,
                int(order["buyer_id"]),
                kind,
                title,
                message,
                "order",
                int(order_id),
                f"/orders?view=orders&order={int(order_id)}",
                int(seller_id),
            )
            if status == "completed":
                _create_notification_with_cursor(
                    cursor,
                    int(seller_id),
                    "item_sold",
                    "Item Sold",
                    f"Your {order['listing_title']} has been sold.",
                    "order",
                    int(order_id),
                    f"/orders?view=sales&order={int(order_id)}",
                    int(order["buyer_id"]),
                )
            connection.commit()
            return True, None, True
        except mysql.connector.Error:
            connection.rollback()
            raise
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
                """
                SELECT listing_id, quantity, stock_deducted, status, payment_status, seller_id
                FROM orders
                WHERE id = %s AND buyer_id = %s
                FOR UPDATE
                """,
                (order_id, buyer_id),
            )
            order = cursor.fetchone()
            if order is None:
                return False, "Order not found."
            if order["status"] != "order_placed":
                return False, "This order can no longer be cancelled."
            if order["payment_status"] in {"pending_verification", "verified"}:
                return False, "An order with submitted or verified payment can no longer be cancelled."
            try:
                cursor.execute(
                    """
                    UPDATE orders
                    SET status = 'cancelled',
                        stock_deducted = 0
                    WHERE id = %s AND buyer_id = %s AND status = 'order_placed'
                      AND payment_status NOT IN ('pending_verification', 'verified')
                      AND created_at >= NOW() - INTERVAL 24 HOUR
                    """,
                    (order_id, buyer_id),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return False, "The 24-hour cancellation period has expired."
                if order["stock_deducted"]:
                    cursor.execute(
                        "UPDATE listings SET quantity = quantity + %s WHERE id = %s",
                        (order["quantity"], order["listing_id"]),
                    )
                connection.commit()
                cursor.execute(
                    """
                    SELECT l.title, u.full_name AS buyer_name
                    FROM listings l
                    JOIN users u ON u.id = %s
                    WHERE l.id = %s
                    """,
                    (buyer_id, order["listing_id"]),
                )
                cancellation_details = cursor.fetchone()
                if cancellation_details:
                    create_notification(
                        int(order["seller_id"]),
                        "order_cancelled",
                        "Order Cancelled",
                        f"The buyer cancelled their order for {cancellation_details['title']}.",
                        "order",
                        int(order_id),
                        f"/orders?view=sales&order={int(order_id)}",
                        buyer_id,
                    )
                    create_notification(
                        int(buyer_id),
                        "order_cancelled",
                        "Order Cancelled",
                        f"Your order for {cancellation_details['title']} has been cancelled.",
                        "order",
                        int(order_id),
                        f"/orders?view=orders&order={int(order_id)}",
                        buyer_id,
                    )
            except mysql.connector.Error:
                connection.rollback()
                raise
            return True, None
        finally:
            cursor.close()


def submit_order_payment(buyer_id, order_id, reference_no, payment_amount, receipt_path):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, buyer_id, total_price, payment_method, payment_status, status
                FROM orders
                WHERE id = %s
                FOR UPDATE
                """,
                (order_id,),
            )
            order = cursor.fetchone()
            if order is None or int(order["buyer_id"]) != int(buyer_id):
                return False, "Order not found."
            if order["payment_method"] not in {"GCash", "Bank Transfer"}:
                return False, "Payment verification is not required for this payment method."
            if order["status"] != "order_placed":
                return False, "Payment can only be submitted before the order is being prepared."
            try:
                amount = Decimal(str(payment_amount))
            except (InvalidOperation, TypeError, ValueError):
                return False, "The payment amount does not match the order total."
            if not amount.is_finite() or amount != Decimal(order["total_price"]):
                return False, "The payment amount does not match the order total."
            if order["payment_status"] not in {"unsubmitted", "rejected"}:
                return False, "This payment has already been submitted or verified."
            cursor.execute(
                """
                INSERT INTO payment_verification_events
                    (order_id, actor_id, event_type, payment_reference_no, payment_amount, receipt_path)
                VALUES (%s, %s, 'submitted', %s, %s, %s)
                """,
                (order_id, buyer_id, reference_no, amount, receipt_path),
            )
            cursor.execute(
                """
                UPDATE orders
                SET payment_status = 'pending_verification',
                    payment_receipt_path = %s,
                    payment_reference_no = %s,
                    payment_submitted_at = CURRENT_TIMESTAMP,
                    payment_verified_at = NULL,
                    payment_verified_by = NULL,
                    payment_rejection_reason = NULL
                WHERE id = %s AND buyer_id = %s
                """,
                (receipt_path, reference_no, order_id, buyer_id),
            )
            connection.commit()
            return True, None
        except mysql.connector.IntegrityError as error:
            connection.rollback()
            if error.errno == 1062:
                return False, "That payment reference number has already been used."
            raise
        finally:
            cursor.close()


def review_order_payment(seller_id, order_id, decision, rejection_reason=None):
    if decision not in {"approved", "rejected"}:
        return False, "Invalid payment review decision."
    if decision == "rejected" and (not rejection_reason or len(rejection_reason) > 500):
        return False, "A rejection reason is required."
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT id, seller_id, payment_method, payment_status,
                       payment_reference_no, payment_receipt_path, status
                FROM orders
                WHERE id = %s
                FOR UPDATE
                """,
                (order_id,),
            )
            order = cursor.fetchone()
            if order is None or int(order["seller_id"]) != int(seller_id):
                return False, "Order not found."
            if order["payment_method"] not in {"GCash", "Bank Transfer"}:
                return False, "Payment verification is not required for this payment method."
            if order["payment_status"] != "pending_verification" or order["status"] == "cancelled":
                return False, "This payment is no longer awaiting review."
            if decision == "approved":
                cursor.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'verified',
                        payment_verified_at = CURRENT_TIMESTAMP,
                        payment_verified_by = %s,
                        payment_rejection_reason = NULL
                    WHERE id = %s AND seller_id = %s
                      AND payment_status = 'pending_verification'
                      AND status <> 'cancelled'
                    """,
                    (seller_id, order_id, seller_id),
                )
                event_type = "approved"
                reason = None
            else:
                cursor.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'rejected',
                        payment_verified_at = NULL,
                        payment_verified_by = NULL,
                        payment_rejection_reason = %s
                    WHERE id = %s AND seller_id = %s
                      AND payment_status = 'pending_verification'
                      AND status <> 'cancelled'
                    """,
                    (rejection_reason, order_id, seller_id),
                )
                event_type = "rejected"
                reason = rejection_reason
            if cursor.rowcount != 1:
                connection.rollback()
                return False, "This payment is no longer awaiting review."
            cursor.execute(
                """
                INSERT INTO payment_verification_events
                    (order_id, actor_id, event_type, payment_reference_no, payment_amount, receipt_path, reason)
                VALUES (%s, %s, %s, NULL, NULL, %s, %s)
                """,
                (
                    order_id, seller_id, event_type, order["payment_receipt_path"], reason,
                ),
            )
            connection.commit()
            return True, None
        finally:
            cursor.close()


def find_order_payment_receipt(user_id, order_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT payment_receipt_path
                FROM orders
                WHERE id = %s AND (buyer_id = %s OR seller_id = %s)
                  AND payment_status IN ('pending_verification', 'verified', 'rejected')
                """,
                (order_id, user_id, user_id),
            )
            row = cursor.fetchone()
            return row["payment_receipt_path"] if row else None
        finally:
            cursor.close()


def find_order_payment_notification(order_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT o.id, o.payment_status, o.payment_rejection_reason,
                       buyer.email AS buyer_email, buyer.full_name AS buyer_name,
                       listing.title AS listing_title
                FROM orders o
                JOIN users buyer ON buyer.id = o.buyer_id
                JOIN listings listing ON listing.id = o.listing_id
                WHERE o.id = %s
                """,
                (order_id,),
            )
            return cursor.fetchone()
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
                       l.description, l.price, l.quantity, l.image_url, l.created_at
                FROM favorites f
                JOIN listings l ON l.id = f.listing_id
                WHERE f.user_id = %s AND l.is_hidden = 0
                ORDER BY f.created_at DESC, f.id DESC
                """,
                (user_id,),
            )
            return cursor.fetchall()
        finally:
            cursor.close()


def create_notification(
    recipient_id,
    kind,
    title,
    message,
    related_type=None,
    related_id=None,
    link_url=None,
    actor_id=None,
):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            notification_id = _create_notification_with_cursor(
                cursor,
                recipient_id,
                kind,
                title,
                message,
                related_type,
                related_id,
                link_url,
                actor_id,
            )
            if notification_id is not None:
                connection.commit()
            return notification_id
        finally:
            cursor.close()


def _create_notification_with_cursor(
    cursor,
    recipient_id,
    kind,
    title,
    message,
    related_type=None,
    related_id=None,
    link_url=None,
    actor_id=None,
):
    normalized_kind = str(kind or "general").strip()[:40] or "general"
    normalized_title = str(title or "").strip()[:160]
    normalized_message = str(message or "").strip()[:255]
    if recipient_id is None or recipient_id <= 0 or not normalized_title or not normalized_message:
        return None
    if related_type and related_id is not None and actor_id is not None:
        cursor.execute(
            """
            SELECT id FROM notifications
            WHERE recipient_id = %s
              AND actor_id = %s
              AND related_type = %s
              AND related_id = %s
              AND title = %s
              AND created_at >= NOW() - INTERVAL 7 DAY
            LIMIT 1
            """,
            (recipient_id, actor_id, related_type, related_id, normalized_title),
        )
        if cursor.fetchone() is not None:
            return None
    cursor.execute(
        """
        INSERT INTO notifications
            (recipient_id, actor_id, kind, title, message, related_type, related_id, link_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            recipient_id,
            actor_id,
            normalized_kind,
            normalized_title,
            normalized_message,
            related_type,
            related_id,
            link_url,
        ),
    )
    return cursor.lastrowid


def find_notifications_for_user(user_id, unread_only=False, limit=20):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            query = """
                SELECT n.id, n.recipient_id, n.actor_id, n.kind, n.title, n.message, n.related_type,
                       n.related_id, n.link_url, n.is_read, n.created_at, actor.full_name AS actor_name
                FROM notifications n
                LEFT JOIN users actor ON actor.id = n.actor_id
                WHERE n.recipient_id = %s
            """
            params = [user_id]
            if unread_only:
                query += " AND n.is_read = 0 "
            query += " ORDER BY n.created_at DESC, n.id DESC LIMIT %s "
            params.append(int(limit) if limit and int(limit) > 0 else 20)
            cursor.execute(query, tuple(params))
            notifications = cursor.fetchall()
            for item in notifications:
                item["id"] = int(item["id"])
                item["recipient_id"] = int(item["recipient_id"])
                item["actor_id"] = int(item["actor_id"]) if item["actor_id"] is not None else None
                item["related_id"] = int(item["related_id"]) if item["related_id"] is not None else None
                item["is_read"] = bool(item["is_read"])
                item["created_at"] = item["created_at"].isoformat() if item["created_at"] else None
            return notifications
        finally:
            cursor.close()


def count_unread_notifications(user_id):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM notifications WHERE recipient_id = %s AND is_read = 0",
                (user_id,),
            )
            return int(cursor.fetchone()[0] or 0)
        finally:
            cursor.close()


def mark_notifications_read(user_id, notification_id=None):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            if notification_id is None:
                cursor.execute(
                    "UPDATE notifications SET is_read = 1 WHERE recipient_id = %s AND is_read = 0",
                    (user_id,),
                )
            else:
                cursor.execute(
                    "UPDATE notifications SET is_read = 1 WHERE recipient_id = %s AND id = %s AND is_read = 0",
                    (user_id, notification_id),
                )
            connection.commit()
            return cursor.rowcount
        finally:
            cursor.close()


def create_message(listing_id, sender_id, recipient_id, body, reply_to_id=None, image_url=None):
    with get_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT user_id FROM listings WHERE id = %s",
                (listing_id,),
            )
            listing = cursor.fetchone()
            if listing is None:
                raise ValueError("The listing could not be found.")
            listing_owner_id = int(listing[0])
            cursor.execute(
                """
                SELECT 1
                FROM messages
                WHERE listing_id = %s
                  AND ((sender_id = %s AND recipient_id = %s)
                    OR (sender_id = %s AND recipient_id = %s))
                LIMIT 1
                """,
                (listing_id, sender_id, recipient_id, recipient_id, sender_id),
            )
            existing_conversation = cursor.fetchone() is not None
            if listing_owner_id != int(recipient_id) and not existing_conversation:
                raise ValueError("You cannot message this user about this listing.")
            if reply_to_id is not None:
                cursor.execute(
                    """
                    SELECT 1
                    FROM messages
                    WHERE id = %s
                      AND ((sender_id = %s AND recipient_id = %s)
                        OR (sender_id = %s AND recipient_id = %s))
                    """,
                    (reply_to_id, sender_id, recipient_id, recipient_id, sender_id),
                )
                if cursor.fetchone() is None:
                    raise ValueError("The reply target is not part of this conversation.")
            cursor.execute(
                """
                INSERT INTO messages (listing_id, sender_id, recipient_id, body, image_url, reply_to_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (listing_id, sender_id, recipient_id, body, image_url, reply_to_id),
            )
            message_id = cursor.lastrowid
            cursor.execute(
                "SELECT full_name FROM users WHERE id = %s",
                (sender_id,),
            )
            sender_name = cursor.fetchone()
            sender_display_name = (sender_name[0] if sender_name else "Someone").strip() or "Someone"
            _create_notification_with_cursor(
                cursor,
                recipient_id,
                "message",
                "New Message",
                f"{sender_display_name} sent you a message." if body else f"{sender_display_name} sent you a photo.",
                "conversation",
                sender_id,
                f"/messages?other_user_id={sender_id}&listing_id={listing_id}",
                sender_id,
            )
            connection.commit()
            return message_id
        finally:
            cursor.close()


def find_messages(user_id, listing_id, other_user_id):
    with get_connection() as connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                  SELECT m.id, m.listing_id, m.sender_id, m.recipient_id,
                      m.body, m.image_url, m.reply_to_id, m.created_at,
                      UNIX_TIMESTAMP(m.created_at) * 1000 AS created_at_ms,
                      u.full_name AS sender_name, u.profile_image_url AS sender_image_url,
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
                f"""
                  SELECT m.id, m.listing_id, m.sender_id, m.recipient_id,
                      m.body, m.image_url, m.reply_to_id, m.created_at,
                      UNIX_TIMESTAMP(m.created_at) * 1000 AS created_at_ms,
                      u.full_name AS sender_name, u.profile_image_url AS sender_image_url,
                      {_verified_account_sql("u")} AS sender_verified,
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
                f"""
                  SELECT m.id, m.listing_id, m.sender_id, m.recipient_id, m.body,
                       m.image_url,
                       m.created_at, UNIX_TIMESTAMP(m.created_at) * 1000 AS created_at_ms,
                       l.title AS listing_title,
                       l.category AS listing_category, l.price AS listing_price,
                       l.image_url AS listing_image, other.profile_image_url AS other_image_url,
                                             (SELECT COUNT(*) FROM messages unread
                                                WHERE unread.sender_id = other.id
                                                    AND unread.recipient_id = %s
                                                    AND unread.read_at IS NULL) AS unread_count,
                       other.id AS other_user_id, other.full_name AS other_name,
                       {_verified_account_sql("other")} AS other_verified
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
