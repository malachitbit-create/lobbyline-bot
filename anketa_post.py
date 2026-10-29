#!/usr/bin/env python3
"""Post inviting members to fill the questionnaire (topic «Анкеты»).
  python anketa_post.py preview   -> sends the exact post to the owner's DM only
  python anketa_post.py publish   -> posts in group -1004495233101, thread 86, and pins it (only after mama's «публикуй»)
"""
import sys, os
os.environ.setdefault("DATA_DIR", "/tmp/akdata"); os.makedirs(os.path.join(os.environ["DATA_DIR"], "logs"), exist_ok=True)
import bot
GROUP, THREAD, OWNER = -1004495233101, 86, 5856323609
TEXT = ("🪐 <b>Анкета игрока Lobby Line</b>\n\n"
        "Расскажи, во что и когда играешь — соберём тебе стильную карточку ✨\nЖми кнопку ниже: анкета откроется прямо здесь, в этой теме 👇\n\n"
        "🔗 Кинь ссылку на Steam — уровень и часы подтянутся сами\n"
        "✍️ Или отметь всё кнопками вручную\n"
        "🎭 Роли и позиции · 🕰 удобное время (МСК)\n\n"
        "Чужую анкету смотри командой /profile @ник 👀")
def markup():
    return {"inline_keyboard": [[{"text": "📝 Анкета", "callback_data": "a:go", "style": "primary"}]]}
def send(chat, **extra):
    png = bot.render_banner("mode", 1, 7, intro=True)
    return bot.tg("sendPhoto", files={"photo": ("anketa_post.png", png, "image/png")}, chat_id=chat, caption=TEXT,
                  parse_mode="HTML", reply_markup=markup(), **extra)
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "preview":
        j = send(OWNER); print("preview", j.get("ok"), j.get("description"))
    elif mode == "publish":
        j = send(GROUP, message_thread_id=THREAD); print("publish", j.get("ok"), j.get("description"))
        if j.get("ok"):
            p = bot.tg("pinChatMessage", chat_id=GROUP, message_id=j["result"]["message_id"], disable_notification=True)
            print("pinned", p.get("ok"), j["result"]["message_id"])
    else: print(__doc__)
