"""
Production-ready Telegram Escrow Bot
Stack: Python 3.11+, aiogram v3, MongoDB (motor), 100% async
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import tempfile
import os
from datetime import datetime, timezone
from typing import Any, Optional

from nudenet import NudeDetector

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("escrow_bot")


# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------

class AdminDashboard(StatesGroup):
    browsing = State()


class ChangeQR(StatesGroup):
    waiting_url = State()


class EditDeal(StatesGroup):
    choose_field = State()
    waiting_value = State()


class ChangeStatus(StatesGroup):
    waiting_status = State()


# ---------------------------------------------------------------------------
# Fee Calculator
# ---------------------------------------------------------------------------

class FeeCalculator:
    """Pure, stateless escrow fee computation."""

    @staticmethod
    def calculate(amount: float) -> float:
        """Return the escrow fee for a given transaction amount (INR)."""
        if amount < 190:
            return 10.0
        elif amount <= 599:
            return 20.0
        elif amount <= 2000:
            return round(amount * 0.035, 2)
        elif amount <= 3000:
            return round(amount * 0.030, 2)
        else:
            return round(amount * 0.030, 2)



# ---------------------------------------------------------------------------
# Settings Service
# ---------------------------------------------------------------------------

class SettingsService:
    """MongoDB settings read/write operations."""

    COLLECTION = "settings"

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db[self.COLLECTION]

    async def get_qr_url(self) -> Optional[str]:
        """Fetch the live QR image URL from the settings collection."""
        doc = await self._col.find_one({})
        if doc and doc.get("qr_image_url"):
            return doc["qr_image_url"]
        return None

    async def set_qr_url(self, url: str) -> None:
        """Persist a new QR image URL to the settings collection."""
        await self._col.update_one(
            {},
            {"$set": {"qr_image_url": url}},
            upsert=True,
        )
        logger.info("QR URL updated to: %s", url)


# ---------------------------------------------------------------------------
# Deal Service
# ---------------------------------------------------------------------------

class DealService:
    """All MongoDB deal CRUD operations, with race-free deal_id assignment."""

    COLLECTION = "deals"

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db[self.COLLECTION]
        self._lock: asyncio.Lock = asyncio.Lock()

    async def _next_deal_id(self) -> int:
        """Atomically compute the next deal_id using asyncio.Lock."""
        async with self._lock:
            last = await self._col.find_one(
                {}, sort=[("deal_id", -1)], projection={"deal_id": 1}
            )
            return (last["deal_id"] + 1) if last else 1

    async def create_deal(
        self,
        seller: str,
        buyer: str,
        details: str,
        amount: float,
        escrow_till: str,
        seller_upi: str,
    ) -> dict[str, Any]:
        """Create and persist a new deal document. Returns the saved document."""
        fee = FeeCalculator.calculate(amount)
        deal_id = await self._next_deal_id()
        doc: dict[str, Any] = {
            "deal_id": deal_id,
            "seller": seller,
            "buyer": buyer,
            "details": details,
            "amount": amount,
            "escrow_till": escrow_till,
            "seller_upi": seller_upi,
            "escrow_fee": fee,
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }
        await self._col.insert_one(doc)
        logger.info("Deal #%d created — amount=%.2f fee=%.2f", deal_id, amount, fee)
        return doc

    async def get_deal(self, deal_id: int) -> Optional[dict[str, Any]]:
        """Fetch a deal by its integer deal_id."""
        return await self._col.find_one({"deal_id": deal_id})

    async def update_status(self, deal_id: int, status: str) -> None:
        """Update the status field of a deal."""
        await self._col.update_one(
            {"deal_id": deal_id}, {"$set": {"status": status}}
        )
        logger.info("Deal #%d status → %s", deal_id, status)

    async def update_field(self, deal_id: int, field: str, value: Any) -> None:
        """Update a single field on a deal document."""
        await self._col.update_one(
            {"deal_id": deal_id}, {"$set": {field: value}}
        )
        logger.info("Deal #%d field '%s' updated", deal_id, field)

    async def delete_deal(self, deal_id: int) -> None:
        """Hard-delete a deal by deal_id."""
        await self._col.delete_one({"deal_id": deal_id})
        logger.info("Deal #%d deleted", deal_id)

    async def list_deals(
        self,
        status_filter: Optional[str] = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a paginated list of deals and the total count."""
        query: dict[str, Any] = {}
        if status_filter:
            query["status"] = status_filter
        total = await self._col.count_documents(query)
        cursor = (
            self._col.find(query)
            .sort("deal_id", -1)
            .skip(skip)
            .limit(limit)
        )
        deals = await cursor.to_list(length=limit)
        return deals, total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FORM_TEMPLATE = config.FORM_TEMPLATE
CHARGES_TEXT = config.CHARGES_TEXT


def _parse_form(text: str) -> Optional[dict[str, Any]]:
    """
    Parse a filled escrow form message into a dict.
    Returns None if any required field is missing or malformed.
    """
    patterns = {
        "seller":      r"Seller:\s*@?(\S+)",
        "buyer":       r"Buyer:\s*@?(\S+)",
        "details":     r"Details:\s*(.+)",
        "amount":      r"Amount:\s*([\d.]+)",
        "escrow_till": r"Escrow Till:\s*(.+)",
        "seller_upi":  r"Seller UPI:\s*(\S+)",
    }
    result: dict[str, Any] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return None
        result[key] = match.group(1).strip()
    try:
        result["amount"] = float(result["amount"])
    except ValueError:
        return None
    # Normalise usernames — strip leading @ if present
    result["seller"] = result["seller"].lstrip("@")
    result["buyer"] = result["buyer"].lstrip("@")
    return result


def _escape_html(text: str) -> str:
    """Escape special HTML characters to prevent parse errors."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _deal_detail_text(deal: dict[str, Any]) -> str:
    created = deal.get("created_at")
    created_str = created.strftime("%Y-%m-%d %H:%M UTC") if isinstance(created, datetime) else str(created or "N/A")
    return (
        f"📄 <b>Deal #{deal['deal_id']}</b>\n"
        f"Status: <code>{_escape_html(deal['status'])}</code>\n"
        f"Seller: @{_escape_html(deal['seller'])}\n"
        f"Buyer: @{_escape_html(deal['buyer'])}\n"
        f"Details: {_escape_html(deal['details'])}\n"
        f"Amount: ₹{deal['amount']:.2f}\n"
        f"Escrow Fee: ₹{deal['escrow_fee']:.2f}\n"
        f"Escrow Till: {_escape_html(deal['escrow_till'])}\n"
        f"Seller UPI: {_escape_html(deal['seller_upi'])}\n"
        f"Created: {created_str}"
    )


def _deal_list_keyboard(
    deals: list[dict[str, Any]],
    total: int,
    page: int,
    status_filter: Optional[str],
    page_size: int = 10,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for deal in deals:
        buttons.append([
            InlineKeyboardButton(
                text=f"#{deal['deal_id']} — {deal['seller']} ↔ {deal['buyer']} [{deal['status']}]",
                callback_data=f"deal_view:{deal['deal_id']}:{page}:{status_filter or 'all'}",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Prev",
            callback_data=f"deal_page:{page - 1}:{status_filter or 'all'}",
        ))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(
            text="Next ➡️",
            callback_data=f"deal_page:{page + 1}:{status_filter or 'all'}",
        ))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="🏠 Back", callback_data="admin_home")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _deal_action_keyboard(deal_id: int, page: int, status_filter: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✏️ Edit Fields",
                callback_data=f"deal_edit:{deal_id}:{page}:{status_filter}",
            ),
            InlineKeyboardButton(
                text="🔄 Change Status",
                callback_data=f"deal_changestatus:{deal_id}:{page}:{status_filter}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🗑 Delete Deal",
                callback_data=f"deal_delete:{deal_id}:{page}:{status_filter}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Back to List",
                callback_data=f"deal_page:{page}:{status_filter}",
            ),
        ],
    ])


def _admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📂 All Deals", callback_data="deal_page:0:all"),
            InlineKeyboardButton(text="🟢 Paid Deals", callback_data="deal_page:0:paid"),
        ],
        [
            InlineKeyboardButton(text="🔴 Cancelled Deals", callback_data="deal_page:0:cancelled"),
            InlineKeyboardButton(text="⚙️ Change QR", callback_data="admin_change_qr"),
        ],
        [
            InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_home"),
        ],
    ])


def _is_admin(user_id: int) -> bool:
    """Check if user is a config-level bot admin (private dashboard access)."""
    return user_id in config.ADMIN_IDS


async def _is_group_admin(message: Message) -> bool:
    """Check if the message sender is an admin/owner of the group."""
    if not message.from_user:
        return False
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# NudeNet detector (singleton — loaded once at startup)
# ---------------------------------------------------------------------------

_nude_detector = NudeDetector()

# ---------------------------------------------------------------------------
# Group Handlers
# ---------------------------------------------------------------------------

class GroupHandlers:
    """aiogram Router handling group-scoped commands (/form, /save, qr<amount>)."""

    def __init__(self, deal_service: DealService, settings_service: SettingsService) -> None:
        self.deal_service = deal_service
        self.settings_service = settings_service
        self.router = Router(name="group")
        self._register()

    def _register(self) -> None:

        # NSFW protection in BOTH groups
        self.router.message.register(
            self.cmd_delete_nsfw,
            F.chat.id.in_({config.MAIN_GROUP_ID, config.ESCROW_GROUP_ID}),
            F.content_type.in_({"photo", "document"}),
        )

        # Main group — ANY message (text) triggers inline buttons
        self.router.message.register(
            self.cmd_escrow_redirect,
            F.chat.id == config.MAIN_GROUP_ID,
            F.text.as_("text"),
        )

        # New member joins
        self.router.message.register(
            self.cmd_new_member_main,
            F.chat.id == config.MAIN_GROUP_ID,
            F.content_type == "new_chat_members",
        )
        self.router.message.register(
            self.cmd_new_member_escrow,
            F.chat.id == config.ESCROW_GROUP_ID,
            F.content_type == "new_chat_members",
        )

        # Escrow group — open to ALL members
        self.router.message.register(self.cmd_form, F.chat.id == config.ESCROW_GROUP_ID, F.text.lower() == "form")
        self.router.message.register(self.cmd_charges, F.chat.id == config.ESCROW_GROUP_ID, F.text.lower() == "fee")
        self.router.message.register(self.cmd_charges, F.chat.id == config.ESCROW_GROUP_ID, F.text.lower() == "fees")
        # Escrow group — group admin only
        self.router.message.register(self.cmd_save, F.chat.id == config.ESCROW_GROUP_ID, F.text.lower() == "save")
        self.router.message.register(self.cmd_pin, F.chat.id == config.ESCROW_GROUP_ID, F.text.lower() == "pin")
        self.router.message.register(self.cmd_help, F.chat.id == config.ESCROW_GROUP_ID, F.text.lower() == "help")
        # qr<amount> or qr<amount>:<deal_id> plain text only
        self.router.message.register(self.cmd_qr, F.chat.id == config.ESCROW_GROUP_ID, F.text.regexp(r"(?i)^qr[\d.]+(?:[:#]\d+)?$"))

    async def cmd_form(self, message: Message) -> None:
        """Anyone: send the escrow deal form template."""
        await message.reply(FORM_TEMPLATE, parse_mode=ParseMode.MARKDOWN)

    async def cmd_charges(self, message: Message) -> None:
        """Anyone: show the escrow fee structure."""
        await message.reply(CHARGES_TEXT, parse_mode=ParseMode.HTML)

    async def cmd_delete_nsfw(self, message: Message) -> None:
        """Main group: detect and delete NSFW images using NudeNet."""
        # Only process photos (and documents that are images)
        file_id = None
        if message.photo:
            file_id = message.photo[-1].file_id  # largest size
        elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
            file_id = message.document.file_id

        if not file_id:
            # Not a detectable image (video/sticker/gif) — skip
            return

        try:
            # Download the image to a temp file
            bot_file = await message.bot.get_file(file_id)
            file_bytes = await message.bot.download_file(bot_file.file_path)

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(file_bytes.read())
                tmp_path = tmp.name

            # Run NudeNet detection in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            detections = await loop.run_in_executor(
                None, _nude_detector.detect, tmp_path
            )
            os.unlink(tmp_path)

            # NSFW labels that warrant deletion
            nsfw_labels = {
                "FEMALE_GENITALIA_EXPOSED",
                "MALE_GENITALIA_EXPOSED",
                "FEMALE_BREAST_EXPOSED",
                "ANUS_EXPOSED",
                "BUTTOCKS_EXPOSED",
            }
            is_nsfw = any(
                d.get("class") in nsfw_labels and d.get("score", 0) >= 0.5
                for d in detections
            )

            if not is_nsfw:
                return

            # Delete the NSFW message
            await message.delete()

            user = message.from_user
            mention = f'<a href="tg://user?id={user.id}">{_escape_html(user.full_name)}</a>' if user else "User"
            warn_msg = await message.answer(
                f"🚫 {mention}, NSFW content is not allowed in this group and has been removed.",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(5)
            await warn_msg.delete()

        except TelegramBadRequest as e:
            logger.warning("Could not process/delete NSFW message: %s", e)
        except Exception:
            logger.exception("NSFW detection error")

    async def cmd_escrow_redirect(self, message: Message) -> None:
        """Main group: reply to any text with escrow group + channel buttons."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔒 Escrow Group", url=config.ESCROW_GROUP_LINK),
                InlineKeyboardButton(text="📢 Main Channel", url=config.MAIN_CHANNEL_LINK),
            ]
        ])
        await message.reply(
            "👇 Use the buttons below to join our Escrow Group or visit the Main Channel:",
            reply_markup=keyboard,
        )

    async def cmd_new_member_main(self, message: Message) -> None:
        """Main group: greet new members with inline buttons."""
        if not message.new_chat_members:
            return
        for member in message.new_chat_members:
            if member.is_bot:
                continue
            mention = f'<a href="tg://user?id={member.id}">{_escape_html(member.full_name)}</a>'
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔒 Escrow Group", url=config.ESCROW_GROUP_LINK),
                    InlineKeyboardButton(text="📢 Main Channel", url=config.MAIN_CHANNEL_LINK),
                ]
            ])
            await message.answer(
                config.GREET_MAIN_GROUP.format(mention=mention),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

    async def cmd_new_member_escrow(self, message: Message) -> None:
        """Escrow group: greet new members with available commands and group/channel buttons."""
        if not message.new_chat_members:
            return
        for member in message.new_chat_members:
            if member.is_bot:
                continue
            mention = f'<a href="tg://user?id={member.id}">{_escape_html(member.full_name)}</a>'
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="💬 Main Group", url=config.MAIN_GROUP_LINK),
                    InlineKeyboardButton(text="📢 Main Channel", url=config.MAIN_CHANNEL_LINK),
                ]
            ])
            await message.answer(
                config.GREET_ESCROW_GROUP.format(mention=mention),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

    async def cmd_help(self, message: Message) -> None:
        """Group admin only: show all available plain text commands."""
        if not await _is_group_admin(message):
            return
        help_text = (
            "📖 <b>Escrow Bot — Commands</b>\n\n"
            "<b>For Everyone:</b>\n"
            "• <code>form</code> — Get the escrow deal form template\n"
            "• <code>fee</code> / <code>fees</code> — View the escrow fee structure\n\n"
            "<b>Group Admins Only:</b>\n"
            "• <code>save</code> — Reply to a filled form to save the deal\n"
            "• <code>qr&lt;amount&gt;</code> — Reply to save confirmation to send QR\n"
            "   e.g. <code>qr500</code> or <code>qr500:1</code> (deal ID = 1)\n"
            "• <code>pin</code> — Reply to any message to pin it\n"
            "• <code>help</code> — Show this help message\n\n"
            "<b>Bot Admins (Private Chat):</b>\n"
            "• /start — Open the admin dashboard\n"
            "   — View, edit, delete deals\n"
            "   — Change deal status\n"
            "   — Update QR image URL"
        )
        await message.reply(help_text, parse_mode=ParseMode.HTML)

    async def cmd_pin(self, message: Message) -> None:
        """Group admin only: pin the replied-to message in the group."""
        if not await _is_group_admin(message):
            return
        if not message.reply_to_message:
            await message.reply("⚠️ Reply to a message to pin it.")
            return
        try:
            await message.bot.pin_chat_message(
                chat_id=message.chat.id,
                message_id=message.reply_to_message.message_id,
                disable_notification=False,
            )
            await message.delete()
        except TelegramBadRequest as e:
            logger.warning("Could not pin message: %s", e)
            await message.reply("❌ Failed to pin. Make sure I am an admin with pin permission.")

    async def cmd_save(self, message: Message) -> None:
        """Group admin only: parse a replied-to form message and save the deal."""
        if not await _is_group_admin(message):
            return

        if not message.reply_to_message or not message.reply_to_message.text:
            await message.reply("⚠️ Please reply to a filled form message with /save.")
            return

        parsed = _parse_form(message.reply_to_message.text)
        if not parsed:
            await message.reply(
                "⚠️ Could not parse the form. Ensure all fields are present:\n"
                "Seller, Buyer, Details, Amount, Escrow Till, Seller UPI."
            )
            return

        try:
            deal = await self.deal_service.create_deal(
                seller=parsed["seller"],
                buyer=parsed["buyer"],
                details=parsed["details"],
                amount=parsed["amount"],
                escrow_till=parsed["escrow_till"],
                seller_upi=parsed["seller_upi"],
            )
        except Exception:
            logger.exception("Failed to create deal")
            await message.reply("❌ Database error while saving the deal. Please try again.")
            return

        await message.reply(
            f"✅ <b>Deal Saved Successfully</b>\n"
            f"Deal ID: #{deal['deal_id']}\n"
            f"Status: Pending",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_qr(self, message: Message) -> None:
        """Group admin only: send QR payment request for a deal."""
        if not await _is_group_admin(message):
            return

        if not message.text:
            return

        # Extract the amount from the command (e.g. "qr50" → 50.0)
        match = re.match(r"^qr([\d.]+)", message.text.strip(), re.IGNORECASE)
        if not match:
            return
        try:
            payment_amount = float(match.group(1))
        except ValueError:
            await message.reply("⚠️ Invalid amount in qr command.")
            return

        # Retrieve deal_id from the replied-to message (search both text and caption)
        deal_id: Optional[int] = None
        reply_msg = message.reply_to_message
        if reply_msg:
            search_text = reply_msg.text or reply_msg.caption or ""
            id_match = re.search(r"Deal\s*ID[:\s#]*(\d+)", search_text, re.IGNORECASE)
            if id_match:
                deal_id = int(id_match.group(1))

        # Also allow inline deal_id in command: qr500:1 or qr500#1
        if deal_id is None and message.text:
            inline_match = re.search(r"[:#](\d+)$", message.text.strip())
            if inline_match:
                deal_id = int(inline_match.group(1))

        if deal_id is None:
            await message.reply(
                "⚠️ Reply to the deal save confirmation message, or include the deal ID in the command.\n"
                "Examples: qr500 (reply to save msg) or qr500:1 (deal ID = 1)"
            )
            return

        deal = await self.deal_service.get_deal(deal_id)
        if not deal:
            await message.reply(f"⚠️ Deal #{deal_id} not found in database.")
            return

        total = round(payment_amount + deal["escrow_fee"], 2)
        qr_url = await self.settings_service.get_qr_url()
        if not qr_url:
            await message.reply("❌ No QR image set. Use the admin dashboard (⚙️ Change QR) to set one.")
            return

        caption = (
            f"Deal ID: #{deal_id}\n"
            f"@{deal['buyer']}\n\n"
            f"Pay ₹{payment_amount:.2f} + Escrow Fee ₹{deal['escrow_fee']:.2f} = ₹{total:.2f}\n"
            f"on this QR."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Paid", callback_data=f"pay_confirm:{deal_id}"),
                InlineKeyboardButton(text="❌ Cancel", callback_data=f"pay_cancel:{deal_id}"),
            ]
        ])

        try:
            await message.answer_photo(
                photo=qr_url,
                caption=caption,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to send QR photo for deal #%d", deal_id)
            await message.reply("❌ Could not send QR image. Check the QR URL in settings.")


# ---------------------------------------------------------------------------
# Callback Handlers
# ---------------------------------------------------------------------------

class CallbackHandlers:
    """aiogram Router handling all inline keyboard callbacks."""

    def __init__(self, bot: Bot, deal_service: DealService, settings_service: SettingsService) -> None:
        self.bot = bot
        self.deal_service = deal_service
        self.settings_service = settings_service
        self.router = Router(name="callbacks")
        self._register()

    def _register(self) -> None:
        self.router.callback_query.register(self.cb_pay_confirm, F.data.startswith("pay_confirm:"))
        self.router.callback_query.register(self.cb_pay_cancel, F.data.startswith("pay_cancel:"))
        self.router.callback_query.register(self.cb_admin_home, F.data == "admin_home")
        self.router.callback_query.register(self.cb_deal_page, F.data.startswith("deal_page:"))
        self.router.callback_query.register(self.cb_deal_view, F.data.startswith("deal_view:"))
        self.router.callback_query.register(self.cb_deal_edit, F.data.startswith("deal_edit:"))
        self.router.callback_query.register(self.cb_deal_changestatus, F.data.startswith("deal_changestatus:"))
        self.router.callback_query.register(self.cb_deal_delete, F.data.startswith("deal_delete:"))
        self.router.callback_query.register(self.cb_admin_change_qr, F.data == "admin_change_qr")

    async def _admin_guard(self, cq: CallbackQuery) -> bool:
        """Return True if the user is an admin; otherwise alert and return False."""
        if not cq.from_user or not _is_admin(cq.from_user.id):
            await cq.answer("⛔ Admins only.", show_alert=True)
            return False
        return True

    async def cb_pay_confirm(self, cq: CallbackQuery) -> None:
        """Mark deal as paid and DM the seller."""
        if not await self._admin_guard(cq):
            return
        deal_id = int(cq.data.split(":")[1])
        await self.deal_service.update_status(deal_id, "paid")

        deal = await self.deal_service.get_deal(deal_id)
        if deal and cq.message:
            await self.bot.send_message(
                chat_id=cq.message.chat.id,
                text=(
                    f"✅ Payment received for Deal #{deal_id}\n"
                    f"@{deal['seller']} — Buyer @{deal['buyer']} has paid.\n"
                    f"Please proceed with the transfer."
                ),
            )

        await cq.answer("✅ Deal marked as paid.")
        if cq.message:
            try:
                await cq.message.edit_caption(
                    caption=(cq.message.caption or "") + "\n\n✅ Payment confirmed.",
                    reply_markup=None,
                )
            except TelegramBadRequest:
                pass

    async def cb_pay_cancel(self, cq: CallbackQuery) -> None:
        """Mark deal as cancelled and edit QR message."""
        if not await self._admin_guard(cq):
            return
        deal_id = int(cq.data.split(":")[1])
        await self.deal_service.update_status(deal_id, "cancelled")
        await cq.answer("❌ Payment cancelled.")
        if cq.message:
            try:
                await cq.message.edit_caption(
                    caption="❌ Payment Cancelled.",
                    reply_markup=None,
                )
            except TelegramBadRequest:
                pass

    async def cb_admin_home(self, cq: CallbackQuery, state: FSMContext) -> None:
        """Show the admin dashboard home."""
        if not await self._admin_guard(cq):
            return
        await state.clear()
        if cq.message:
            try:
                await cq.message.edit_text(
                    "🏠 <b>Admin Dashboard</b>\nSelect an option:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_admin_home_keyboard(),
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def cb_deal_page(self, cq: CallbackQuery) -> None:
        """Paginated deal list."""
        if not await self._admin_guard(cq):
            return
        _, page_str, status_filter = cq.data.split(":", 2)
        page = int(page_str)
        filter_arg = None if status_filter == "all" else status_filter
        deals, total = await self.deal_service.list_deals(
            status_filter=filter_arg, skip=page * 10, limit=10
        )
        label = status_filter.capitalize() if status_filter != "all" else "All"
        text = f"📋 <b>{label} Deals</b> (page {page + 1}) — {total} total"
        keyboard = _deal_list_keyboard(deals, total, page, filter_arg)
        if cq.message:
            try:
                await cq.message.edit_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def cb_deal_view(self, cq: CallbackQuery) -> None:
        """Show full detail view for a single deal."""
        if not await self._admin_guard(cq):
            return
        parts = cq.data.split(":")
        deal_id = int(parts[1])
        page = int(parts[2])
        status_filter = parts[3]
        deal = await self.deal_service.get_deal(deal_id)
        if not deal:
            await cq.answer("Deal not found.", show_alert=True)
            return
        if cq.message:
            try:
                await cq.message.edit_text(
                    _deal_detail_text(deal),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_deal_action_keyboard(deal_id, page, status_filter),
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def cb_deal_edit(self, cq: CallbackQuery, state: FSMContext) -> None:
        """Begin FSM flow: choose field to edit."""
        if not await self._admin_guard(cq):
            return
        parts = cq.data.split(":")
        deal_id = int(parts[1])
        page = int(parts[2])
        status_filter = parts[3]
        await state.set_state(EditDeal.choose_field)
        await state.update_data(deal_id=deal_id, page=page, status_filter=status_filter)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Seller", callback_data="editfield:seller"),
                InlineKeyboardButton(text="Buyer", callback_data="editfield:buyer"),
            ],
            [
                InlineKeyboardButton(text="Details", callback_data="editfield:details"),
                InlineKeyboardButton(text="Amount", callback_data="editfield:amount"),
            ],
            [
                InlineKeyboardButton(text="Escrow Till", callback_data="editfield:escrow_till"),
                InlineKeyboardButton(text="Seller UPI", callback_data="editfield:seller_upi"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="editfield:cancel")],
        ])
        if cq.message:
            try:
                await cq.message.edit_text(
                    f"✏️ <b>Edit Deal #{deal_id}</b>\nChoose field to edit:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def cb_deal_changestatus(self, cq: CallbackQuery, state: FSMContext) -> None:
        """Begin FSM flow: change deal status."""
        if not await self._admin_guard(cq):
            return
        parts = cq.data.split(":")
        deal_id = int(parts[1])
        page = int(parts[2])
        status_filter = parts[3]
        await state.set_state(ChangeStatus.waiting_status)
        await state.update_data(deal_id=deal_id, page=page, status_filter=status_filter)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏳ Pending", callback_data="setstatus:pending"),
                InlineKeyboardButton(text="✅ Paid", callback_data="setstatus:paid"),
                InlineKeyboardButton(text="❌ Cancelled", callback_data="setstatus:cancelled"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="setstatus:cancel")],
        ])
        if cq.message:
            try:
                await cq.message.edit_text(
                    f"🔄 <b>Change Status — Deal #{deal_id}</b>\nSelect new status:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def cb_deal_delete(self, cq: CallbackQuery) -> None:
        """Delete a deal with confirmation."""
        if not await self._admin_guard(cq):
            return
        parts = cq.data.split(":")
        deal_id = int(parts[1])
        page = int(parts[2])
        status_filter = parts[3]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Yes, Delete",
                    callback_data=f"deal_delete_confirm:{deal_id}:{page}:{status_filter}",
                ),
                InlineKeyboardButton(
                    text="❌ No, Go Back",
                    callback_data=f"deal_view:{deal_id}:{page}:{status_filter}",
                ),
            ]
        ])
        if cq.message:
            try:
                await cq.message.edit_text(
                    f"🗑 Are you sure you want to <b>delete Deal #{deal_id}</b>? This is irreversible.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def cb_admin_change_qr(self, cq: CallbackQuery, state: FSMContext) -> None:
        """Begin FSM flow: change QR URL."""
        if not await self._admin_guard(cq):
            return
        await state.set_state(ChangeQR.waiting_url)
        if cq.message:
            try:
                await cq.message.edit_text(
                    "⚙️ <b>Change QR Image URL</b>\nSend the new image URL now:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_home")]
                    ]),
                )
            except TelegramBadRequest:
                pass
        await cq.answer()


# ---------------------------------------------------------------------------
# Admin Handlers (private chat)
# ---------------------------------------------------------------------------

class AdminHandlers:
    """aiogram Router for private admin dashboard and FSM message handlers."""

    def __init__(
        self,
        deal_service: DealService,
        settings_service: SettingsService,
    ) -> None:
        self.deal_service = deal_service
        self.settings_service = settings_service
        self.router = Router(name="admin")
        self._register()

    def _register(self) -> None:
        self.router.message.register(self.cmd_start, CommandStart())
        # FSM: ChangeQR
        self.router.message.register(self.fsm_change_qr_url, ChangeQR.waiting_url)
        # FSM: EditDeal — field selection via inline (handled in CallbackHandlers),
        #                  value input via message
        self.router.callback_query.register(
            self.cb_editfield_choose, F.data.startswith("editfield:")
        )
        self.router.message.register(self.fsm_edit_deal_value, EditDeal.waiting_value)
        # FSM: ChangeStatus via inline
        self.router.callback_query.register(
            self.cb_setstatus, F.data.startswith("setstatus:")
        )
        # Delete confirmation
        self.router.callback_query.register(
            self.cb_deal_delete_confirm, F.data.startswith("deal_delete_confirm:")
        )

    async def cmd_start(self, message: Message, state: FSMContext) -> None:
        """Private /start — show admin dashboard or deny access."""
        if not message.from_user or not _is_admin(message.from_user.id):
            await message.answer("⛔ Access Denied")
            return
        await state.clear()
        await message.answer(
            "🏠 <b>Admin Dashboard</b>\nSelect an option:",
            parse_mode=ParseMode.HTML,
            reply_markup=_admin_home_keyboard(),
        )

    async def fsm_change_qr_url(self, message: Message, state: FSMContext) -> None:
        """Receive new QR URL and persist it."""
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        url = (message.text or "").strip()
        if not url.startswith("http"):
            await message.answer("⚠️ Please provide a valid URL starting with http/https.")
            return
        await self.settings_service.set_qr_url(url)
        await state.clear()
        await message.answer(
            f"✅ QR URL updated successfully.\n<code>{url}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_admin_home_keyboard(),
        )

    async def cb_editfield_choose(self, cq: CallbackQuery, state: FSMContext) -> None:
        """Handle field selection for deal edit FSM."""
        if not cq.from_user or not _is_admin(cq.from_user.id):
            await cq.answer("⛔ Admins only.", show_alert=True)
            return
        field = cq.data.split(":")[1]
        if field == "cancel":
            data = await state.get_data()
            await state.clear()
            deal_id = data.get("deal_id", 0)
            page = data.get("page", 0)
            status_filter = data.get("status_filter", "all")
            if cq.message:
                try:
                    await cq.message.edit_text(
                        "❌ Edit cancelled.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="⬅️ Back",
                                callback_data=f"deal_view:{deal_id}:{page}:{status_filter}",
                            )]
                        ]),
                    )
                except TelegramBadRequest:
                    pass
            await cq.answer()
            return

        await state.update_data(edit_field=field)
        await state.set_state(EditDeal.waiting_value)
        labels = {
            "seller": "Seller @username",
            "buyer": "Buyer @username",
            "details": "Deal details",
            "amount": "Amount (number)",
            "escrow_till": "Escrow Till (date/condition)",
            "seller_upi": "Seller UPI handle",
        }
        if cq.message:
            try:
                await cq.message.edit_text(
                    f"✏️ Send the new value for <b>{labels.get(field, field)}</b>:",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def fsm_edit_deal_value(self, message: Message, state: FSMContext) -> None:
        """Receive new field value and persist it."""
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        data = await state.get_data()
        deal_id: int = data["deal_id"]
        field: str = data["edit_field"]
        page: int = data.get("page", 0)
        status_filter: str = data.get("status_filter", "all")
        raw = (message.text or "").strip()
        value: Any = raw

        if field == "amount":
            try:
                value = float(raw)
            except ValueError:
                await message.answer("⚠️ Amount must be a number. Try again:")
                return
            # Recalculate fee
            new_fee = FeeCalculator.calculate(value)
            await self.deal_service.update_field(deal_id, "escrow_fee", new_fee)

        if field in ("seller", "buyer"):
            value = value.lstrip("@")

        await self.deal_service.update_field(deal_id, field, value)
        await state.clear()
        deal = await self.deal_service.get_deal(deal_id)
        text = _deal_detail_text(deal) if deal else f"Deal #{deal_id} updated."
        await message.answer(
            f"✅ Field updated.\n\n{text}",
            parse_mode=ParseMode.HTML,
            reply_markup=_deal_action_keyboard(deal_id, page, status_filter),
        )

    async def cb_setstatus(self, cq: CallbackQuery, state: FSMContext) -> None:
        """Handle status selection for ChangeStatus FSM."""
        if not cq.from_user or not _is_admin(cq.from_user.id):
            await cq.answer("⛔ Admins only.", show_alert=True)
            return
        new_status = cq.data.split(":")[1]
        data = await state.get_data()
        deal_id: int = data.get("deal_id", 0)
        page: int = data.get("page", 0)
        status_filter: str = data.get("status_filter", "all")

        if new_status == "cancel":
            await state.clear()
            if cq.message:
                try:
                    await cq.message.edit_text(
                        "❌ Status change cancelled.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="⬅️ Back",
                                callback_data=f"deal_view:{deal_id}:{page}:{status_filter}",
                            )]
                        ]),
                    )
                except TelegramBadRequest:
                    pass
            await cq.answer()
            return

        await self.deal_service.update_status(deal_id, new_status)
        await state.clear()
        deal = await self.deal_service.get_deal(deal_id)
        if cq.message:
            try:
                await cq.message.edit_text(
                    f"✅ Status updated to <code>{new_status}</code>.\n\n{_deal_detail_text(deal) if deal else ''}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_deal_action_keyboard(deal_id, page, status_filter),
                )
            except TelegramBadRequest:
                pass
        await cq.answer()

    async def cb_deal_delete_confirm(self, cq: CallbackQuery, state: FSMContext) -> None:
        """Confirm and execute deal deletion."""
        if not cq.from_user or not _is_admin(cq.from_user.id):
            await cq.answer("⛔ Admins only.", show_alert=True)
            return
        parts = cq.data.split(":")
        deal_id = int(parts[1])
        page = int(parts[2])
        status_filter = parts[3]
        await self.deal_service.delete_deal(deal_id)
        await state.clear()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⬅️ Back to List",
                callback_data=f"deal_page:{page}:{status_filter}",
            )]
        ])
        if cq.message:
            try:
                await cq.message.edit_text(
                    f"🗑 Deal #{deal_id} has been deleted.",
                    reply_markup=keyboard,
                )
            except TelegramBadRequest:
                pass
        await cq.answer("Deleted.")


# ---------------------------------------------------------------------------
# Scope Enforcement Middleware
# ---------------------------------------------------------------------------

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from typing import Callable, Awaitable


class GroupScopeMiddleware(BaseMiddleware):
    """
    Silently drop any update that originates from a group/supergroup
    that is NOT the configured ALLOWED_GROUP_ID.

    Private chats (user ↔ bot) are always allowed through — they are
    handled separately by AdminHandlers with their own access control.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            # Determine the chat the event originates from
            chat = None
            if event.message:
                chat = event.message.chat
            elif event.callback_query and event.callback_query.message:
                chat = event.callback_query.message.chat

            if chat and chat.type in ("group", "supergroup"):
                allowed = {config.ESCROW_GROUP_ID, config.MAIN_GROUP_ID}
                if chat.id not in allowed:
                    logger.debug("Ignored update from disallowed group %d", chat.id)
                    return  # silently drop

        return await handler(event, data)


# ---------------------------------------------------------------------------
# BotApp — application entry point and lifecycle
# ---------------------------------------------------------------------------

class BotApp:
    """Application entry point: wires all services, routers, and starts polling."""

    def __init__(self) -> None:
        self.bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)

        # MongoDB
        self._mongo_client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AsyncIOMotorDatabase] = None

        # Services
        self.deal_service: Optional[DealService] = None
        self.settings_service: Optional[SettingsService] = None

    async def _setup_db(self) -> None:
        """Initialise the Motor client and service instances."""
        self._mongo_client = AsyncIOMotorClient(config.MONGO_URI)
        self._db = self._mongo_client[config.DB_NAME]
        self.deal_service = DealService(self._db)
        self.settings_service = SettingsService(self._db)
        logger.info("MongoDB connected — db: %s", config.DB_NAME)

    async def _setup_indexes(self) -> None:
        """Create necessary database indexes."""
        assert self._db is not None
        await self._db["deals"].create_index("deal_id", unique=True)
        logger.info("Database indexes ensured.")

    def _build_routers(self) -> None:
        """Instantiate handlers and include their routers in the dispatcher."""
        assert self.deal_service and self.settings_service

        group_h = GroupHandlers(self.deal_service, self.settings_service)
        callback_h = CallbackHandlers(self.bot, self.deal_service, self.settings_service)
        admin_h = AdminHandlers(self.deal_service, self.settings_service)

        # Middleware is applied at the dispatcher level
        self.dp.update.middleware(GroupScopeMiddleware())

        # Include routers — order matters for FSM fallthrough
        self.dp.include_router(admin_h.router)
        self.dp.include_router(callback_h.router)
        self.dp.include_router(group_h.router)

    async def start(self) -> None:
        """Run the bot (blocking until stopped)."""
        await self._setup_db()
        await self._setup_indexes()
        self._build_routers()
        logger.info("Starting Escrow Bot polling…")
        try:
            await self.dp.start_polling(self.bot, allowed_updates=["message", "callback_query", "channel_post"])
        finally:
            if self._mongo_client:
                self._mongo_client.close()
                logger.info("MongoDB connection closed.")
            await self.bot.session.close()
            logger.info("Bot session closed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(BotApp().start())

