# Encrypt the current local state.json into state_seed.enc (key from STATE_KEY env var). Run right after stopping the local bot.
import os, gzip
from cryptography.fernet import Fernet
src = os.environ.get("SRC_STATE", "/home/box/tg/lobbyline_bot/state.json")
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_seed.enc"), "wb").write(
    Fernet(os.environ["STATE_KEY"].encode()).encrypt(gzip.compress(open(src, "rb").read())))
print("seed written")
