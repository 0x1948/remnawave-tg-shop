import logging
from dataclasses import dataclass
from typing import Callable, List, Set

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class Migration:
    id: str
    description: str
    upgrade: Callable[[Connection], None]


def _ensure_migrations_table(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )


def _migration_0001_add_channel_subscription_fields(connection: Connection) -> None:
    inspector = inspect(connection)
    columns: Set[str] = {col["name"] for col in inspector.get_columns("users")}
    statements: List[str] = []

    if "channel_subscription_verified" not in columns:
        statements.append(
            "ALTER TABLE users ADD COLUMN channel_subscription_verified BOOLEAN"
        )
    if "channel_subscription_checked_at" not in columns:
        statements.append(
            "ALTER TABLE users ADD COLUMN channel_subscription_checked_at TIMESTAMPTZ"
        )
    if "channel_subscription_verified_for" not in columns:
        statements.append(
            "ALTER TABLE users ADD COLUMN channel_subscription_verified_for BIGINT"
        )

    for stmt in statements:
        connection.execute(text(stmt))

def _migration_0002_add_user_balance(connection: Connection) -> None:
    inspector = inspect(connection)
    columns = {col["name"] for col in inspector.get_columns("users")}

    if "balance" not in columns:
        connection.execute(
            text("ALTER TABLE users ADD COLUMN balance BIGINT DEFAULT 0")
        )

def _migration_0003_add_user_total_earned(connection: Connection) -> None:
    inspector = inspect(connection)
    columns = {col["name"] for col in inspector.get_columns("users")}

    if "total_earned" not in columns:
        connection.execute(
            text("ALTER TABLE users ADD COLUMN total_earned BIGINT DEFAULT 0")
        )

def _migration_0004_add_user_referral_reward_applied(connection: Connection) -> None:
    inspector = inspect(connection)
    columns = {col["name"] for col in inspector.get_columns("payments")}

    if "referral_reward_applied" not in columns:
        connection.execute(
            text("ALTER TABLE payments ADD COLUMN referral_reward_applied BOOLEAN DEFAULT FALSE")
        )

def _migration_0005_add_subscription_resend_disable_fields(connection: Connection) -> None:
    inspector = inspect(connection)
    columns = {col["name"] for col in inspector.get_columns("subscriptions")}
    statements: List[str] = []

    if "resend_disable_message_date" not in columns:
        statements.append(
            "ALTER TABLE subscriptions "
            "ADD COLUMN resend_disable_message_date TIMESTAMPTZ"
        )

    if "resend_disable_message_step" not in columns:
        statements.append(
            "ALTER TABLE subscriptions "
            "ADD COLUMN resend_disable_message_step INTEGER DEFAULT 0"
        )

    for stmt in statements:
        connection.execute(text(stmt))

def _migration_0006_create_payouts_table(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())

    if "payouts" in tables:
        return

    connection.execute(text("""
        CREATE TABLE payouts (
            payout_id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL
                REFERENCES users(user_id)
                ON DELETE CASCADE,
            price INTEGER NOT NULL,
            requisites VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ
        )
    """))

    connection.execute(text(
        "CREATE INDEX idx_payouts_user_id ON payouts(user_id)"
    ))
    connection.execute(text(
        "CREATE INDEX idx_payouts_status ON payouts(status)"
    ))

MIGRATIONS: List[Migration] = [
    Migration(
        id="0001_add_channel_subscription_fields",
        description="Add columns to track required channel subscription verification",
        upgrade=_migration_0001_add_channel_subscription_fields,
    ),
    Migration(
        id="0002_add_user_balance",
        description="Add balance column to users table",
        upgrade=_migration_0002_add_user_balance,
    ),
    Migration(
        id="0003_add_user_total_earned",
        description="Add total_earned column to users table",
        upgrade=_migration_0003_add_user_total_earned,
    ),
    Migration(
        id="0004_add_user_referral_reward_applied",
        description="Add referral_reward_applied column to users table",
        upgrade=_migration_0004_add_user_referral_reward_applied,
    ),
    Migration(
        id="0005_add_subscription_resend_disable_fields",
        description="Add resend disable notification fields to subscriptions table",
        upgrade=_migration_0005_add_subscription_resend_disable_fields,
    ),
    Migration(
        id="0006_create_payouts_table",
        description="Create payouts table",
        upgrade=_migration_0006_create_payouts_table,
    )
]


def run_database_migrations(connection: Connection) -> None:
    """
    Apply pending migrations sequentially. Already applied revisions are skipped.
    """
    _ensure_migrations_table(connection)

    applied_revisions: Set[str] = {
        row[0]
        for row in connection.execute(
            text("SELECT id FROM schema_migrations")
        )
    }

    for migration in MIGRATIONS:
        if migration.id in applied_revisions:
            continue

        logging.info(
            "Migrator: applying %s – %s", migration.id, migration.description
        )
        try:
            with connection.begin_nested():
                migration.upgrade(connection)
                connection.execute(
                    text(
                        "INSERT INTO schema_migrations (id) VALUES (:revision)"
                    ),
                    {"revision": migration.id},
                )
        except Exception as exc:
            logging.error(
                "Migrator: failed to apply %s (%s)",
                migration.id,
                migration.description,
                exc_info=True,
            )
            raise exc
        else:
            logging.info("Migrator: migration %s applied successfully", migration.id)
