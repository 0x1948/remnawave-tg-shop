import logging
import re

from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from typing import Optional, Union

from aiogram.fsm.context import FSMContext
from aiogram.types import InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.markdown import hcode
from sqlalchemy.ext.asyncio import AsyncSession
from bot.services.subscription_service import SubscriptionService

from config.settings import Settings
from bot.services.referral_service import ReferralService
from bot.states.user_states import UserGetRequisites

from db.dal import user_dal, payout_dal

from bot.keyboards.inline.user_keyboards import get_back_to_main_menu_markup, get_create_invite_keyboard
from bot.middlewares.i18n import JsonI18n

router = Router(name="user_referral_router")

SUSPICIOUS_SQL_KEYWORDS_REGEX = re.compile(
    r"\b(DROP\s*TABLE|DELETE\s*FROM|ALTER\s*TABLE|TRUNCATE\s*TABLE|UNION\s*SELECT|"
    r";\s*SELECT|;\s*INSERT|;\s*UPDATE|;\s*DELETE|xp_cmdshell|sysdatabases|sysobjects|INFORMATION_SCHEMA)\b",
    re.IGNORECASE)
SUSPICIOUS_CHARS_REGEX = re.compile(r"(--|#\s|;|\*\/|\/\*)")
MAX_PROMO_CODE_INPUT_LENGTH = 100

async def referral_command_handler(event: Union[types.Message,
                                                types.CallbackQuery],
                                   settings: Settings, i18n_data: dict,
                                   referral_service: ReferralService, bot: Bot,
                                   session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    target_message_obj = event.message if isinstance(
        event, types.CallbackQuery) else event
    if not target_message_obj:
        logging.error(
            "Target message is None in referral_command_handler (possibly from callback without message)."
        )
        if isinstance(event, types.CallbackQuery):
            await event.answer("Error displaying referral info.",
                               show_alert=True)
        return

    if not i18n or not referral_service:
        logging.error(
            "Dependencies (i18n or ReferralService) missing in referral_command_handler"
        )
        await target_message_obj.answer(
            "Service error. Please try again later.")
        if isinstance(event, types.CallbackQuery): await event.answer()
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception as e_bot_info:
        logging.error(
            f"Failed to get bot info for referral link: {e_bot_info}")
        await target_message_obj.answer(_("error_generating_referral_link"))
        if isinstance(event, types.CallbackQuery): await event.answer()
        return

    if not bot_username:
        logging.error("Bot username is None, cannot generate referral link.")
        await target_message_obj.answer(_("error_generating_referral_link"))
        if isinstance(event, types.CallbackQuery): await event.answer()
        return

    inviter_user_id = event.from_user.id
    referral_link = referral_service.generate_referral_link(bot_username, inviter_user_id)

    bonus_info_parts = []
    if settings.subscription_options:

        for months_period_key, _price in sorted(
                settings.subscription_options.items()):

            inv_bonus = settings.referral_bonus_inviter.get(months_period_key)
            ref_bonus = settings.referral_bonus_referee.get(months_period_key)
            if inv_bonus is not None or ref_bonus is not None:
                bonus_info_parts.append(
                    _("referral_bonus_per_period",
                      months=months_period_key,
                      inviter_bonus_days=inv_bonus
                      if inv_bonus is not None else _("no_bonus_placeholder"),
                      referee_bonus_days=ref_bonus
                      if ref_bonus is not None else _("no_bonus_placeholder")))

    bonus_details_str = "\n".join(bonus_info_parts) if bonus_info_parts else _(
        "referral_no_bonuses_configured")

    # Get referral statistics
    referral_stats = await referral_service.get_referral_stats(session, inviter_user_id)

    text = _("referral_program_info_new")

    # referral_link = referral_link,
    # bonus_details = bonus_details_str,
    # invited_count = referral_stats["invited_count"],
    # purchased_count = referral_stats["purchased_count"]

    from bot.keyboards.inline.user_keyboards import get_referral_link_keyboard
    reply_markup_val = get_referral_link_keyboard(current_lang, i18n)

    if isinstance(event, types.Message):
        await event.answer(text,
                           reply_markup=reply_markup_val,
                           disable_web_page_preview=True)
    elif isinstance(event, types.CallbackQuery) and event.message:
        try:
            if settings.PHOTO_ID_GIFT_BRO:
                await event.message.edit_media(media=InputMediaPhoto(media=settings.PHOTO_ID_GIFT_BRO, caption=text), reply_markup=reply_markup_val, disable_web_page_preview=True)
            else:
                await event.message.edit_text(text,
                                          reply_markup=reply_markup_val,
                                          disable_web_page_preview=True)
        except Exception as e_edit:
            logging.warning(
                f"Failed to edit message for referral info: {e_edit}. Sending new one."
            )
            await event.message.answer(text,
                                       reply_markup=reply_markup_val,
                                       disable_web_page_preview=True)
        await event.answer()


async def prompt_get_requisites_handler(callback: types.CallbackQuery,
                                  state: FSMContext, i18n_data: dict,
                                  settings: Settings, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await callback.answer("Language service error.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    if not callback.message:
        logging.error(
            "CallbackQuery has no message in prompt_promo_code_input")
        await callback.answer(_("error_occurred_processing_request"),
                              show_alert=True)
        return

    try:
        if settings.PHOTO_ID_GIFT_BRO:
            await callback.message.edit_media(media=InputMediaPhoto(media=settings.PHOTO_ID_GIFT_BRO, caption=_(key="requisites_prompt")),
                                              reply_markup=get_back_to_main_menu_markup(current_lang, i18n, callback_data="referral_action:my_link"), disable_web_page_preview=True)
        else:
            await callback.message.edit_text(
                text=_(key="requisites_prompt"),
                reply_markup=get_back_to_main_menu_markup(current_lang, i18n, callback_data="main_action:own")
            )
    except Exception as e_edit:
        logging.warning(
            f"Failed to edit message for promo prompt: {e_edit}. Sending new one."
        )
        await callback.message.answer(
            text=_(key="promo_code_prompt"),
            reply_markup=get_back_to_main_menu_markup(current_lang, i18n, callback_data="main_action:own"))

    await callback.answer()
    await state.set_state(UserGetRequisites.waiting_for_requisites)
    logging.info(
        f"User {callback.from_user.id} entered state UserPromoStates.waiting_for_promo_code. "
        f"FSM state: {await state.get_state()}")

@router.message(UserGetRequisites.waiting_for_requisites, F.text)
async def process_requisites_input(message: types.Message, state: FSMContext,
                                   settings: Settings, i18n_data: dict,
                                   subscription_service: SubscriptionService,
                                   bot: Bot, session: AsyncSession):
    logging.info(
        f"Processing requisites input from user {message.from_user.id} in state {await state.get_state()}: '{message.text}'"
    )

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not i18n:
        logging.error(
            "Dependencies (i18n) missing in process_requisites_input"
        )
        await message.reply("Service error. Please try again later.")
        await state.clear()
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    code_input = message.text.strip() if message.text else ""
    user = message.from_user

    is_suspicious = False
    if not code_input:
        is_suspicious = True
        logging.warning(f"Empty requisites input by user {user.id}.")
    elif len(
            code_input
    ) > MAX_PROMO_CODE_INPUT_LENGTH or SUSPICIOUS_SQL_KEYWORDS_REGEX.search(
            code_input) or SUSPICIOUS_CHARS_REGEX.search(code_input):
        is_suspicious = True
        logging.warning(
            f"Suspicious input for requisites by user {user.id} (len: {len(code_input)}): '{code_input}'"
        )

    response_to_user_text = ""
    if is_suspicious:
        if settings.LOG_SUSPICIOUS_ACTIVITY:
            try:
                from bot.services.notification_service import NotificationService
                notification_service = NotificationService(bot, settings, i18n)
                await notification_service.notify_suspicious_promo_attempt(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    suspicious_input=code_input
                )
            except Exception as e:
                logging.error(f"Failed to send suspicious requisites: {e}")

        response_to_user_text = _("requisites_prompt_fail")
        reply_markup = get_back_to_main_menu_markup(current_lang, i18n, callback_data="referral_action:my_link")
    else:
        db_user = await user_dal.get_user_by_id(session, user.id)
        balance = db_user.balance

        await user_dal.update_user(session, user.id, {"balance": 0})

        payout_data = {
            "user_id": user.id,
            "price": balance,
            "requisites": code_input,
            "status": "created"
        }

        new_payout, was_created = await payout_dal.create_payout(session, payout_data)

        from bot.services.notification_service import NotificationService
        notification_service = NotificationService(bot, settings, i18n)
        await notification_service.notify_new_payout(
            user_id=user.id,
            payout_id=new_payout.payout_id,
            price=balance,
            timestamp=new_payout.created_at,
            requisites=code_input,
            username=user.username
        )

        response_to_user_text = _("requisites_payout_good", payout_id=new_payout.payout_id, price=balance)

    if settings.PHOTO_ID_GIFT_BRO:
        await message.answer_photo(
            photo=settings.PHOTO_ID_GIFT_BRO,
            caption=response_to_user_text,
            reply_markup=get_back_to_main_menu_markup(current_lang, i18n, callback_data="referral_action:my_link"),
            disable_web_page_preview=True)
    else:
        await message.answer(
            text=response_to_user_text,
            reply_markup=get_back_to_main_menu_markup(current_lang, i18n, callback_data="main_action:own")
        )
    await state.clear()
    logging.info(
        f"Payout input '{code_input}' processing finished for user {message.from_user.id} (payout_id {new_payout.payout_id}). State cleared."
    )



@router.callback_query(F.data.startswith("referral_action:"))
async def referral_action_handler(callback: types.CallbackQuery, state: FSMContext, settings: Settings,
                                 i18n_data: dict, referral_service: ReferralService, 
                                 bot: Bot, session: AsyncSession):
    action = callback.data.split(":")[1]
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    await state.clear()

    if action == "my_link":
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username
            if not bot_username:
                await callback.answer("Ошибка получения имени бота", show_alert=True)
                return

            db_user = await user_dal.get_user_by_id(session, callback.from_user.id)
            referrals_count = await user_dal.count_referrals(session, callback.from_user.id)

            inviter_user_id = callback.from_user.id
            referral_link = referral_service.generate_referral_link(bot_username, inviter_user_id)

            my_statics_message = _(
                "referral_info_text",
                ref_link=referral_link,
                count_invited=f"{referrals_count} чел.",
                cash_all_time=db_user.total_earned,
                balance=db_user.balance
            )

            kb = get_create_invite_keyboard(current_lang, i18n)

            if settings.PHOTO_ID_GIFT_BRO:
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=settings.PHOTO_ID_GIFT_BRO,
                        caption=my_statics_message
                    ),
                    reply_markup=kb
                )
            else:
                await callback.message.answer(
                    my_statics_message,
                    reply_markup=kb,
                    disable_web_page_preview=True
                )
        except Exception as e:
            logging.error(f"Error in referral MY_LINK: {e}")
            await callback.answer("Произошла ошибка", show_alert=True)
    elif action == "get_payout":
        try:
            db_user = await user_dal.get_user_by_id(session, callback.from_user.id)

            if db_user.balance < 1000:
                await callback.answer("Недостаточно средств! Вывод доступен от 1000 ₽")
            else:
                await prompt_get_requisites_handler(callback, state, i18n_data, settings, session)
        except Exception as e:
            logging.error(f"Error in referral GET_PAYOUT: {e}")
            await callback.answer("Произошла ошибка", show_alert=True)

    elif action == "share_message":
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username
            if not bot_username:
                await callback.answer("Ошибка получения имени бота", show_alert=True)
                return

            inviter_user_id = callback.from_user.id
            referral_link = referral_service.generate_referral_link(bot_username, inviter_user_id)
            
            friend_message = _("referral_friend_message", referral_link=referral_link)

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=_("referral_friend_message_button"), url=referral_link)]
            ])

            if settings.PHOTO_ID_START:
                await callback.message.answer_photo(
                    photo=settings.PHOTO_ID_START,
                    caption=friend_message,
                    reply_markup=kb
                )
            else:
                await callback.message.answer(
                    friend_message,
                    reply_markup=kb,
                    disable_web_page_preview=True
                )

            await callback.message.answer("<i>☝️☝️☝️ Поделись этим сообщением с другом.</i>")
            
        except Exception as e:
            logging.error(f"Error in referral share message: {e}")
            await callback.answer("Произошла ошибка", show_alert=True)
        
    await callback.answer()
