#!/usr/bin/env python3
"""Local test: Steam link forms -> steamid (needs STEAM_API_KEY in env). python test_steam_links.py"""
import os; os.environ.setdefault("DATA_DIR", "/tmp/akdata"); os.makedirs("/tmp/akdata/logs", exist_ok=True)
import bot
G = "76561197960287930"  # public test profile (Gabe Newell, vanity gabelogannewell)
CASES = [(f"https://steamcommunity.com/profiles/{G}", G), (f"https://steamcommunity.com/profiles/{G}/", G),
         (f"steamcommunity.com/profiles/{G}", G), (f"http://www.steamcommunity.com/profiles/{G}/?l=russian", G),
         ("https://steamcommunity.com/id/gabelogannewell", G), ("https://steamcommunity.com/id/gabelogannewell/", G),
         ("steamcommunity.com/id/gabelogannewell", G), (G, G), ("gabelogannewell", G), (f"вот мой профиль: https://steamcommunity.com/profiles/{G}/ спасибо", G),
         ("https://s.team/p/hjqp/ABCDEFGH", G), ("s.team/p/hjqp", G), ("https://steamcommunity.com/user/hjqp/ABCDEFGH/", G),
         ("https://s.team/p/nqct-ptp/TQKNVJRK", "76561198122916570"),
         ("https://google.com/x", None), ("привет как дела", None), ("https://steamcommunity.com/id/zz_no_such_user_zz_123456", None)]
ok = 0
for text, want in CASES:
    sid, why = bot.steam_id_from(text); good = sid == want; ok += good
    print("OK  " if good else "FAIL", text[:60], "->", sid, why or "")
print(f"{ok}/{len(CASES)} passed")
