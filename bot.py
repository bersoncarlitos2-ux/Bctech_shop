"""BC TECH SHOP - FASE 1: menu e loja (só leitura, sem compras nem depósitos)."""
import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager

from telegram import (InlineKeyboardButton as B, InlineKeyboardMarkup as M,
                      ReplyKeyboardMarkup, Update)
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import provider
from provider import ProviderError

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
STORE = os.getenv("STORE_NAME", "BC TECH SHOP")
MARGIN = float(os.getenv("MARGIN", "0.30"))            # 0.30 = vendes 30% acima do custo
MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.25"))    # lucro mínimo por venda (USD)
SUPPORT = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
DB_PATH = os.getenv("DB_PATH", "loja.db")
# Categorias a esconder (nomes separados por vírgula). Vazio = mostra tudo.
HIDE = {x.strip().lower() for x in os.getenv("HIDE_GROUPS", "").split(",") if x.strip()}
PER_PAGE = 10  # marcas por página (2 colunas)

KEYBOARD = ReplyKeyboardMarkup(
    [["🏠 Início", "💰 Saldo"], ["📜 Histórico", "💬 Suporte"]], resize_keyboard=True)


# ---------- base de dados ----------
@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        with c:  # commit no fim, ou desfaz se der erro
            yield c
    finally:
        c.close()


def add_cols(table, cols):
    """Junta colunas que faltem (útil se já existir uma base de dados antiga)."""
    with db() as c:
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols.items():
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, balance REAL DEFAULT 0, spent REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, method TEXT, amount REAL, proof TEXT, status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, product_id TEXT, name TEXT, price REAL, cost REAL DEFAULT 0,
            ref TEXT, items TEXT, status TEXT, created TEXT);
        """)
    add_cols("users", {"spent": "REAL DEFAULT 0"})
    add_cols("orders", {"cost": "REAL DEFAULT 0", "items": "TEXT", "created": "TEXT", "ref": "TEXT"})


def ensure_user(uid):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))


def get_user(uid):
    ensure_user(uid)
    with db() as c:
        return c.execute("SELECT balance, spent FROM users WHERE id=?", (uid,)).fetchone()


def order_counts(uid):
    with db() as c:
        r = c.execute("SELECT COUNT(*) total, COALESCE(SUM(status='done'),0) done "
                      "FROM orders WHERE user_id=?", (uid,)).fetchone()
    return {"total": r["total"], "done": r["done"], "pending": r["total"] - r["done"]}


def history(uid, limit=10):
    with db() as c:
        return c.execute("SELECT id, name, price, status FROM orders WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall()


def sell_price(cost):
    """Custo + margem, com um lucro mínimo garantido."""
    return round(max(cost * (1 + MARGIN), cost + MIN_PROFIT), 2)


# ---------- textos e botões ----------
def home_text(uid):
    u, n = get_user(uid), order_counts(uid)
    return (f"👋 Bem-vindo à {STORE}!\n\n"
            f"💰 Saldo: ${u['balance']:.2f}\n"
            f"🧾 Total gasto: ${u['spent']:.2f}\n\n"
            f"📦 PEDIDOS\n"
            f"⏳ Pendentes: {n['pending']}\n"
            f"✅ Entregues: {n['done']}\n"
            f"📋 Total: {n['total']}\n\n"
            "💡 Usa os botões abaixo para comprar, recarregar a carteira ou ver o teu perfil.\n"
            "Escolhe uma opção:")


def home_markup():
    return M([
        [B("🛍️ Loja", callback_data="shop"), B("➕ Depositar", callback_data="dep")],
        [B("👤 Perfil", callback_data="perfil"), B("🧹 Limpar chat", callback_data="clear")],
    ])


def back_home():
    return M([[B("🏠 Início", callback_data="home")]])


def saldo_text(uid):
    u = get_user(uid)
    return f"💰 Saldo: ${u['balance']:.2f}\n🧾 Total gasto: ${u['spent']:.2f}"


def perfil_text(uid, name):
    u, n = get_user(uid), order_counts(uid)
    return (f"👤 O teu perfil\n\nNome: {name}\nID: {uid}\n\n"
            f"💰 Saldo: ${u['balance']:.2f}\n🧾 Total gasto: ${u['spent']:.2f}\n"
            f"📋 Pedidos: {n['total']} (entregues: {n['done']})")


STATUS = {"done": "✅ entregue", "manual": "⏳ a aguardar", "check": "⏳ em confirmação"}


def historico_text(uid):
    rows = history(uid)
    if not rows:
        return "📜 Histórico\n\nAinda não tens pedidos."
    lines = [f"#{r['id']} {r['name'][:30]} — ${r['price']:.2f} — {STATUS.get(r['status'], r['status'])}"
             for r in rows]
    return "📜 Últimos pedidos\n\n" + "\n".join(lines)


def track(ctx, msg):
    """Guarda o ID das mensagens para o botão 'Limpar chat'."""
    mid = msg if isinstance(msg, int) else msg.message_id
    ids = ctx.chat_data.setdefault("ids", [])
    ids.append(mid)
    del ids[:-300]


async def say(ctx, chat_id, text, markup=None):
    m = await ctx.bot.send_message(chat_id, text, reply_markup=markup)
    track(ctx, m)
    return m


async def show(q, ctx, text, markup=None):
    """Edita a mensagem do botão (menos confusão no chat)."""
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await say(ctx, q.message.chat_id, text, markup)


async def notify_admin(bot, text):
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception:
        logging.exception("Não consegui avisar o admin")


async def provider_fail(q, ctx, e):
    await show(q, ctx, f"❌ {e.public}", back_home())
    if e.internal:
        await notify_admin(ctx.bot, f"⚠️ Problema com o fornecedor: {e}")


# ---------- catálogo ----------
def build_groups(items):
    g = {}
    for p in items:
        name = str(p["group_name"] or p["name"]).strip()
        if name.lower() in HIDE:
            continue
        k = str(p["group_id"] or p["id"])
        g.setdefault(k, {"name": name, "plans": []})["plans"].append(p)
    return g


def stock_label(plans):
    if any(p["infinite"] for p in plans):
        return "∞"
    total = sum(p["stock"] or 0 for p in plans)
    return str(total) if total else "—"


def stock_text(p):
    if p["infinite"]:
        return "ilimitado"
    return str(p["stock"]) if p["stock"] is not None else "—"


async def show_groups(q, ctx, page=0):
    try:
        items = await asyncio.to_thread(provider.catalog)
    except ProviderError as e:
        await provider_fail(q, ctx, e)
        return
    groups = build_groups(items)
    if not groups:
        await show(q, ctx, "Sem produtos disponíveis agora. Tenta mais tarde.", back_home())
        return
    keys = sorted(groups, key=lambda k: groups[k]["name"].lower())
    pages = (len(keys) + PER_PAGE - 1) // PER_PAGE
    page = max(0, min(page, pages - 1))
    ctx.user_data["page"] = page
    chunk = keys[page * PER_PAGE:(page + 1) * PER_PAGE]
    btns = [B(f"{groups[k]['name'][:24]} ({stock_label(groups[k]['plans'])})", callback_data=f"g:{k}")
            for k in chunk]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(B("⬅️", callback_data=f"gp:{page - 1}"))
        nav.append(B(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(B("➡️", callback_data=f"gp:{page + 1}"))
        rows.append(nav)
    rows.append([B("🏠 Início", callback_data="home")])
    await show(q, ctx, f"🛍️ Loja — escolhe o produto ({len(keys)} marcas):", M(rows))


async def show_plans(q, ctx, gkey):
    try:
        items = await asyncio.to_thread(provider.catalog)
    except ProviderError as e:
        await provider_fail(q, ctx, e)
        return
    g = build_groups(items).get(gkey)
    if not g:
        await show(q, ctx, "Esse produto já não está disponível.",
                   M([[B("🔙 Loja", callback_data="gp:0")]]))
        return
    plans = sorted(g["plans"], key=lambda p: p["cost"])
    if len(plans) == 1:
        await show_product(q, ctx, plans[0]["id"])
        return
    rows = [[B(f"{(p['variant'] or p['name'])[:34]} — ${sell_price(p['cost']):.2f}",
               callback_data=f"p:{p['id']}")] for p in plans[:40]]
    rows.append([B("🔙 Loja", callback_data=f"gp:{ctx.user_data.get('page', 0)}")])
    await show(q, ctx, f"{g['name']} — escolhe o plano:", M(rows))


async def show_product(q, ctx, pid):
    try:
        p = await asyncio.to_thread(provider.product, pid)
    except ProviderError as e:
        await provider_fail(q, ctx, e)
        return
    price = sell_price(p["cost"])
    war = f"{p['warranty_hours']}h" if p.get("warranty_hours") else "sem garantia"
    text = f"📦 {p['name']}\n💵 Preço: ${price:.2f}\n🛡️ Garantia: {war}\n📊 Stock: {stock_text(p)}"
    if p.get("note"):
        text += f"\n\nℹ️ {p['note'][:1200]}"
    text += "\n\n🛒 A compra com saldo é ativada na próxima fase."
    await show(q, ctx, text, M([[B("🔙 Loja", callback_data=f"gp:{ctx.user_data.get('page', 0)}")]]))


# ---------- comandos ----------
async def send_home(ctx, chat_id, uid):
    await say(ctx, chat_id, home_text(uid), home_markup())


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    ensure_user(uid)
    track(ctx, update.message)
    await say(ctx, chat_id, f"🏪 {STORE}", KEYBOARD)
    await send_home(ctx, chat_id, uid)


async def fornecedor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        bal = await asyncio.to_thread(provider.get_balance)
        await update.message.reply_text(f"Saldo no fornecedor: ${bal:.2f}")
    except ProviderError as e:
        await update.message.reply_text(f"❌ {e}")


async def teste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Só admin: mostra como a API real responde (sem mostrar a chave)."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("A testar a API...")
    txt = await asyncio.to_thread(provider.diagnostico)
    for i in range(0, len(txt), 3800):
        await update.message.reply_text(txt[i:i + 3800])


# ---------- botões ----------
async def clear_chat(q, ctx):
    chat_id = q.message.chat_id
    ids = ctx.chat_data.pop("ids", [])
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        try:
            await ctx.bot.delete_messages(chat_id, batch)
        except Exception:
            for mid in batch:
                try:
                    await ctx.bot.delete_message(chat_id, mid)
                except Exception:
                    pass
    await send_home(ctx, chat_id, q.from_user.id)


async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = q.data
    await q.answer()

    if d == "noop":
        return
    if d == "home":
        await show(q, ctx, home_text(uid), home_markup())
    elif d == "shop":
        await show_groups(q, ctx, 0)
    elif d.startswith("gp:"):
        await show_groups(q, ctx, int(d[3:]))
    elif d.startswith("g:"):
        await show_plans(q, ctx, d[2:])
    elif d.startswith("p:"):
        await show_product(q, ctx, d[2:])
    elif d == "perfil":
        await show(q, ctx, perfil_text(uid, q.from_user.full_name), back_home())
    elif d == "dep":
        await show(q, ctx, "➕ Depositar\n\nAs recargas ficam disponíveis na próxima fase.", back_home())
    elif d == "clear":
        await clear_chat(q, ctx)


# ---------- mensagens de texto (teclado fixo) ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    track(ctx, update.message)
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    ensure_user(uid)
    t = (update.message.text or "").strip()
    if t.startswith("🏠"):
        await send_home(ctx, chat_id, uid)
    elif t.startswith("💰"):
        await say(ctx, chat_id, saldo_text(uid), back_home())
    elif t.startswith("📜"):
        await say(ctx, chat_id, historico_text(uid), back_home())
    elif t.startswith("💬"):
        if SUPPORT:
            await say(ctx, chat_id, "💬 Precisas de ajuda? Fala com o suporte:",
                      M([[B("Abrir suporte", url=f"https://t.me/{SUPPORT}")]]))
        else:
            await say(ctx, chat_id, "Suporte indisponível de momento.", back_home())
    else:
        await say(ctx, chat_id, "Usa o menu 👇", home_markup())


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    logging.error("Erro inesperado", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await ctx.bot.send_message(update.effective_chat.id,
                                       "⚠️ Ocorreu um erro. Tenta de novo daqui a pouco.")
    except Exception:
        pass
    await notify_admin(ctx.bot, f"⚠️ Erro no bot: {type(ctx.error).__name__}")


def main():
    init()
    logging.info("Bot a arrancar (fornecedor em modo: %s)", provider.MODE)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fornecedor", fornecedor))
    app.add_handler(CommandHandler("teste", teste))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
  
