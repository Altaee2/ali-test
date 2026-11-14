# -*- coding: utf-8 -*-
"""
بوت السايت (رسائل مجهولة) — Telebot + APScheduler + JSON
=========================================================
المتطلبات:
    pip install "pyTelegramBotAPI>=4.21" "APScheduler>=3.10,<4"

التشغيل:
    export BOT_TOKEN="123456:ABC..."
    python site_bot.py

ملاحظات مهمة:
 - يجب أن يكون البوت مشرفاً في قناة الاشتراك الإجباري (@alii_index) ليتمكن من فحص الاشتراك.
 - يجب أن يكون البوت مشرفاً (صلاحية النشر) في قناة النشر التي يعيّنها المستخدم.
 - تفاعلات الرسائل (message_reaction) مفعّلة عبر allowed_updates عند بدء الـ polling.
"""

import atexit
import copy
import html
import json
import logging
import os
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError

# ======================================================================
# 1) الإعدادات الثابتة
# ======================================================================
BOT_TOKEN = "9106605412:AAEWiWzgqObgCQVRP8B3Hlzgsx-NYAnW7Xs"
ADMIN_ID = 6454550864
DATA_DIR = "data"
SESSION_TTL_HOURS = 6  # مدة الخمول قبل إنهاء الجلسة تلقائياً
MSG_MAP_LIMIT = 3000  # أقصى عدد رسائل نحتفظ بخرائطها (للتفاعلات والأزرار)
FAVORITES_LIMIT = 100
SIGNATURE_TEXT = "بوت السايت"  # نص توقيع البوت في منشورات القناة

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("site_bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8)
scheduler = BackgroundScheduler(
    job_defaults={"misfire_grace_time": 3600, "coalesce": True}
)

BOT_USERNAME = ""  # تُعبّأ عند التشغيل
BOT_ID = 0
NO_PREVIEW = types.LinkPreviewOptions(is_disabled=True)  # منع معاينة الروابط


# ======================================================================
# 2) طبقة التخزين: ملفات JSON (آمنة للخيوط + كتابة ذرّية)
# ======================================================================
class JsonStore:
    """مخزن بسيط فوق ملف JSON. save(now=False) يؤجّل الكتابة لمهمة دورية."""

    def __init__(self, filename, default):
        self.path = os.path.join(DATA_DIR, filename)
        self.default = default
        self.lock = threading.RLock()
        self.dirty = False
        self.data = self._load()

    def _load(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # إكمال المفاتيح الافتراضية الناقصة (مفيد عند تحديث البوت)
                if isinstance(self.default, dict) and isinstance(data, dict):
                    for k, v in self.default.items():
                        data.setdefault(k, copy.deepcopy(v))
                return data
            except Exception:
                log.exception(
                    "تعذر قراءة %s — سيتم نسخه احتياطياً وإنشاء ملف جديد", self.path
                )
                os.replace(self.path, self.path + ".broken")
        data = copy.deepcopy(self.default)
        self.data = data
        self._write()
        return data

    def _write(self):
        for _ in range(3):  # إعادة المحاولة إذا تغيّر القاموس أثناء الكتابة
            try:
                with self.lock:
                    tmp = self.path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(self.data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, self.path)
                    self.dirty = False
                return
            except RuntimeError:
                time.sleep(0.05)

    def save(self, now=True):
        if now:
            self._write()
        else:
            self.dirty = True

    def flush(self):
        if self.dirty:
            self._write()


DEFAULT_CONFIG = {
    "maintenance": False,
    "force_channels": ["@alii_index"],
    "force_message": {
        "ar": "⚠️ <b>اشتراك إجباري</b>\n\nيجب عليك الاشتراك في القناة أولاً لتتمكن من استخدام البوت، ثم اضغط «تحقق».",
        "en": "⚠️ <b>Mandatory subscription</b>\n\nPlease join the channel first to use the bot, then press “Check”.",
    },
    "policy": {
        "ar": "📜 <b>سياسة الاستخدام</b>\n\n1) يُمنع إرسال محتوى مسيء أو تهديدات أو ابتزاز.\n2) يُمنع انتهاك الخصوصية أو نشر معلومات شخصية.\n3) الإدارة غير مسؤولة عن محتوى الرسائل ولها حق الحظر.\n4) باستخدامك للبوت فأنت توافق على هذه الشروط.",
        "en": "📜 <b>Usage policy</b>\n\n1) No abusive content, threats or blackmail.\n2) No privacy violations or sharing personal data.\n3) Admins are not responsible for message content and may ban users.\n4) By using the bot you accept these terms.",
    },
    "faq": {
        "ar": "❓ <b>الأسئلة الشائعة</b>\n\n<b>كيف أستقبل رسائل مجهولة؟</b>\nأنشئ رابطك وشاركه.\n\n<b>هل يعرف صاحب الرابط هويتي؟</b>\nلا، الهوية مخفية دائماً.\n\n<b>كم تدوم الجلسة؟</b>\nتنتهي بعد 6 ساعات من الخمول.",
        "en": "❓ <b>FAQ</b>\n\n<b>How do I receive anonymous messages?</b>\nCreate your link and share it.\n\n<b>Can the link owner see who I am?</b>\nNo, your identity stays hidden.\n\n<b>How long does a session last?</b>\nIt ends after 6 hours of inactivity.",
    },
    # حقوق البوت: تُلحق بالرسائل الصادرة عبر with_credits() على شكل رابط HTML
    "credits": {"text": "بوت السايت", "url": "https://t.me/alii_index"},
}

config = JsonStore("config.json", DEFAULT_CONFIG)
users = JsonStore(
    "users.json", {}
)  # {uid: {lang, banned, publish_channel, favorites, ...}}
links = JsonStore("links.json", {})  # {code: {owner, created}}
sessions = JsonStore("sessions.json", {"sessions": {}, "msg_map": {}, "pending": {}})


def flush_all():
    for store in (config, users, links, sessions):
        store.flush()


# ======================================================================
# 3) النصوص (عربي / إنكليزي)
# ======================================================================
TEXTS = {
    "ar": {
        "choose_lang": "🌐 <b>اختر لغتك</b>\nChoose your language",
        "welcome": "👋 أهلاً بك في <b>بوت السايت</b>!\n\nأنشئ رابطك الخاص واستقبل رسائل مجهولة من أصدقائك 🎭",
        "menu_title": "🏠 <b>القائمة الرئيسية</b>\nاختر من الأزرار أدناه:",
        "btn_create": "➕ إنشاء رابط جديد",
        "btn_delete": "🗑 حذف الرابط الحالي",
        "btn_mylink": "🔗 رابطي",
        "btn_channel": "📢 تعيين قناة نشر",
        "btn_favs": "⭐ المفضلة",
        "btn_support": "🆘 التواصل مع الدعم",
        "btn_policy": "📜 سياسة الاستخدام",
        "btn_faq": "❓ الأسئلة الشائعة",
        "btn_lang": "🌐 تغيير اللغة",
        "btn_back": "🔙 رجوع",
        "btn_menu": "🏠 القائمة",
        "btn_end_session": "🔚 إنهاء الجلسة",
        "btn_reply": "↩️ الرد",
        "btn_live": "💬 دردشة مباشرة",
        "btn_publish": "📢 نشر بالقناة",
        "btn_fav": "⭐ حفظ بالمفضلة",
        "btn_end_chat": "🔚 إنهاء الدردشة",
        "btn_accept": "✅ قبول",
        "btn_decline": "❌ رفض",
        "btn_check": "✅ تحقق من الاشتراك",
        "btn_join": "📢 اشترك في القناة",
        "btn_cancel": "❌ إلغاء",
        "btn_confirm_del": "✅ نعم، احذف",
        "link_created": "✅ تم إنشاء رابطك الجديد:\n\n<code>{link}</code>\n\nشاركه ليرسل لك الناس رسائل مجهولة.\n⚠️ الرابط القديم (إن وجد) لم يعد يعمل.",
        "link_exists": "🔗 رابطك الحالي:\n\n<code>{link}</code>",
        "no_link": "ℹ️ ليس لديك رابط بعد. أنشئ واحداً من القائمة.",
        "confirm_del": "⚠️ هل تريد حذف رابطك الحالي؟ لن يعمل بعد الآن.",
        "link_deleted": "🗑 تم حذف رابطك.",
        "link_invalid": "❌ هذا الرابط غير صالح أو تم حذفه.",
        "self_link": "😅 لا يمكنك مراسلة نفسك.",
        "session_started": "✍️ <b>أنت الآن تراسل صاحب الرابط بشكل مجهول.</b>\n\nأرسل رسالتك (نص، صورة، فيديو، فيديو دائري، بصمة صوت، ملف أو ملصق).\n⏳ تنتهي الجلسة تلقائياً بعد 6 ساعات من الخمول.",
        "sent_ok": "✅ تم الإرسال.",
        "session_ended": "🔚 تم إنهاء الجلسة.",
        "session_expired": "⌛ انتهت الجلسة تلقائياً بسبب الخمول (6 ساعات).",
        "hdr_anon": "📩 <b>رسالة مجهولة جديدة</b>",
        "hdr_reply": "💬 <b>رد من صاحب الرابط</b>",
        "reply_started": "↩️ أنت الآن ترد على المجهول. أرسل رسالتك.\n⏳ تنتهي الجلسة بعد 6 ساعات من الخمول.",
        "reply_back_started": "↩️ أنت الآن ترد على صاحب الرابط بشكل مجهول. أرسل رسالتك.",
        "live_request": "💬 صاحب الرابط يطلب فتح <b>دردشة مباشرة</b> معك. هل تقبل؟",
        "live_request_sent": "📨 تم إرسال طلب الدردشة المباشرة.",
        "live_started": "💬 <b>بدأت الدردشة المباشرة.</b>\nأي رسالة ترسلها ستصل للطرف الآخر مباشرة.\nللإنهاء اضغط الزر أو أرسل /end",
        "live_ended": "🔚 انتهت الدردشة المباشرة.",
        "live_declined": "❌ رفض الطرف الآخر طلب الدردشة المباشرة.",
        "live_busy": "⚠️ الطرف الآخر في دردشة مباشرة أخرى حالياً.",
        "live_gone": "⚠️ انتهى هذا الطلب أو لم يعد صالحاً.",
        "set_channel_prompt": "📢 أرسل معرف قناتك (مثال: <code>@mychannel</code>)\n\n⚠️ يجب أن تكون <b>أنت</b> مشرفاً في القناة وأن يكون <b>البوت</b> مشرفاً بصلاحية النشر.",
        "channel_set": "✅ تم ربط القناة {ch} بنجاح.",
        "channel_err": "❌ تعذر التحقق من القناة. تأكد أنها قناة صحيحة وأنك والبوت مشرفان فيها.",
        "publish_no_channel": "⚠️ عيّن قناة نشر أولاً من القائمة الرئيسية.",
        "published": "✅ تم النشر في قناتك.",
        "publish_fail": "❌ فشل النشر. تأكد من صلاحيات البوت في القناة.",
        "fav_saved": "⭐ تم الحفظ في المفضلة.",
        "fav_empty": "⭐ مفضلتك فارغة.",
        "fav_title": "⭐ <b>رسائلك المفضلة</b>\nاضغط على رسالة لعرضها:",
        "support_started": "🆘 <b>الدعم الفني</b>\n\nاكتب رسالتك الآن وسيصلك الرد هنا.\nاضغط الزر لإنهاء المحادثة مع الدعم.",
        "support_sent": "✅ وصلت رسالتك للدعم الفني.",
        "support_hdr": "💬 <b>رد الدعم الفني</b>",
        "support_closed": "🔚 تم إنهاء جلسة الدعم الفني.",
        "banned": "🚫 تم حظرك من استخدام هذا البوت.",
        "maintenance": "🛠 البوت في وضع الصيانة حالياً. يرجى المحاولة لاحقاً.",
        "not_subscribed": "❌ لم تشترك بعد! اشترك ثم اضغط تحقق.",
        "reaction_notice": "{emoji} تفاعل الطرف الآخر مع هذه الرسالة",
        "unsupported": "⚠️ هذا النوع من الرسائل غير مدعوم.",
        "send_failed": "⚠️ تعذر إيصال الرسالة (ربما أوقف الطرف الآخر البوت).",
        "no_session": "ℹ️ لا توجد جلسة نشطة. استخدم الأزرار أدناه.",
        "cancelled": "✅ تم الإلغاء.",
        "not_found_msg": "⚠️ لا توجد بيانات لهذه الرسالة (قديمة جداً).",
        "lang_changed": "✅ تم تغيير اللغة.",
    },
    "en": {
        "choose_lang": "🌐 <b>اختر لغتك</b>\nChoose your language",
        "welcome": "👋 Welcome to <b>Site Bot</b>!\n\nCreate your own link and receive anonymous messages from your friends 🎭",
        "menu_title": "🏠 <b>Main menu</b>\nChoose from the buttons below:",
        "btn_create": "➕ Create new link",
        "btn_delete": "🗑 Delete current link",
        "btn_mylink": "🔗 My link",
        "btn_channel": "📢 Set publish channel",
        "btn_favs": "⭐ Favorites",
        "btn_support": "🆘 Contact support",
        "btn_policy": "📜 Usage policy",
        "btn_faq": "❓ FAQ",
        "btn_lang": "🌐 Change language",
        "btn_back": "🔙 Back",
        "btn_menu": "🏠 Menu",
        "btn_end_session": "🔚 End session",
        "btn_reply": "↩️ Reply",
        "btn_live": "💬 Live chat",
        "btn_publish": "📢 Publish to channel",
        "btn_fav": "⭐ Save to favorites",
        "btn_end_chat": "🔚 End chat",
        "btn_accept": "✅ Accept",
        "btn_decline": "❌ Decline",
        "btn_check": "✅ Check subscription",
        "btn_join": "📢 Join channel",
        "btn_cancel": "❌ Cancel",
        "btn_confirm_del": "✅ Yes, delete",
        "link_created": "✅ Your new link:\n\n<code>{link}</code>\n\nShare it to receive anonymous messages.\n⚠️ Your old link (if any) no longer works.",
        "link_exists": "🔗 Your current link:\n\n<code>{link}</code>",
        "no_link": "ℹ️ You have no link yet. Create one from the menu.",
        "confirm_del": "⚠️ Delete your current link? It will stop working.",
        "link_deleted": "🗑 Your link was deleted.",
        "link_invalid": "❌ This link is invalid or was deleted.",
        "self_link": "😅 You can't message yourself.",
        "session_started": "✍️ <b>You are now messaging the link owner anonymously.</b>\n\nSend your message (text, photo, video, video note, voice, file or sticker).\n⏳ The session ends automatically after 6 hours of inactivity.",
        "sent_ok": "✅ Sent.",
        "session_ended": "🔚 Session ended.",
        "session_expired": "⌛ Session ended automatically due to inactivity (6 hours).",
        "hdr_anon": "📩 <b>New anonymous message</b>",
        "hdr_reply": "💬 <b>Reply from the link owner</b>",
        "reply_started": "↩️ You are now replying to the anonymous sender. Send your message.\n⏳ Session ends after 6 hours of inactivity.",
        "reply_back_started": "↩️ You are now replying to the link owner anonymously. Send your message.",
        "live_request": "💬 The link owner wants to open a <b>live chat</b> with you. Accept?",
        "live_request_sent": "📨 Live chat request sent.",
        "live_started": "💬 <b>Live chat started.</b>\nEvery message you send goes straight to the other side.\nPress the button or send /end to finish.",
        "live_ended": "🔚 Live chat ended.",
        "live_declined": "❌ The other side declined the live chat request.",
        "live_busy": "⚠️ The other side is in another live chat right now.",
        "live_gone": "⚠️ This request has expired or is no longer valid.",
        "set_channel_prompt": "📢 Send your channel username (e.g. <code>@mychannel</code>)\n\n⚠️ <b>You</b> must be an admin of the channel and the <b>bot</b> must be an admin with posting rights.",
        "channel_set": "✅ Channel {ch} linked successfully.",
        "channel_err": "❌ Couldn't verify the channel. Make sure it's valid and both you and the bot are admins.",
        "publish_no_channel": "⚠️ Set a publish channel first from the main menu.",
        "published": "✅ Published to your channel.",
        "publish_fail": "❌ Publishing failed. Check the bot's permissions in the channel.",
        "fav_saved": "⭐ Saved to favorites.",
        "fav_empty": "⭐ Your favorites are empty.",
        "fav_title": "⭐ <b>Your favorite messages</b>\nTap one to view it:",
        "support_started": "🆘 <b>Support</b>\n\nWrite your message now and the reply will arrive here.\nPress the button to end the support chat.",
        "support_sent": "✅ Your message reached support.",
        "support_hdr": "💬 <b>Support reply</b>",
        "support_closed": "🔚 Support session ended.",
        "banned": "🚫 You have been banned from using this bot.",
        "maintenance": "🛠 The bot is under maintenance. Please try again later.",
        "not_subscribed": "❌ You haven't subscribed yet! Join, then press Check.",
        "reaction_notice": "{emoji} The other side reacted to this message",
        "unsupported": "⚠️ This message type is not supported.",
        "send_failed": "⚠️ Couldn't deliver the message (the other side may have stopped the bot).",
        "no_session": "ℹ️ No active session. Use the buttons below.",
        "cancelled": "✅ Cancelled.",
        "not_found_msg": "⚠️ No data for this message (too old).",
        "lang_changed": "✅ Language changed.",
    },
}


def lang_of(uid):
    rec = users.data.get(str(uid)) or {}
    return rec.get("lang") or "ar"


def tr(uid, key, **kw):
    """ترجمة نص حسب لغة المستخدم (الافتراضي عربي)."""
    text = TEXTS[lang_of(uid)].get(key) or TEXTS["ar"].get(key, key)
    return text.format(**kw) if kw else text


# ======================================================================
# 4) دوال مساعدة عامة: حقوق البوت + الإرسال + التعديل
# ======================================================================
def credits_html():
    """حقوق البوت كرابط HTML (تُقرأ من config.json وتُعدَّل من لوحة الأدمن)."""
    c = config.data.get("credits") or {}
    text = html.escape(c.get("text") or "")
    url = (c.get("url") or "").replace("'", "%27")
    return f"<a href='{url}'>{text}</a>" if text and url else ""


def with_credits(text):
    """⭐ الدالة المساعدة: تُلحق حقوق البوت بنهاية أي رسالة صادرة تتطلب ذلك."""
    c = credits_html()
    return f"{text}\n\n{c}" if c else text


def send(chat_id, text, markup=None, credits=True, **kw):
    """إرسال رسالة HTML آمنة (لا ترفع استثناءً). تعيد الرسالة أو None."""
    raw = text
    for attempt in range(2):
        body = with_credits(raw) if credits else raw
        try:
            return bot.send_message(
                chat_id,
                body,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
                **kw,
            )
        except ApiTelegramException as e:
            if attempt == 0 and "parse entities" in str(e):
                raw = html.escape(
                    raw
                )  # نص أدخله الأدمن وفيه HTML خاطئ → نعرضه كنص عادي
                continue
            log.warning("send failed to %s: %s", chat_id, e)
            return None


def edit(call, text, markup=None, credits=True):
    """تعديل رسالة الـ callback، وإن تعذّر نرسل رسالة جديدة."""
    raw = text
    chat_id, mid = call.message.chat.id, call.message.message_id
    for attempt in range(2):
        body = with_credits(raw) if credits else raw
        try:
            bot.edit_message_text(
                body, chat_id, mid, reply_markup=markup, link_preview_options=NO_PREVIEW
            )
            return
        except ApiTelegramException as e:
            if "message is not modified" in str(e):
                return
            if attempt == 0 and "parse entities" in str(e):
                raw = html.escape(raw)
                continue
            break
    send(chat_id, text, markup, credits=credits)


def ans(call, text=None, alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=alert)
    except ApiTelegramException:
        pass


def ibtn(text, cb=None, url=None):
    return types.InlineKeyboardButton(text, callback_data=cb, url=url)


def B(uid, key, cb=None, url=None):
    """زر inline بنص مترجم."""
    return ibtn(tr(uid, key), cb, url)


def reply_to(msg):
    return types.ReplyParameters(
        message_id=msg.message_id, allow_sending_without_reply=True
    )


def esc(s):
    return html.escape(str(s or ""))


def user_info(u):
    uname = f"@{u.username}" if u.username else "—"
    return f"👤 {esc(u.full_name)}\n🆔 <code>{u.id}</code>\n🔖 {esc(uname)}"


def notify_admin(text, markup=None):
    send(ADMIN_ID, text, markup, credits=False)


# ======================================================================
# 5) المحتوى: استخراج وإرسال كل أنواع الوسائط
# ======================================================================
MEDIA_SENDERS = {
    "photo": "send_photo",
    "video": "send_video",
    "voice": "send_voice",
    "document": "send_document",
    "audio": "send_audio",
    "animation": "send_animation",
}
NO_CAPTION_SENDERS = {"sticker": "send_sticker", "video_note": "send_video_note"}
CONTENT_TYPES = [
    "text",
    "photo",
    "video",
    "video_note",
    "voice",
    "document",
    "sticker",
    "audio",
    "animation",
]
CT_ICON = {
    "text": "💬",
    "photo": "🖼",
    "video": "🎬",
    "video_note": "⭕️",
    "voice": "🎤",
    "document": "📎",
    "sticker": "🎭",
    "audio": "🎵",
    "animation": "🎞",
}


def extract_content(msg):
    """يحوّل الرسالة إلى قاموس بسيط {ctype, text, file_id} قابل للتخزين وإعادة الإرسال."""
    ct = msg.content_type
    if ct == "text":
        return {"ctype": "text", "text": msg.html_text, "file_id": None}
    getters = {
        "photo": lambda: msg.photo[-1].file_id,  # أكبر دقة
        "video": lambda: msg.video.file_id,
        "video_note": lambda: msg.video_note.file_id,
        "voice": lambda: msg.voice.file_id,
        "document": lambda: msg.document.file_id,
        "sticker": lambda: msg.sticker.file_id,
        "audio": lambda: msg.audio.file_id,
        "animation": lambda: msg.animation.file_id,
    }
    if ct not in getters:
        return None
    return {"ctype": ct, "text": msg.html_caption or "", "file_id": getters[ct]()}


def _join(*parts):
    return "\n\n".join(p for p in parts if p)


def send_content(chat_id, c, header=None, footer=None, markup=None):
    """
    يرسل المحتوى (أي نوع) مع ترويسة وتذييل اختياريين.
    - الملصق والفيديو الدائري لا يدعمان caption، فنرسل الترويسة/التذييل كرسائل منفصلة.
    - إذا تجاوز الـ caption حد 1024 حرفاً نرسل الوسائط ثم النص في رسالة مستقلة تحمل الأزرار.
    ترفع ApiTelegramException عند الفشل.
    """
    ct, body = c["ctype"], c.get("text") or ""
    if ct == "text":
        return bot.send_message(
            chat_id,
            _join(header, body, footer),
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )
    if ct in NO_CAPTION_SENDERS:
        if header:
            bot.send_message(chat_id, header, link_preview_options=NO_PREVIEW)
        m = getattr(bot, NO_CAPTION_SENDERS[ct])(
            chat_id, c["file_id"], reply_markup=markup
        )
        if footer:
            bot.send_message(chat_id, footer, link_preview_options=NO_PREVIEW)
        return m
    sender = getattr(bot, MEDIA_SENDERS[ct])
    caption = _join(header, body, footer)
    if len(caption) <= 1024:
        return sender(
            chat_id, c["file_id"], caption=caption or None, reply_markup=markup
        )
    sender(chat_id, c["file_id"])
    return bot.send_message(
        chat_id, caption, reply_markup=markup, link_preview_options=NO_PREVIEW
    )


# ======================================================================
# 6) المستخدمون والروابط
# ======================================================================
def get_user(uid):
    return users.data.get(str(uid))


def register_user(tg_user):
    """ينشئ سجل المستخدم عند أول دخول. يعيد (السجل، هل هو جديد؟)."""
    key = str(tg_user.id)
    with users.lock:
        rec = users.data.get(key)
        is_new = rec is None
        if is_new:
            rec = {
                "lang": None,
                "banned": False,
                "publish_channel": None,
                "favorites": [],
                "joined": int(time.time()),
            }
            users.data[key] = rec
        rec["name"] = tg_user.full_name
        rec["username"] = tg_user.username
        users.save()
    return rec, is_new


def user_link_code(uid):
    for code, l in links.data.items():
        if l["owner"] == uid:
            return code
    return None


def link_url(code):
    return f"https://t.me/{BOT_USERNAME}?start={code}"


def new_code():
    alphabet = string.ascii_letters + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(10))
        if code not in links.data:
            return code


# ======================================================================
# 7) الجلسات + APScheduler (انتهاء تلقائي بعد 6 ساعات خمول)
# ======================================================================
EXPIRING = ("anon", "reply", "live", "support")


def schedule_expiry(uid, run_at=None):
    run_at = run_at or (datetime.now() + timedelta(hours=SESSION_TTL_HOURS))
    scheduler.add_job(
        expire_session,
        "date",
        run_date=run_at,
        args=[uid],
        id=f"exp_{uid}",
        replace_existing=True,
    )


def cancel_expiry(uid):
    try:
        scheduler.remove_job(f"exp_{uid}")
    except JobLookupError:
        pass


def get_session(uid):
    return sessions.data["sessions"].get(str(uid))


def set_session(uid, **fields):
    """ينشئ/يستبدل جلسة المستخدم (جلسة واحدة فقط لكل مستخدم)."""
    old = get_session(uid)
    # إذا كان في دردشة مباشرة وبدأ شيئاً آخر → ننهي الدردشة للطرفين أولاً
    if old and old.get("type") == "live" and fields.get("type") != "live":
        end_session(uid, "replaced")
    with sessions.lock:
        fields["last"] = time.time()
        sessions.data["sessions"][str(uid)] = fields
        sessions.save(now=False)
    if fields["type"] in EXPIRING:
        schedule_expiry(uid)
    else:
        cancel_expiry(uid)


def touch_session(uid):
    """تحديث وقت آخر نشاط وإعادة جدولة مهمة الانتهاء."""
    s = get_session(uid)
    if s and s["type"] in EXPIRING:
        s["last"] = time.time()
        sessions.save(now=False)
        schedule_expiry(uid)


def end_session(uid, reason="manual"):
    """ينهي جلسة المستخدم ويعالج الأطراف المرتبطة (دردشة مباشرة / دعم)."""
    with sessions.lock:
        sess = sessions.data["sessions"].pop(str(uid), None)
        sessions.save(now=False)
    cancel_expiry(uid)
    if not sess:
        return None
    typ = sess["type"]
    if typ == "live":
        peer = sess.get("peer")
        ps = get_session(peer)
        if ps and ps.get("type") == "live" and ps.get("peer") == uid:
            with sessions.lock:
                sessions.data["sessions"].pop(str(peer), None)
            cancel_expiry(peer)
            send(peer, tr(peer, "live_ended"), menu_btn(peer), credits=False)
    elif typ == "support" and reason != "admin":
        notify_admin(f"ℹ️ انتهت جلسة الدعم مع المستخدم <code>{uid}</code>.")
    return sess


def expire_session(uid):
    """تُنفَّذ بواسطة APScheduler عند انتهاء مدة الخمول."""
    s = get_session(uid)
    if not s or s["type"] not in EXPIRING:
        return
    if time.time() - s.get("last", 0) < SESSION_TTL_HOURS * 3600 - 5:
        schedule_expiry(uid)  # حدث نشاط بعد الجدولة → أعد الجدولة
        return
    end_session(uid, "timeout")
    send(uid, tr(uid, "session_expired"), menu_btn(uid), credits=False)


def restore_jobs():
    """بعد إعادة تشغيل البوت: إعادة جدولة انتهاء الجلسات المحفوظة."""
    for key, s in list(sessions.data["sessions"].items()):
        if s.get("type") in EXPIRING:
            remaining = s.get("last", 0) + SESSION_TTL_HOURS * 3600 - time.time()
            schedule_expiry(
                int(key), datetime.now() + timedelta(seconds=max(remaining, 5))
            )


# ---------- خريطة الرسائل (للأزرار والتفاعلات) ----------
def remember(chat_id, msg_id, **info):
    """
    نحفظ لكل رسالة: الطرف الآخر ورقم رسالته عنده + النوع (+ المحتوى للرسائل المستلمة).
    بهذا لا نضع أي معرّف مستخدم داخل callback_data، ونستطيع نقل التفاعلات للطرف الآخر.
    """
    with sessions.lock:
        mm = sessions.data["msg_map"]
        mm[f"{chat_id}:{msg_id}"] = info
        if len(mm) > MSG_MAP_LIMIT:  # تقليم الأقدم
            for k in list(mm.keys())[: MSG_MAP_LIMIT // 5]:
                mm.pop(k, None)
        sessions.save(now=False)


def recall(chat_id, msg_id):
    return sessions.data["msg_map"].get(f"{chat_id}:{msg_id}")


def deliver(src, dst, msg, content, kind, header=None, markup=None):
    """يوصل رسالة من src إلى dst ويسجّل الخرائط للطرفين. يعيد True عند النجاح."""
    try:
        sent = send_content(dst, content, header=header, markup=markup)
    except ApiTelegramException as e:
        log.warning("deliver failed %s→%s: %s", src, dst, e)
        send(src, tr(src, "send_failed"), credits=False)
        return False
    remember(
        dst,
        sent.message_id,
        peer=src,
        peer_msg=msg.message_id,
        kind=kind,
        content=content,
    )
    remember(src, msg.message_id, peer=dst, peer_msg=sent.message_id, kind="out")
    return True


# ======================================================================
# 8) الاشتراك الإجباري + الحارس (حظر / صيانة / اشتراك)
# ======================================================================
_sub_cache = {}


def is_subscribed(uid):
    if uid == ADMIN_ID:
        return True
    channels = config.data.get("force_channels") or []
    if not channels:
        return True
    if time.time() - _sub_cache.get(uid, 0) < 30:  # كاش قصير لتخفيف الطلبات
        return True
    for ch in channels:
        try:
            m = bot.get_chat_member(ch, uid)
            ok = m.status in ("member", "administrator", "creator") or (
                m.status == "restricted" and getattr(m, "is_member", False)
            )
            if not ok:
                _sub_cache.pop(uid, None)
                return False
        except ApiTelegramException as e:
            # غالباً البوت ليس مشرفاً في القناة: نتجاوزها كي لا نقفل البوت على الجميع
            log.warning("فحص الاشتراك فشل للقناة %s: %s", ch, e)
    _sub_cache[uid] = time.time()
    return True


def channel_url(ch):
    ch = str(ch)
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    try:
        chat = bot.get_chat(ch)
        return f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    except ApiTelegramException:
        return None


def sub_markup(uid):
    kb = types.InlineKeyboardMarkup()
    for ch in config.data.get("force_channels") or []:
        url = channel_url(ch)
        if url:
            kb.add(B(uid, "btn_join", url=url))
    kb.add(B(uid, "btn_check", "chk"))
    return kb


def guard(uid, chat_id, check_sub=True):
    """يعيد True إذا يُسمح للمستخدم بالمتابعة (الأدمن دائماً مسموح)."""
    if uid == ADMIN_ID:
        return True
    rec = get_user(uid)
    if rec and rec.get("banned"):
        send(chat_id, tr(uid, "banned"), credits=False)
        return False
    if config.data.get("maintenance"):
        send(chat_id, tr(uid, "maintenance"))
        return False
    if check_sub and not is_subscribed(uid):
        msg = (config.data.get("force_message") or {}).get(lang_of(uid)) or TEXTS["ar"][
            "not_subscribed"
        ]
        send(chat_id, msg, sub_markup(uid))
        return False
    return True


# ======================================================================
# 9) القوائم ولوحات المستخدم
# ======================================================================
def menu_btn(uid):
    kb = types.InlineKeyboardMarkup()
    kb.add(B(uid, "btn_menu", "menu"))
    return kb


def end_markup(uid, key="btn_end_session"):
    kb = types.InlineKeyboardMarkup()
    kb.add(B(uid, key, "end"))
    return kb


def cancel_markup(uid):
    kb = types.InlineKeyboardMarkup()
    kb.add(B(uid, "btn_cancel", "end"))
    return kb


def back_markup(uid):
    kb = types.InlineKeyboardMarkup()
    kb.add(B(uid, "btn_back", "menu"))
    return kb


def main_menu_markup(uid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(B(uid, "btn_create", "new"), B(uid, "btn_delete", "del"))
    kb.add(B(uid, "btn_mylink", "my"), B(uid, "btn_channel", "chan"))
    kb.add(B(uid, "btn_favs", "favs"), B(uid, "btn_support", "sup"))
    kb.add(B(uid, "btn_policy", "pol"), B(uid, "btn_faq", "faq"))
    kb.add(B(uid, "btn_lang", "chlang"))
    return kb


def lang_markup():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(ibtn("🇸🇦 العربية", "lang:ar"), ibtn("🇬🇧 English", "lang:en"))
    return kb


def show_menu(uid, chat_id):
    send(chat_id, tr(uid, "menu_title"), main_menu_markup(uid))


def owner_actions_markup(uid):
    """الأزرار التي تظهر للمالك أسفل كل رسالة مجهولة."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(B(uid, "btn_reply", "rep"), B(uid, "btn_live", "live"))
    kb.add(B(uid, "btn_publish", "pub"), B(uid, "btn_fav", "fav"))
    return kb


def anon_actions_markup(uid):
    kb = types.InlineKeyboardMarkup()
    kb.add(B(uid, "btn_reply", "rep"))
    return kb


# ======================================================================
# 10) مسار /start والاشتراك واللغة
# ======================================================================
def proceed_after_access(uid, chat_id, payload):
    """بعد اجتياز الحظر/الصيانة/الاشتراك: اللغة ثم الرابط أو القائمة."""
    rec = get_user(uid)
    if not rec.get("lang"):
        rec["pending_start"] = payload
        users.save()
        send(chat_id, TEXTS["ar"]["choose_lang"], lang_markup(), credits=False)
        return
    if payload:
        open_link(uid, chat_id, payload)
    else:
        show_menu(uid, chat_id)


def start_flow(tg_user, chat_id, payload=None):
    uid = tg_user.id
    rec, is_new = register_user(tg_user)
    if is_new:
        notify_admin(
            f"🆕 <b>مستخدم جديد دخل البوت</b>\n\n{user_info(tg_user)}\n\n📊 الإجمالي: {len(users.data)}"
        )
    if not guard(uid, chat_id):
        if payload:  # نحفظ رابط الدخول لنكمله بعد الاشتراك
            rec["pending_start"] = payload
            users.save()
        return
    proceed_after_access(uid, chat_id, payload)


@bot.message_handler(commands=["start"])
def cmd_start(msg):
    if msg.chat.type != "private":
        return
    parts = (msg.text or "").split(maxsplit=1)
    start_flow(msg.from_user, msg.chat.id, parts[1].strip() if len(parts) > 1 else None)


@bot.message_handler(commands=["menu"])
def cmd_menu(msg):
    if msg.chat.type != "private":
        return
    if not get_user(msg.from_user.id):
        return start_flow(msg.from_user, msg.chat.id)
    if guard(msg.from_user.id, msg.chat.id):
        show_menu(msg.from_user.id, msg.chat.id)


def do_end(uid, chat_id, call=None):
    """إنهاء/إلغاء أي جلسة أو حالة (يُستخدم من /end و /cancel وزر الإنهاء)."""
    sess = end_session(uid, "manual")
    if not sess:
        text, markup = tr(uid, "no_session"), main_menu_markup(uid)
    else:
        typ = sess["type"]
        key = {"live": "live_ended", "support": "support_closed"}.get(typ)
        if not key:
            key = (
                "cancelled"
                if typ
                in ("set_channel", "admin_input", "admin_broadcast", "admin_support")
                else "session_ended"
            )
        text, markup = tr(uid, key), menu_btn(uid)
    if call:
        edit(call, text, markup, credits=False)
    else:
        send(chat_id, text, markup, credits=False)


@bot.message_handler(commands=["end", "cancel"])
def cmd_end(msg):
    if msg.chat.type == "private" and get_user(msg.from_user.id):
        do_end(msg.from_user.id, msg.chat.id)


def open_link(uid, chat_id, code):
    """المستخدم دخل برابط شخص آخر → ننشئ جلسة إرسال مجهول."""
    link = links.data.get(code)
    owner_rec = get_user(link["owner"]) if link else None
    if not link or (owner_rec and owner_rec.get("banned")):
        send(chat_id, tr(uid, "link_invalid"), menu_btn(uid))
        return
    if link["owner"] == uid:
        send(chat_id, tr(uid, "self_link"), menu_btn(uid))
        return
    set_session(uid, type="anon", target=link["owner"])
    send(chat_id, tr(uid, "session_started"), end_markup(uid))


@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if msg.chat.type == "private" and msg.from_user.id == ADMIN_ID:
        send(msg.chat.id, "🛡 <b>لوحة الإدارة</b>", admin_markup(), credits=False)


# ======================================================================
# 11) معالج الرسائل العام (يوجّه حسب حالة الجلسة)
# ======================================================================
@bot.message_handler(content_types=CONTENT_TYPES)
def on_message(msg):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    if not get_user(uid):  # لم يضغط /start من قبل
        return start_flow(msg.from_user, msg.chat.id)
    if not guard(uid, msg.chat.id):
        return
    if not lang_of_set(uid):
        return send(
            msg.chat.id, TEXTS["ar"]["choose_lang"], lang_markup(), credits=False
        )

    sess = get_session(uid)
    if not sess:
        return show_menu(uid, msg.chat.id)

    typ = sess["type"]
    if uid == ADMIN_ID and typ in ("admin_input", "admin_broadcast", "admin_support"):
        return handle_admin_state(msg, sess)
    if typ == "set_channel":
        return handle_set_channel(msg)
    if typ in ("anon", "reply"):
        return relay_one_way(msg, sess)
    if typ == "live":
        return relay_live(msg, sess)
    if typ == "support":
        return relay_support(msg)
    show_menu(uid, msg.chat.id)


def lang_of_set(uid):
    return bool((get_user(uid) or {}).get("lang"))


def relay_one_way(msg, sess):
    """إرسال رسالة المجهول إلى المالك (type=anon) أو رد المالك على المجهول (type=reply)."""
    uid, target = msg.from_user.id, sess["target"]
    content = extract_content(msg)
    if not content:
        return send(msg.chat.id, tr(uid, "unsupported"), credits=False)
    if sess["type"] == "anon":
        ok = deliver(
            uid,
            target,
            msg,
            content,
            "anon",
            header=tr(target, "hdr_anon"),
            markup=owner_actions_markup(target),
        )
    else:
        ok = deliver(
            uid,
            target,
            msg,
            content,
            "reply",
            header=tr(target, "hdr_reply"),
            markup=anon_actions_markup(target),
        )
    if ok:
        touch_session(uid)
        send(
            msg.chat.id,
            tr(uid, "sent_ok"),
            end_markup(uid),
            credits=False,
            reply_parameters=reply_to(msg),
        )


def relay_live(msg, sess):
    uid, peer = msg.from_user.id, sess["peer"]
    content = extract_content(msg)
    if not content:
        return send(msg.chat.id, tr(uid, "unsupported"), credits=False)
    if deliver(uid, peer, msg, content, "live"):
        touch_session(uid)
        touch_session(peer)


def relay_support(msg):
    """رسالة دعم فني من المستخدم → الأدمن (مع أزرار الرد والإنهاء)."""
    uid = msg.from_user.id
    content = extract_content(msg)
    if not content:
        return send(msg.chat.id, tr(uid, "unsupported"), credits=False)
    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("✍️ رد", f"supr:{uid}"), ibtn("🔚 إنهاء الدعم", f"supe:{uid}"))
    header = f"🆘 <b>رسالة دعم فني</b>\n{user_info(msg.from_user)}"
    try:
        send_content(ADMIN_ID, content, header=header, markup=kb)
    except ApiTelegramException:
        return send(uid, tr(uid, "send_failed"), credits=False)
    touch_session(uid)
    send(
        uid,
        tr(uid, "support_sent"),
        end_markup(uid),
        credits=False,
        reply_parameters=reply_to(msg),
    )


# ======================================================================
# 12) التفاعلات الأصلية (Native Reactions)
# ======================================================================
def _emojis(reactions):
    out = []
    for r in reactions or []:
        out.append(getattr(r, "emoji", None) or "✨")  # إيموجي مخصص/مدفوع → رمز عام
    return out


@bot.message_reaction_handler(func=lambda r: True)
def on_reaction(r):
    if not r.user or r.chat.type != "private":
        return
    uid = r.user.id
    rec = get_user(uid)
    if (rec and rec.get("banned")) or (
        config.data.get("maintenance") and uid != ADMIN_ID
    ):
        return
    entry = recall(uid, r.message_id)
    if not entry or "peer" not in entry:
        return
    old = _emojis(r.old_reaction)
    added = [e for e in _emojis(r.new_reaction) if e not in old]
    if not added:  # إزالة تفاعل: لا نرسل إشعاراً
        return
    peer = entry["peer"]
    send(
        peer,
        tr(peer, "reaction_notice", emoji=" ".join(added)),
        credits=False,
        reply_parameters=types.ReplyParameters(
            message_id=entry["peer_msg"], allow_sending_without_reply=True
        ),
    )


# ======================================================================
# 13) الـ Callbacks (المستخدم)
# ======================================================================
def cb_check_sub(call, arg):
    uid = call.from_user.id
    if not is_subscribed(uid):
        return ans(call, tr(uid, "not_subscribed"), alert=True)
    ans(call)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except ApiTelegramException:
        pass
    rec = get_user(uid)
    payload = rec.pop("pending_start", None)
    users.save()
    proceed_after_access(uid, call.message.chat.id, payload)


def cb_lang(call, arg):
    uid = call.from_user.id
    if arg not in TEXTS:
        return ans(call)
    rec = get_user(uid)
    rec["lang"] = arg
    payload = rec.pop("pending_start", None)
    users.save()
    ans(call, tr(uid, "lang_changed"))
    edit(call, tr(uid, "welcome"))
    if payload:
        open_link(uid, call.message.chat.id, payload)
    else:
        show_menu(uid, call.message.chat.id)


def cb_choose_lang(call, arg):
    ans(call)
    edit(call, TEXTS["ar"]["choose_lang"], lang_markup(), credits=False)


def cb_menu(call, arg):
    uid = call.from_user.id
    ans(call)
    s = get_session(uid)
    if s and s["type"] == "set_channel":
        end_session(uid)
    edit(call, tr(uid, "menu_title"), main_menu_markup(uid))


def cb_new(call, arg):
    uid = call.from_user.id
    with links.lock:
        for code, l in list(links.data.items()):  # حذف الرابط القديم
            if l["owner"] == uid:
                del links.data[code]
        code = new_code()
        links.data[code] = {"owner": uid, "created": int(time.time())}
        links.save()
    ans(call)
    edit(call, tr(uid, "link_created", link=link_url(code)), back_markup(uid))
    notify_admin(
        f"🔗 <b>تم إنشاء رابط جديد</b>\n\n{user_info(call.from_user)}\n🔑 <code>{code}</code>\n📊 إجمالي الروابط: {len(links.data)}"
    )


def cb_mylink(call, arg):
    uid = call.from_user.id
    code = user_link_code(uid)
    ans(call)
    edit(
        call,
        tr(uid, "link_exists", link=link_url(code)) if code else tr(uid, "no_link"),
        back_markup(uid),
    )


def cb_delete(call, arg):
    uid = call.from_user.id
    ans(call)
    if not user_link_code(uid):
        return edit(call, tr(uid, "no_link"), back_markup(uid))
    kb = types.InlineKeyboardMarkup()
    kb.add(B(uid, "btn_confirm_del", "delok"), B(uid, "btn_back", "menu"))
    edit(call, tr(uid, "confirm_del"), kb)


def cb_delete_ok(call, arg):
    uid = call.from_user.id
    with links.lock:
        for code, l in list(links.data.items()):
            if l["owner"] == uid:
                del links.data[code]
        links.save()
    ans(call)
    edit(call, tr(uid, "link_deleted"), back_markup(uid))


def cb_policy(call, arg):
    uid = call.from_user.id
    ans(call)
    edit(
        call,
        (config.data.get("policy") or {}).get(lang_of(uid)) or "—",
        back_markup(uid),
    )


def cb_faq(call, arg):
    uid = call.from_user.id
    ans(call)
    edit(
        call, (config.data.get("faq") or {}).get(lang_of(uid)) or "—", back_markup(uid)
    )


# ---------- قناة النشر ----------
def cb_channel(call, arg):
    uid = call.from_user.id
    ans(call)
    set_session(uid, type="set_channel")
    edit(call, tr(uid, "set_channel_prompt"), cancel_markup(uid))


def handle_set_channel(msg):
    uid = msg.from_user.id
    ch = (msg.text or "").strip() if msg.content_type == "text" else ""
    if not re.fullmatch(r"@[A-Za-z0-9_]{4,}", ch):
        return send(
            msg.chat.id, tr(uid, "channel_err"), cancel_markup(uid), credits=False
        )
    try:
        chat = bot.get_chat(ch)
        if chat.type != "channel":
            raise ValueError("not a channel")
        member = bot.get_chat_member(ch, uid)
        if member.status not in ("creator", "administrator"):
            raise ValueError("user is not admin")
        bm = bot.get_chat_member(ch, BOT_ID)
        if (
            bm.status != "administrator"
            or getattr(bm, "can_post_messages", None) is False
        ):
            raise ValueError("bot can't post")
    except (ApiTelegramException, ValueError) as e:
        log.info("channel check failed: %s", e)
        return send(
            msg.chat.id, tr(uid, "channel_err"), cancel_markup(uid), credits=False
        )
    get_user(uid)["publish_channel"] = ch
    users.save()
    end_session(uid)
    send(msg.chat.id, tr(uid, "channel_set", ch=esc(ch)), menu_btn(uid))


# ---------- المفضلة ----------
def _fav_label(item):
    plain = (
        html.unescape(re.sub(r"<[^>]+>", "", item.get("text") or ""))
        .strip()
        .replace("\n", " ")
    )
    label = plain[:28] + ("…" if len(plain) > 28 else "")
    return f"{CT_ICON.get(item['ctype'], '📄')} {label}".strip()


def render_favs(uid):
    favs = (get_user(uid) or {}).get("favorites", [])
    if not favs:
        return tr(uid, "fav_empty"), back_markup(uid)
    kb = types.InlineKeyboardMarkup()
    for item in favs[-20:][::-1]:
        kb.add(
            ibtn(_fav_label(item), f"fshow:{item['id']}"),
            ibtn("🗑", f"fdel:{item['id']}"),
        )
    kb.add(B(uid, "btn_back", "menu"))
    return tr(uid, "fav_title"), kb


def cb_favs(call, arg):
    uid = call.from_user.id
    ans(call)
    text, kb = render_favs(uid)
    edit(call, text, kb)


def cb_fav_show(call, arg):
    uid = call.from_user.id
    item = next((f for f in get_user(uid).get("favorites", []) if f["id"] == arg), None)
    if not item:
        return ans(call, tr(uid, "not_found_msg"), alert=True)
    ans(call)
    try:
        send_content(call.message.chat.id, item)
    except ApiTelegramException:
        send(call.message.chat.id, tr(uid, "send_failed"), credits=False)


def cb_fav_del(call, arg):
    uid = call.from_user.id
    rec = get_user(uid)
    rec["favorites"] = [f for f in rec.get("favorites", []) if f["id"] != arg]
    users.save()
    ans(call)
    text, kb = render_favs(uid)
    edit(call, text, kb)


def cb_fav_save(call, arg):
    """زر «حفظ بالمفضلة» أسفل الرسالة المستلمة."""
    uid = call.from_user.id
    e = recall(call.message.chat.id, call.message.message_id)
    if not e or "content" not in e:
        return ans(call, tr(uid, "not_found_msg"), alert=True)
    c = e["content"]
    rec = get_user(uid)
    favs = rec.setdefault("favorites", [])
    favs.append(
        {
            "id": secrets.token_hex(3),
            "ctype": c["ctype"],
            "text": c.get("text"),
            "file_id": c.get("file_id"),
            "date": int(time.time()),
        }
    )
    if len(favs) > FAVORITES_LIMIT:
        favs.pop(0)
    users.save()
    ans(call, tr(uid, "fav_saved"))


# ---------- النشر بالقناة ----------
def publish_to_channel(owner_id, channel, content):
    """ينشر الرسالة في قناة المالك مع توقيع البوت بتنسيق HTML في النهاية."""
    code = user_link_code(owner_id)
    target = link_url(code) if code else f"https://t.me/{BOT_USERNAME}"
    signature = f"<a href='{target}'>{SIGNATURE_TEXT}</a>"
    send_content(
        channel,
        content,
        header="💌 <b>رسالة مجهولة< من بوت السايت: /b>",
        footer=signature,
    )


def cb_publish(call, arg):
    uid = call.from_user.id
    e = recall(call.message.chat.id, call.message.message_id)
    if not e or e.get("kind") != "anon":
        return ans(call, tr(uid, "not_found_msg"), alert=True)
    ch = get_user(uid).get("publish_channel")
    if not ch:
        return ans(call, tr(uid, "publish_no_channel"), alert=True)
    try:
        publish_to_channel(uid, ch, e["content"])
        ans(call, tr(uid, "published"), alert=True)
    except ApiTelegramException as ex:
        log.warning("publish failed: %s", ex)
        ans(call, tr(uid, "publish_fail"), alert=True)


# ---------- الرد والدردشة المباشرة ----------
def cb_reply(call, arg):
    uid = call.from_user.id
    e = recall(call.message.chat.id, call.message.message_id)
    if not e or e.get("kind") not in ("anon", "reply"):
        return ans(call, tr(uid, "not_found_msg"), alert=True)
    peer = e["peer"]
    if e["kind"] == "anon":  # المالك يرد على المجهول
        set_session(uid, type="reply", target=peer)
        text = tr(uid, "reply_started")
    else:  # المجهول يرد على المالك
        set_session(uid, type="anon", target=peer)
        text = tr(uid, "reply_back_started")
    ans(call)
    send(call.message.chat.id, text, end_markup(uid), credits=False)


def drop_pending(token):
    with sessions.lock:
        sessions.data["pending"].pop(token, None)
        sessions.save(now=False)


def cb_live_request(call, arg):
    """المالك يطلب دردشة مباشرة مع المجهول."""
    uid = call.from_user.id
    e = recall(call.message.chat.id, call.message.message_id)
    if not e or e.get("kind") != "anon":
        return ans(call, tr(uid, "not_found_msg"), alert=True)
    anon = e["peer"]
    s = get_session(anon)
    if s and s["type"] == "live":
        return ans(call, tr(uid, "live_busy"), alert=True)
    token = secrets.token_hex(4)
    with sessions.lock:
        sessions.data["pending"][token] = {"owner": uid, "anon": anon}
        sessions.save(now=False)
    scheduler.add_job(
        drop_pending,
        "date",
        run_date=datetime.now() + timedelta(hours=1),
        args=[token],
        id=f"pend_{token}",
        replace_existing=True,
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(
        B(anon, "btn_accept", f"lcy:{token}"), B(anon, "btn_decline", f"lcn:{token}")
    )
    if send(anon, tr(anon, "live_request"), kb, credits=False):
        ans(call, tr(uid, "live_request_sent"), alert=True)
    else:
        ans(call, tr(uid, "send_failed"), alert=True)


def cb_live_accept(call, arg):
    uid = call.from_user.id
    req = sessions.data["pending"].get(arg)
    if not req or req["anon"] != uid:
        return ans(call, tr(uid, "live_gone"), alert=True)
    owner = req["owner"]
    os_ = get_session(owner)
    if os_ and os_["type"] == "live":
        drop_pending(arg)
        return ans(call, tr(uid, "live_busy"), alert=True)
    drop_pending(arg)
    set_session(owner, type="live", peer=uid)
    set_session(uid, type="live", peer=owner)
    ans(call)
    edit(call, tr(uid, "live_started"), end_markup(uid, "btn_end_chat"), credits=False)
    send(
        owner,
        tr(owner, "live_started"),
        end_markup(owner, "btn_end_chat"),
        credits=False,
    )


def cb_live_decline(call, arg):
    uid = call.from_user.id
    req = sessions.data["pending"].get(arg)
    if not req or req["anon"] != uid:
        return ans(call, tr(uid, "live_gone"), alert=True)
    drop_pending(arg)
    ans(call)
    edit(call, tr(uid, "live_declined"), menu_btn(uid), credits=False)
    send(req["owner"], tr(req["owner"], "live_declined"), credits=False)


def cb_end(call, arg):
    ans(call)
    do_end(call.from_user.id, call.message.chat.id, call)


# ---------- الدعم الفني (جهة المستخدم) ----------
def cb_support(call, arg):
    uid = call.from_user.id
    ans(call)
    set_session(uid, type="support")
    edit(call, tr(uid, "support_started"), end_markup(uid))


# ======================================================================
# 14) لوحة الإدارة (للأدمن فقط)
# ======================================================================
SETTINGS_FIELDS = {
    "credits": "🔗 حقوق البوت",
    "force_channels": "📢 قنوات الاشتراك",
    "force_message": "📝 رسالة الاشتراك",
    "policy": "📜 سياسة الاستخدام",
    "faq": "❓ الأسئلة الشائعة",
}


def admin_markup():
    on = config.data.get("maintenance")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(ibtn("📊 الإحصائيات", "adm:stats"), ibtn("📢 إذاعة عامة", "adm:bc"))
    kb.add(ibtn("🚫 حظر مستخدم", "adm:ban"), ibtn("✅ إلغاء حظر", "adm:unban"))
    kb.add(ibtn(f"🛠 الصيانة: {'🟢 مفعّلة' if on else '⚪️ معطّلة'}", "adm:maint"))
    kb.add(ibtn("⚙️ الإعدادات الديناميكية", "adm:settings"))
    kb.add(ibtn("📂 جلب ملف sessions.json", "adm:sessions"))
    kb.add(ibtn("👥 جلب ملف users.json", "adm:users"))
    kb.add(ibtn("❌ إغلاق", "adm:close"))
    return kb


def admin_back():
    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("🔙 لوحة الإدارة", "adm:home"))
    return kb


def admin_cancel():
    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("❌ إلغاء", "end"))
    return kb


def stats_text():
    now_midnight = (
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    total = len(users.data)
    banned = sum(1 for u in users.data.values() if u.get("banned"))
    today = sum(1 for u in users.data.values() if u.get("joined", 0) >= now_midnight)
    by_type = {}
    for s in sessions.data["sessions"].values():
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    active = ", ".join(f"{k}: {v}" for k, v in by_type.items()) or "لا يوجد"
    return (
        f"📊 <b>الإحصائيات</b>\n\n👥 المستخدمون: <b>{total}</b>\n🆕 انضموا اليوم: <b>{today}</b>\n"
        f"🚫 المحظورون: <b>{banned}</b>\n🔗 الروابط: <b>{len(links.data)}</b>\n"
        f"⏳ الجلسات النشطة: {esc(active)}"
    )


def sessions_report_text():
    data = sessions.data

    active_sessions = data.get("sessions", {})
    msg_map = data.get("msg_map", {})
    pending = data.get("pending", {})

    # إحصائيات أنواع الجلسات
    session_types = {}
    for s in active_sessions.values():
        typ = s.get("type", "unknown")
        session_types[typ] = session_types.get(typ, 0) + 1

    # إحصائيات أنواع الرسائل
    message_types = {}
    for m in msg_map.values():
        kind = m.get("kind", "unknown")
        message_types[kind] = message_types.get(kind, 0) + 1

    # أنواع المحتوى
    content_types = {}
    for m in msg_map.values():
        content = m.get("content", {})
        if content:
            ct = content.get("ctype", "unknown")
            content_types[ct] = content_types.get(ct, 0) + 1

    text = (
        "📊 <b>تقرير ملف الجلسات</b>\n\n"
        f"👥 الجلسات النشطة: <b>{len(active_sessions)}</b>\n"
        f"💬 سجلات الرسائل: <b>{len(msg_map)}</b>\n"
        f"⏳ الطلبات المعلقة: <b>{len(pending)}</b>\n\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>أنواع الجلسات:</b>\n"
    )

    for typ, count in session_types.items():
        text += f"• {esc(typ)}: <b>{count}</b>\n"

    text += "\n📨 <b>أنواع سجلات الرسائل:</b>\n"

    for typ, count in message_types.items():
        text += f"• {esc(typ)}: <b>{count}</b>\n"

    text += "\n📎 <b>أنواع المحتوى:</b>\n"

    for typ, count in content_types.items():
        text += f"• {esc(typ)}: <b>{count}</b>\n"

    text += "\n━━━━━━━━━━━━━━\n"
    text += "👤 <b>تفاصيل الجلسات النشطة:</b>\n\n"

    for uid, s in list(active_sessions.items())[:30]:
        last = s.get("last", 0)

        try:
            last_time = datetime.fromtimestamp(float(last)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (ValueError, TypeError, OSError):
            last_time = "غير معروف"

        text += (
            f"🆔 المستخدم: <code>{esc(uid)}</code>\n"
            f"🔖 النوع: <b>{esc(s.get('type', 'unknown'))}</b>\n"
            f"🎯 الهدف: <code>{esc(str(s.get('target', '-')))}</code>\n"
            f"🕒 آخر نشاط: <code>{last_time}</code>\n\n"
        )

    if len(active_sessions) > 30:
        text += f"… وهناك {len(active_sessions) - 30} جلسة أخرى.\n"

    return text

def users_report_text():
    report = (
        "👥 <b>تقرير مستخدمي البوت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 إجمالي المستخدمين: <b>{len(users.data)}</b>\n"
    )

    banned_count = sum(
        1 for u in users.data.values() if u.get("banned")
    )

    report += f"🚫 المحظورون: <b>{banned_count}</b>\n"
    report += f"✅ النشطون: <b>{len(users.data) - banned_count}</b>\n"
    report += "\n━━━━━━━━━━━━━━━━━━\n"

    for uid, user in users.data.items():
        name = esc(str(user.get("name") or "غير معروف"))
        username = user.get("username")
        username_text = (
            f"@{esc(str(username).lstrip('@'))}"
            if username else "لا يوجد"
        )

        lang = "العربية 🇮🇶" if user.get("lang") == "ar" else "English 🇬🇧"
        banned = "🚫 محظور" if user.get("banned") else "✅ نشط"
        channel = user.get("publish_channel") or "لا توجد"
        favorites = len(user.get("favorites", []))

        report += (
            f"\n👤 <b>{name}</b>\n"
            f"🆔 ID: <code>{esc(str(uid))}</code>\n"
            f"🔗 Username: {username_text}\n"
            f"🌐 اللغة: {lang}\n"
            f"📌 الحالة: {banned}\n"
            f"📢 القناة: <code>{esc(str(channel))}</code>\n"
            f"⭐ المفضلة: {favorites}\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

    return report
def cb_admin(call, arg):
    if call.from_user.id != ADMIN_ID:
        return ans(call)
    ans(call)
    if arg == "home":
        end_session(ADMIN_ID, "admin")
        edit(call, "🛡 <b>لوحة الإدارة</b>", admin_markup(), credits=False)
    elif arg == "stats":
        edit(call, stats_text(), admin_back(), credits=False)
    elif arg == "users":
        try:
            users.flush()
    
            # إنشاء التقرير
            report = users_report_text()
    
            # تقسيم التقرير حتى لا يتجاوز حد تيليجرام
            chunks = []
            current = ""
    
            for line in report.split("\n"):
                if len(current) + len(line) + 1 > 3500:
                    chunks.append(current)
                    current = ""
    
                current += line + "\n"
    
            if current:
                chunks.append(current)
    
            # إرسال التقارير
            for chunk in chunks:
                bot.send_message(
                    call.message.chat.id,
                    chunk,
                    parse_mode="HTML"
                )
    
            # إرسال الملف الأصلي
            with open(users.path, "rb") as file:
                bot.send_document(
                    call.message.chat.id,
                    file,
                    caption="👥 <b>قاعدة بيانات المستخدمين</b>\nusers.json",
                    parse_mode="HTML"
                )
    
        except Exception as e:
            log.exception("users export failed")
            send(
                call.message.chat.id,
                f"❌ حدث خطأ أثناء جلب المستخدمين:\n"
                f"<code>{esc(str(e))}</code>",
                credits=False
            )        
    elif arg == "sessions":
        try:
            # حفظ آخر التغييرات قبل إرسال الملف
            sessions.flush()

            # إرسال التقرير
            report = sessions_report_text()
            send(call.message.chat.id, report, credits=False)

            # إرسال الملف الأصلي
            with open(sessions.path, "rb") as file:
                bot.send_document(
                    call.message.chat.id,
                    file,
                    caption="📂 <b>ملف قاعدة بيانات الجلسات</b>\n"
                            "sessions.json",
                    parse_mode="HTML"
                )

        except Exception as e:
            log.exception("sessions export failed")
            send(
                call.message.chat.id,
                f"❌ حدث خطأ أثناء جلب الملف:\n<code>{esc(str(e))}</code>",
                credits=False
            )
    elif arg in ("ban", "unban"):
        set_session(ADMIN_ID, type="admin_input", action=arg)
        edit(
            call,
            "🚫 أرسل <b>ID</b> المستخدم الذي تريد "
            + ("حظره:" if arg == "ban" else "إلغاء حظره:"),
            admin_cancel(),
            credits=False,
        )
    elif arg == "bc":
        kb = types.InlineKeyboardMarkup()
        kb.add(
            ibtn("📨 رسالة عادية (نسخ)", "adm:bc_copy"),
            ibtn("↪️ توجيه (Forward)", "adm:bc_fwd"),
        )
        kb.add(ibtn("🔙 رجوع", "adm:home"))
        edit(call, "📢 اختر نوع الإذاعة:", kb, credits=False)
    elif arg in ("bc_copy", "bc_fwd"):
        set_session(
            ADMIN_ID,
            type="admin_broadcast",
            mode="copy" if arg == "bc_copy" else "forward",
        )
        edit(
            call,
            "📢 أرسل الآن الرسالة التي تريد إذاعتها لجميع المستخدمين.",
            admin_cancel(),
            credits=False,
        )
    elif arg == "maint":
        config.data["maintenance"] = not config.data.get("maintenance")
        config.save()
        edit(call, "🛡 <b>لوحة الإدارة</b>", admin_markup(), credits=False)
    elif arg == "settings":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(ibtn(SETTINGS_FIELDS["credits"], "admset:credits"))
        kb.add(ibtn(SETTINGS_FIELDS["force_channels"], "admset:force_channels"))
        for f in ("force_message", "policy", "faq"):
            kb.add(
                ibtn(f"{SETTINGS_FIELDS[f]} (AR)", f"admset:{f}:ar"),
                ibtn(f"{SETTINGS_FIELDS[f]} (EN)", f"admset:{f}:en"),
            )
        kb.add(ibtn("🔙 رجوع", "adm:home"))
        edit(
            call,
            "⚙️ <b>الإعدادات الديناميكية</b>\nاختر ما تريد تعديله:",
            kb,
            credits=False,
        )
    elif arg == "close":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except ApiTelegramException:
            pass


def cb_admin_set(call, arg):
    if call.from_user.id != ADMIN_ID:
        return ans(call)
    ans(call)
    field, _, lang = arg.partition(":")
    cur = config.data.get(field)
    if isinstance(cur, dict) and lang in cur:
        cur = cur[lang]
    if isinstance(cur, dict):
        cur = " | ".join(str(v) for v in cur.values())
    if isinstance(cur, list):
        cur = ", ".join(cur)
    hints = {
        "credits": "الصيغة: <code>النص | الرابط</code>\nمثال: <code>بوت السايت | https://t.me/alii_index</code>\nأرسل <code>-</code> لتعطيل الحقوق.",
        "force_channels": "أرسل المعرفات مفصولة بفاصلة أو سطر. مثال: <code>@alii_index</code>\nأرسل <code>-</code> لتعطيل الاشتراك الإجباري.",
    }
    hint = hints.get(field, "أرسل النص الجديد (يدعم تنسيق HTML).")
    set_session(
        ADMIN_ID, type="admin_input", action="set", field=field, lang=lang or None
    )
    edit(
        call,
        f"✏️ تعديل: <b>{SETTINGS_FIELDS.get(field, field)}</b> {lang.upper() if lang else ''}\n\n{hint}\n\n"
        f"<b>القيمة الحالية:</b>\n<pre>{esc(cur)}</pre>",
        admin_cancel(),
        credits=False,
    )


def handle_admin_state(msg, sess):
    typ = sess["type"]
    # ----- ردّ الأدمن على مستخدم في الدعم الفني -----
    if typ == "admin_support":
        target = sess["target"]
        content = extract_content(msg)
        if not content:
            return send(ADMIN_ID, tr(ADMIN_ID, "unsupported"), credits=False)
        try:
            send_content(
                target,
                content,
                header=tr(target, "support_hdr"),
                markup=end_markup(target),
            )
            send(
                ADMIN_ID,
                "✅ تم إرسال الرد.",
                reply_parameters=reply_to(msg),
                credits=False,
            )
        except ApiTelegramException:
            send(ADMIN_ID, "⚠️ تعذر إرسال الرد (المستخدم حظر البوت؟).", credits=False)
        return
    # ----- إذاعة -----
    if typ == "admin_broadcast":
        mode = sess["mode"]
        end_session(ADMIN_ID, "admin")
        threading.Thread(
            target=run_broadcast, args=(msg.chat.id, msg.message_id, mode), daemon=True
        ).start()
        return
    # ----- إدخال نصي (حظر / إلغاء حظر / إعدادات) -----
    if msg.content_type != "text":
        return send(ADMIN_ID, "⚠️ أرسل نصاً.", admin_cancel(), credits=False)
    action = sess["action"]
    if action in ("ban", "unban"):
        if not msg.text.strip().isdigit():
            return send(
                ADMIN_ID, "⚠️ أرسل رقم ID صحيحاً.", admin_cancel(), credits=False
            )
        target = msg.text.strip()
        rec = users.data.get(target)
        if not rec:
            return send(
                ADMIN_ID,
                "⚠️ هذا المستخدم غير موجود في قاعدة البيانات.",
                admin_cancel(),
                credits=False,
            )
        rec["banned"] = action == "ban"
        users.save()
        end_session(ADMIN_ID, "admin")
        if action == "ban":
            end_session(int(target), "admin")
            send(int(target), tr(int(target), "banned"), credits=False)
        send(
            ADMIN_ID,
            f"✅ تم {'حظر' if action == 'ban' else 'إلغاء حظر'} <code>{target}</code>.",
            admin_back(),
            credits=False,
        )
        return
    if action == "set":
        field, lang = sess["field"], sess.get("lang")
        text = msg.text.strip()
        if field == "credits":
            if text == "-":
                config.data["credits"] = {"text": "", "url": ""}
            elif "|" in text:
                t, u = [x.strip() for x in text.split("|", 1)]
                config.data["credits"] = {"text": t, "url": u}
            else:
                return send(
                    ADMIN_ID,
                    "⚠️ الصيغة: <code>النص | الرابط</code>",
                    admin_cancel(),
                    credits=False,
                )
        elif field == "force_channels":
            if text == "-":
                config.data["force_channels"] = []
            else:
                chans = [
                    c
                    for c in re.split(r"[,\s]+", text)
                    if re.fullmatch(r"@[A-Za-z0-9_]{4,}|-100\d+", c)
                ]
                if not chans:
                    return send(
                        ADMIN_ID,
                        "⚠️ لم أجد معرّفات صحيحة (مثل @channel).",
                        admin_cancel(),
                        credits=False,
                    )
                config.data["force_channels"] = chans
            _sub_cache.clear()
        else:
            config.data.setdefault(field, {})[
                lang
            ] = msg.html_text  # نحفظ التنسيق كـ HTML
        config.save()
        end_session(ADMIN_ID, "admin")
        send(ADMIN_ID, "✅ تم حفظ الإعداد.", admin_back(), credits=False)


def run_broadcast(from_chat, message_id, mode):
    """إذاعة لجميع المستخدمين غير المحظورين (تعمل في خيط مستقل مع مراعاة حدود تيليجرام)."""
    ids = [int(k) for k, v in users.data.items() if not v.get("banned")]
    send(ADMIN_ID, f"⏳ بدأت الإذاعة إلى {len(ids)} مستخدم...", credits=False)
    ok = fail = 0
    for target in ids:
        for _ in range(2):
            try:
                if mode == "forward":
                    bot.forward_message(target, from_chat, message_id)
                else:
                    bot.copy_message(target, from_chat, message_id)
                ok += 1
                break
            except ApiTelegramException as e:
                if e.error_code == 429:  # تجاوز الحد → ننتظر ثم نعيد المحاولة
                    wait = (e.result_json.get("parameters") or {}).get("retry_after", 3)
                    time.sleep(wait + 1)
                    continue
                fail += 1
                break
        time.sleep(0.05)
    send(
        ADMIN_ID,
        f"✅ انتهت الإذاعة.\n📬 نجح: <b>{ok}</b>\n❌ فشل: <b>{fail}</b>",
        admin_back(),
        credits=False,
    )


# ---------- الدعم الفني (جهة الأدمن) ----------
def cb_support_reply(call, arg):
    if call.from_user.id != ADMIN_ID:
        return ans(call)
    ans(call)
    target = int(arg)
    set_session(ADMIN_ID, type="admin_support", target=target)
    send(
        ADMIN_ID,
        f"✍️ أنت الآن ترد على <code>{target}</code>. أرسل رسالتك (أو /cancel للخروج).",
        admin_cancel(),
        credits=False,
    )


def cb_support_end(call, arg):
    if call.from_user.id != ADMIN_ID:
        return ans(call)
    target = int(arg)
    s = get_session(target)
    if s and s["type"] == "support":
        end_session(target, "admin")
    send(target, tr(target, "support_closed"), menu_btn(target), credits=False)
    a = get_session(ADMIN_ID)
    if a and a["type"] == "admin_support" and a.get("target") == target:
        end_session(ADMIN_ID, "admin")
    ans(call, "✅ تم إنهاء جلسة الدعم.", alert=True)


# ======================================================================
# 15) موجّه الـ Callbacks
# ======================================================================
CALLBACKS = {
    "chk": cb_check_sub,
    "lang": cb_lang,
    "chlang": cb_choose_lang,
    "menu": cb_menu,
    "new": cb_new,
    "my": cb_mylink,
    "del": cb_delete,
    "delok": cb_delete_ok,
    "pol": cb_policy,
    "faq": cb_faq,
    "chan": cb_channel,
    "favs": cb_favs,
    "fshow": cb_fav_show,
    "fdel": cb_fav_del,
    "fav": cb_fav_save,
    "pub": cb_publish,
    "rep": cb_reply,
    "live": cb_live_request,
    "lcy": cb_live_accept,
    "lcn": cb_live_decline,
    "end": cb_end,
    "sup": cb_support,
    "adm": cb_admin,
    "admset": cb_admin_set,
    "supr": cb_support_reply,
    "supe": cb_support_end,
}


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    try:
        if call.message is None or call.message.chat.type != "private":
            return ans(call)
        uid = call.from_user.id
        action, _, arg = (call.data or "").partition(":")
        handler = CALLBACKS.get(action)
        if not handler:
            return ans(call)
        if not get_user(uid):
            register_user(call.from_user)
        # فحص الحظر/الصيانة/الاشتراك (زر التحقق يفحص الاشتراك بنفسه)
        if not guard(uid, call.message.chat.id, check_sub=(action != "chk")):
            return ans(call)
        handler(call, arg)
    except Exception:
        log.exception("callback error: %s", getattr(call, "data", None))
        ans(call)


# ======================================================================
# 16) التشغيل
# ======================================================================
def main():
    global BOT_USERNAME, BOT_ID
    me = bot.get_me()
    BOT_USERNAME, BOT_ID = me.username, me.id
    sessions.data["pending"].clear()  # طلبات الدردشة المعلّقة لا تُحفظ بين التشغيلات
    scheduler.start()
    scheduler.add_job(
        flush_all, "interval", seconds=5, id="flush", replace_existing=True
    )
    restore_jobs()
    atexit.register(flush_all)
    log.info("✅ البوت يعمل: @%s", BOT_USERNAME)
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        allowed_updates=[
            "message",
            "callback_query",
            "message_reaction",
        ],  # message_reaction للتفاعلات
    )


if __name__ == "__main__":
    main()
