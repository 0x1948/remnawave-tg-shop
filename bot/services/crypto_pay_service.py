import logging
import json
import random
import string
from datetime import date, timedelta, datetime, timezone
from typing import Optional

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from aiocryptopay import AioCryptoPay, Networks
from aiocryptopay.models.update import Update

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.subscription_service import SubscriptionService
from bot.services.referral_service import ReferralService
from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
from bot.services.notification_service import NotificationService
from db.dal import payment_dal, user_dal, promo_code_dal
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display


class CryptoPayService:
    def __init__(
        self,
        token: Optional[str],
        network: str,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.subscription_service = subscription_service
        self.referral_service = referral_service
        if token:
            net = Networks.TEST_NET if str(network).lower() == "testnet" else Networks.MAIN_NET
            self.client = AioCryptoPay(token=token, network=net)
            self.client.register_pay_handler(self._invoice_paid_handler)
            self.configured = True
        else:
            logging.warning("CryptoPay token not provided. CryptoPay disabled")
            self.client = None
            self.configured = False

    async def close(self):
        """Close underlying AioCryptoPay session if initialized."""
        if self.client:
            try:
                await self.client.close()
                logging.info("CryptoPay client session closed.")
            except Exception as e:
                logging.warning(f"Failed to close CryptoPay client: {e}")

    async def create_invoice(
        self,
        session: AsyncSession,
        user_id: int,
        months: int,
        amount: float,
        description: str,
        is_gift: bool = False
    ):
        if not self.configured or not self.client:
            logging.error("CryptoPayService not configured")
            return None

        # Create pending payment in DB and commit to persist
        try:
            payment_record = await payment_dal.create_payment_record(
                session,
                {
                    "user_id": user_id,
                    "amount": float(amount),
                    "currency": self.settings.CRYPTOPAY_ASSET,
                    "status": "pending_cryptopay",
                    "description": description,
                    "subscription_duration_months": months,
                    "provider": "cryptopay",
                    "is_gift": is_gift
                },
            )
            await session.commit()
        except Exception as e_db_create:
            await session.rollback()
            logging.error(
                f"Failed to create cryptopay payment record for user {user_id}: {e_db_create}",
                exc_info=True,
            )
            return None
        payload = json.dumps({
            "user_id": str(user_id),
            "subscription_months": str(months),
            "payment_db_id": str(payment_record.payment_id),
        })
        try:
            invoice = await self.client.create_invoice(
                amount=amount,
                currency_type=self.settings.CRYPTOPAY_CURRENCY_TYPE,
                fiat=self.settings.CRYPTOPAY_ASSET if self.settings.CRYPTOPAY_CURRENCY_TYPE == "fiat" else None,
                asset=self.settings.CRYPTOPAY_ASSET if self.settings.CRYPTOPAY_CURRENCY_TYPE == "crypto" else None,
                description=description,
                payload=payload,
            )
            try:
                await payment_dal.update_provider_payment_and_status(
                    session,
                    payment_record.payment_id,
                    str(invoice.invoice_id),
                    str(invoice.status),
                )
                await session.commit()
            except Exception as e_db_update:
                await session.rollback()
                logging.error(
                    f"Failed to update cryptopay payment record {payment_record.payment_id}: {e_db_update}",
                    exc_info=True,
                )
                return None
            return invoice.bot_invoice_url, str(invoice.invoice_id)
        except Exception as e:
            logging.error(f"CryptoPay invoice creation failed: {e}", exc_info=True)
            return None

    async def _invoice_paid_handler(self, update: Update, app: web.Application):
        invoice = update.payload
        if not invoice.payload:
            logging.warning("CryptoPay webhook without payload")
            return
        try:
            meta = json.loads(invoice.payload)
            user_id = int(meta["user_id"])
            months = int(meta["subscription_months"])
            payment_db_id = int(meta["payment_db_id"])
        except Exception as e:
            logging.error(f"Failed to parse CryptoPay payload: {e}")
            return

        async_session_factory: sessionmaker = app["async_session_factory"]
        bot: Bot = app["bot"]
        settings: Settings = app["settings"]
        i18n: JsonI18n = app["i18n"]
        subscription_service: SubscriptionService = app["subscription_service"]
        referral_service: ReferralService = app["referral_service"]

        async with async_session_factory() as session:
            db_user = await user_dal.get_user_by_id(session, user_id)
            lang = db_user.language_code if db_user and db_user.language_code else settings.DEFAULT_LANGUAGE
            _ = lambda k, **kw: i18n.gettext(lang, k, **kw)
            try:
                await payment_dal.update_provider_payment_and_status(
                    session,
                    payment_db_id,
                    str(invoice.invoice_id),
                    "succeeded",
                )
                payment = await payment_dal.get_payment_by_db_id(session, payment_db_id)

                if payment.is_gift:
                    today = date.today()
                    new_month = (today.month - 1 + months) % 12 + 1
                    new_year = today.year + (today.month - 1 + months) // 12
                    last_day = (date(new_year + (new_month == 12), new_month % 12 + 1, 1) - timedelta(days=1)).day
                    alphabet = string.ascii_uppercase + string.digits
                    code = ''.join(random.choice(alphabet) for _ in range(10))

                    promo_data = {
                        "code": code,
                        "bonus_days": last_day,
                        "max_activations": 1,
                        "current_activations": 0,
                        "is_active": True,
                        "created_by_admin_id": 0,
                        "created_at": datetime.now(timezone.utc),
                        "valid_until": None
                    }

                    created_promo = await promo_code_dal.create_promo_code(session, promo_data)
                    await session.commit()

                    text = _("payment_successful_gift", link=f"https://t.me/VoronVPNbot?start=promo_{created_promo.codes}",
                             days=last_day)
                    await bot.send_message(user_id, text, parse_mode="HTML")

                    try:
                        notification_service = NotificationService(bot, settings, i18n)
                        user = await user_dal.get_user_by_id(session, user_id)
                        await notification_service.notify_payment_received(
                            user_id=user_id,
                            amount=float(invoice.amount),
                            currency=invoice.asset or settings.DEFAULT_CURRENCY_SYMBOL,
                            months=months,
                            payment_provider="crypto_pay",
                            username=user.username if user else None
                        )
                    except Exception as e:
                        logging.error(f"Failed to send crypto_pay payment notification: {e}")

                    return

                activation = await subscription_service.activate_subscription(
                    session,
                    user_id,
                    months,
                    float(invoice.amount),
                    payment_db_id,
                    provider="cryptopay",
                )
                await session.commit()
            except Exception as e:
                await session.rollback()
                logging.error(f"Failed to process CryptoPay invoice: {e}", exc_info=True)
                return

            config_link = activation.get("subscription_url") or _("config_link_not_available")
            final_end = activation.get("end_date")
            days_left = (final_end.date() - datetime.now().date()).days if final_end else 0
            pay_succesful = True
            text = _(
                "payment_successful_full",
                date=final_end.strftime('%d0-%m-%Y %H:%M'),
                period=max(0, days_left),
                sub_url=config_link
            )

            markup = get_connect_and_main_keyboard(
                lang, i18n, settings, config_link, preserve_message=True
            )
            try:
                if pay_succesful and settings.PHOTO_ID_YOUR_PROF:
                        await bot.send_photo(
                            user_id,
                            photo=settings.PHOTO_ID_YOUR_PROF,
                            caption=text,
                            reply_markup=markup,
                            parse_mode="HTML"
                        )
                else:
                    await bot.send_message(
                        user_id,
                        text,
                        reply_markup=markup,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
            except Exception as e:
                logging.error(f"Failed to send CryptoPay success message: {e}")

            # Send notification about payment
            try:
                notification_service = NotificationService(bot, settings, i18n)
                user = await user_dal.get_user_by_id(session, user_id)
                await notification_service.notify_payment_received(
                    user_id=user_id,
                    amount=float(invoice.amount),
                    currency=invoice.asset or settings.DEFAULT_CURRENCY_SYMBOL,
                    months=months,
                    payment_provider="crypto_pay",
                    username=user.username if user else None
                )
            except Exception as e:
                logging.error(f"Failed to send crypto_pay payment notification: {e}")

    async def webhook_route(self, request: web.Request) -> web.Response:
        if not self.configured or not self.client:
            return web.Response(status=503, text="cryptopay_disabled")
        return await self.client.get_updates(request)


async def cryptopay_webhook_route(request: web.Request) -> web.Response:
    service: CryptoPayService = request.app["cryptopay_service"]
    return await service.webhook_route(request)
