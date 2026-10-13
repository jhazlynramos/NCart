CREATE DATABASE IF NOT EXISTS ncart
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE ncart;

CREATE TABLE IF NOT EXISTS users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  full_name VARCHAR(120) NOT NULL,
  first_name VARCHAR(120) NULL,
  middle_name VARCHAR(120) NULL,
  last_name VARCHAR(120) NULL,
  email VARCHAR(255) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  email_verified_at TIMESTAMP NULL DEFAULT NULL,
  profile_image_url VARCHAR(500) NULL,
  cover_image_url VARCHAR(500) NULL,
  username VARCHAR(32) NULL,
  bio VARCHAR(500) NULL,
  location VARCHAR(160) NULL,
  role VARCHAR(20) NOT NULL DEFAULT 'buyer',
  account_status VARCHAR(32) NOT NULL DEFAULT 'active',
  student_number VARCHAR(100) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  confirmed_by BIGINT UNSIGNED NULL,
  confirmed_at TIMESTAMP NULL DEFAULT NULL,
  approval_status VARCHAR(20) NOT NULL DEFAULT 'approved',
  approved_by BIGINT UNSIGNED NULL,
  approved_at TIMESTAMP NULL DEFAULT NULL,
  approval_reviewed_by BIGINT UNSIGNED NULL,
  approval_reviewed_at TIMESTAMP NULL DEFAULT NULL,
  rejection_reason VARCHAR(500) NULL,
  student_id_number VARCHAR(100) NULL,
  student_id_image_path VARCHAR(255) NULL,
  verification_submitted_at TIMESTAMP NULL DEFAULT NULL,
  verification_reviewed_at TIMESTAMP NULL DEFAULT NULL,
  verification_reviewed_by BIGINT UNSIGNED NULL,
  verification_rejection_reason VARCHAR(500) NULL,
  admin_authorization_details VARCHAR(500) NULL,
  admin_request_submitted_at TIMESTAMP NULL DEFAULT NULL,
  admin_request_status VARCHAR(20) NOT NULL DEFAULT 'none',
  admin_request_reviewed_at TIMESTAMP NULL DEFAULT NULL,
  admin_request_reviewed_by BIGINT UNSIGNED NULL,
  admin_request_rejection_reason VARCHAR(500) NULL,
  admin_otp_delivery_status VARCHAR(20) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_email (email),
  UNIQUE KEY uq_users_username (username),
  UNIQUE KEY uq_users_student_number (student_number),
  KEY idx_users_verification_status (account_status, verification_submitted_at),
  CONSTRAINT fk_users_verification_admin
    FOREIGN KEY (verification_reviewed_by) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_users_confirmed_by
    FOREIGN KEY (confirmed_by) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_users_approved_by
    FOREIGN KEY (approved_by) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_users_approval_reviewed_by
    FOREIGN KEY (approval_reviewed_by) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_users_admin_request_reviewer
    FOREIGN KEY (admin_request_reviewed_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS admin_registration_otps (
  email VARCHAR(255) NOT NULL,
  full_name VARCHAR(120) NOT NULL,
  first_name VARCHAR(120) NULL,
  middle_name VARCHAR(120) NULL,
  last_name VARCHAR(120) NULL,
  password_hash VARCHAR(255) NOT NULL,
  account_type VARCHAR(20) NOT NULL DEFAULT 'admin',
  student_number VARCHAR(100) NULL,
  student_id_image_path VARCHAR(255) NULL,
  invitation_hash CHAR(64) NULL,
  otp_hash CHAR(64) NOT NULL,
  expires_at BIGINT UNSIGNED NOT NULL,
  attempt_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
  send_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
  window_started_at BIGINT UNSIGNED NOT NULL,
  last_sent_at BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (email),
  KEY idx_admin_registration_otp_expiry (expires_at)
);

CREATE TABLE IF NOT EXISTS admin_invitations (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  target_email VARCHAR(255) NOT NULL,
  code_hash CHAR(64) NOT NULL,
  issued_by BIGINT UNSIGNED NOT NULL,
  created_at BIGINT UNSIGNED NOT NULL,
  expires_at BIGINT UNSIGNED NOT NULL,
  used_by BIGINT UNSIGNED NULL,
  used_at BIGINT UNSIGNED NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_admin_invitation_code_hash (code_hash),
  KEY idx_admin_invitation_email (target_email, expires_at),
  CONSTRAINT fk_admin_invitation_issuer
    FOREIGN KEY (issued_by) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_admin_invitation_user
    FOREIGN KEY (used_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS admin_access_codes (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  target_email VARCHAR(255) NULL,
  code_hash CHAR(64) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at BIGINT UNSIGNED NOT NULL,
  expires_at BIGINT UNSIGNED NULL,
  revoked_at BIGINT UNSIGNED NULL,
  used_by BIGINT UNSIGNED NULL,
  used_at BIGINT UNSIGNED NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_admin_access_code_hash (code_hash),
  KEY idx_admin_access_code_status (status, expires_at),
  CONSTRAINT fk_admin_access_code_user
    FOREIGN KEY (used_by) REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS listings (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  title VARCHAR(160) NOT NULL,
  category VARCHAR(80) NOT NULL,
  item_condition VARCHAR(80) NOT NULL,
  description TEXT NOT NULL,
  price DECIMAL(10, 2) NOT NULL,
  quantity INT UNSIGNED NOT NULL DEFAULT 1,
  image_url VARCHAR(500) NULL,
  is_flagged TINYINT(1) NOT NULL DEFAULT 0,
  is_hidden TINYINT(1) NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_listings_user (user_id),
  CONSTRAINT fk_listings_user
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cart_items (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  listing_id BIGINT UNSIGNED NULL,
  quantity INT UNSIGNED NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_cart_items_user_listing (user_id, listing_id),
  KEY idx_cart_items_user_updated (user_id, updated_at),
  CONSTRAINT fk_cart_items_user
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_cart_items_listing
    FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS reports (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  reporter_id BIGINT UNSIGNED NULL,
  reported_user_id BIGINT UNSIGNED NULL,
  listing_id BIGINT UNSIGNED NULL,
  listing_title VARCHAR(160) NOT NULL,
  listing_image_url VARCHAR(500) NULL,
  listing_category VARCHAR(80) NULL,
  listing_condition VARCHAR(80) NULL,
  listing_description TEXT NULL,
  listing_price DECIMAL(10, 2) NULL,
  listing_created_at TIMESTAMP NULL,
  reason VARCHAR(120) NOT NULL,
  description TEXT NOT NULL,
  status ENUM('pending', 'under_review', 'approved', 'denied') NOT NULL DEFAULT 'pending',
  admin_id BIGINT UNSIGNED NULL,
  admin_decision VARCHAR(30) NULL,
  admin_note TEXT NULL,
  action_taken VARCHAR(30) NOT NULL DEFAULT 'none',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reviewed_at TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_reports_status_created (status, created_at),
  KEY idx_reports_reporter_created (reporter_id, created_at),
  KEY idx_reports_reported_user (reported_user_id),
  KEY idx_reports_listing (listing_id),
  CONSTRAINT fk_reports_reporter
    FOREIGN KEY (reporter_id) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_reports_reported_user
    FOREIGN KEY (reported_user_id) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_reports_listing
    FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE SET NULL,
  CONSTRAINT fk_reports_admin
    FOREIGN KEY (admin_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS report_history (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  report_id BIGINT UNSIGNED NOT NULL,
  reporter_id BIGINT UNSIGNED NULL,
  reported_user_id BIGINT UNSIGNED NULL,
  listing_id BIGINT UNSIGNED NULL,
  admin_id BIGINT UNSIGNED NULL,
  previous_status VARCHAR(30) NOT NULL,
  new_status VARCHAR(30) NOT NULL,
  admin_decision VARCHAR(30) NOT NULL,
  admin_note TEXT NOT NULL,
  action_taken VARCHAR(30) NOT NULL DEFAULT 'none',
  reviewed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_report_history_report (report_id, reviewed_at),
  CONSTRAINT fk_report_history_report
    FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE CASCADE,
  CONSTRAINT fk_report_history_reporter
    FOREIGN KEY (reporter_id) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_report_history_reported_user
    FOREIGN KEY (reported_user_id) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_report_history_listing
    FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE SET NULL,
  CONSTRAINT fk_report_history_admin
    FOREIGN KEY (admin_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS favorites (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  listing_id BIGINT UNSIGNED NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_favorites_user_listing (user_id, listing_id),
  KEY idx_favorites_user (user_id),
  CONSTRAINT fk_favorites_user
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_favorites_listing
    FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  buyer_id BIGINT UNSIGNED NOT NULL,
  seller_id BIGINT UNSIGNED NOT NULL,
  listing_id BIGINT UNSIGNED NOT NULL,
  quantity INT UNSIGNED NOT NULL DEFAULT 1,
  stock_deducted TINYINT(1) NOT NULL DEFAULT 0,
  total_price DECIMAL(10, 2) NOT NULL,
  payment_method VARCHAR(40) NOT NULL DEFAULT 'Cash on Pick Up',
  payment_status VARCHAR(32) NOT NULL DEFAULT 'not_required',
  payment_receipt_path VARCHAR(255) NULL,
  payment_reference_no VARCHAR(100) NULL,
  payment_submitted_at TIMESTAMP NULL DEFAULT NULL,
  payment_verified_at TIMESTAMP NULL DEFAULT NULL,
  payment_verified_by BIGINT UNSIGNED NULL,
  payment_rejection_reason VARCHAR(500) NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'order_placed',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_orders_buyer (buyer_id, created_at),
  KEY idx_orders_seller (seller_id, created_at),
  KEY idx_orders_seller_payment_status (seller_id, payment_status),
  CONSTRAINT fk_orders_payment_verified_by
    FOREIGN KEY (payment_verified_by) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_orders_buyer
    FOREIGN KEY (buyer_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_orders_seller
    FOREIGN KEY (seller_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_orders_listing
    FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ratings (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  transaction_id BIGINT UNSIGNED NOT NULL,
  reviewer_id BIGINT UNSIGNED NOT NULL,
  reviewed_user_id BIGINT UNSIGNED NOT NULL,
  rating TINYINT UNSIGNED NOT NULL,
  review TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_ratings_transaction (transaction_id),
  KEY idx_ratings_reviewed_created (reviewed_user_id, created_at),
  CONSTRAINT chk_ratings_value CHECK (rating BETWEEN 1 AND 5),
  CONSTRAINT fk_ratings_transaction
    FOREIGN KEY (transaction_id) REFERENCES orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_ratings_reviewer
    FOREIGN KEY (reviewer_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_ratings_reviewed_user
    FOREIGN KEY (reviewed_user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS payment_verification_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id BIGINT UNSIGNED NOT NULL,
  actor_id BIGINT UNSIGNED NULL,
  event_type VARCHAR(32) NOT NULL,
  payment_reference_no VARCHAR(100) NULL,
  payment_amount DECIMAL(10, 2) NULL,
  receipt_path VARCHAR(255) NULL,
  reason VARCHAR(500) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_payment_event_reference (payment_reference_no),
  KEY idx_payment_events_order (order_id, created_at),
  CONSTRAINT fk_payment_events_order
    FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_payment_events_actor
    FOREIGN KEY (actor_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  listing_id BIGINT UNSIGNED NOT NULL,
  sender_id BIGINT UNSIGNED NOT NULL,
  recipient_id BIGINT UNSIGNED NOT NULL,
  body TEXT NOT NULL,
  image_url VARCHAR(500) NULL,
  reply_to_id BIGINT UNSIGNED NULL,
  read_at TIMESTAMP NULL DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_messages_participants (sender_id, recipient_id, listing_id),
  KEY idx_messages_recipient (recipient_id, created_at),
  CONSTRAINT fk_messages_listing
    FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE CASCADE,
  CONSTRAINT fk_messages_sender
    FOREIGN KEY (sender_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_messages_recipient
    FOREIGN KEY (recipient_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_messages_reply
    FOREIGN KEY (reply_to_id) REFERENCES messages (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  recipient_id BIGINT UNSIGNED NOT NULL,
  actor_id BIGINT UNSIGNED NULL,
  kind VARCHAR(40) NOT NULL DEFAULT 'general',
  title VARCHAR(160) NOT NULL,
  message VARCHAR(255) NOT NULL,
  related_type VARCHAR(40) NULL,
  related_id BIGINT UNSIGNED NULL,
  link_url VARCHAR(255) NULL,
  is_read TINYINT(1) NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_notifications_recipient (recipient_id, is_read, created_at),
  CONSTRAINT fk_notifications_recipient
    FOREIGN KEY (recipient_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_notifications_actor
    FOREIGN KEY (actor_id) REFERENCES users (id) ON DELETE SET NULL
);
