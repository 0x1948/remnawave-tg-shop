import logging
from datetime import datetime

from db.session import async_session_factory
from db.dal import subscription_dal

async def process_subscription_autorenew(session, sub, bot) -> bool:
    """
    Возвращает:
    - True → сообщение отправлено / попытка была
    - False → ничего не делали
    """

    if not sub.auto_renew_enabled:
        return False

    yookassa_attempted = False
    yookassa_failed = False

    try:
        if sub.provider != "tribute":
            yookassa_attempted = True

            from services.subscription_service import subscription_service

            renewed = await subscription_service.charge_subscription_renewal(
                session, sub
            )

            if renewed:
                await session.commit()
                return False
            else:
                yookassa_failed = True
                await session.rollback()

    except Exception:
        yookassa_failed = True
        await session.rollback()
        logging.exception("Auto-renew failed")

    if yookassa_attempted and yookassa_failed:
        await bot.send_photo(
            chat_id=sub.user_id,
            photo=settings.PHOTO_ID_VPN_DISABLED,
            caption=_("payment_failed_vpn_dis"),
            reply_markup=get_subscribe_only_markup(
                settings.DEFAULT_LANGUAGE, i18n
            )
        )
        return True

    return False

async def check_expired_subscriptions_job(bot):
    async with async_session_factory() as session:
        subs = await subscription_dal.get_subscriptions_for_resend(session)

        for sub in subs:
            try:
                handled = await process_subscription_autorenew(
                    session=session,
                    sub=sub,
                    bot=bot,
                )

                if handled:
                    sub.resend_disable_message_step += 1
                    sub.resend_disable_message_date = datetime.now()
                    await session.commit()

            except Exception as e:
                await session.rollback()
                logging.exception(
                    f"Failed processing subscription {sub.subscription_id} - {repr(e)}"
                )
