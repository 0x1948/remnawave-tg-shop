import logging
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import update, delete, func, and_, or_
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..models import (
    User,
    Subscription,
    Payment,
    Payout,
    PromoCodeActivation,
    MessageLog,
    UserBilling,
    UserPaymentMethod,
    AdAttribution,
)

async def get_payout_by_id(session: AsyncSession, payout_id: int) -> Optional[User]:
    stmt = select(Payout).where(Payout.payout_id == payout_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def create_payout(session: AsyncSession, payout_data: Dict[str, Any]) -> Tuple[User, bool]:
    """Create a payout in a race-safe way.

    Returns a tuple of (user, created_flag).
    """

    if "created_at" not in payout_data:
        payout_data["created_at"] = datetime.now(timezone.utc)

    stmt = (
        pg_insert(Payout)
        .values(**payout_data)
        .on_conflict_do_nothing(index_elements=[Payout.payout_id])
        .returning(Payout.payout_id)
    )

    result = await session.execute(stmt)
    inserted_row = result.first()
    created = inserted_row is not None

    payout_id: int = inserted_row["payout_id"]
    payout = await get_payout_by_id(session, payout_id)

    if created and payout is not None:
        logging.info(
            f"New payout {payout.payout_id} created in DAL."
        )
    elif payout is not None:
        logging.info(
            f"payout {payout.payout_id} already exists in DAL. Proceeding without creation."
        )

    return payout, created