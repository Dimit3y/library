import sqlite3
import asyncio
import os
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# 1. НАСТРОЙКИ
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = "/webhook"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8080))

if not API_TOKEN:
    raise Exception("BOT_TOKEN not set")
if not WEBHOOK_URL:
    raise Exception("WEBHOOK_URL not set")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# 2. БАЗА ДАННЫХ
def init_db():
    print(">>> Initializing database...", file=sys.stderr)
    conn = sqlite3.connect('library.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS books 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                       title TEXT, 
                       author TEXT, 
                       count INTEGER DEFAULT 1)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS students 
                      (tg_id INTEGER PRIMARY KEY, name TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS issued_books 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                       book_id INTEGER,
                       user_id INTEGER,
                       issued_at TIMESTAMP,
                       FOREIGN KEY(book_id) REFERENCES books(id))''')
    conn.commit()
    conn.close()
    print(">>> Database initialized", file=sys.stderr)

# 3. КЛАВИАТУРЫ И СОСТОЯНИЯ
def main_menu():
    buttons = [
        [InlineKeyboardButton(text="📚 Список книг", callback_data="list_books")],
        [InlineKeyboardButton(text="👤 Мои книги", callback_data="my_books")],
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_book")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

class SearchState(StatesGroup):
    waiting_for_query = State()

# 4. ОБРАБОТЧИКИ
@dp.message(Command("start"))
async def start(message: Message):
    conn = sqlite3.connect('library.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO students (tg_id, name) VALUES (?, ?)", 
                   (message.from_user.id, message.from_user.first_name))
    conn.commit()
    conn.close()
    await message.answer(f"Привет, {message.from_user.first_name}! Я бот библиотеки. Выбери действие:", 
                         reply_markup=main_menu())

@dp.callback_query(F.data == "list_books")
async def show_books(callback: CallbackQuery):
    conn = sqlite3.connect('library.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, author, count FROM books")
    books = cursor.fetchall()
    conn.close()

    if not books:
        await callback.message.answer("В библиотеке пока нет книг.")
        return

    text = "📖 Список книг:\n\n"
    keyboard = []
    for b_id, title, author, count in books:
        status = f"✅ Доступно: {count}" if count > 0 else "❌ Нет в наличии"
        text += f"{b_id}. {title} — {author} [{status}]\n"
        if count > 0:
            keyboard.append([InlineKeyboardButton(text=f"📥 Взять {title}", callback_data=f"take_{b_id}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("take_"))
async def take_book(callback: CallbackQuery):
    book_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    conn = sqlite3.connect('library.db')
    cursor = conn.cursor()
    cursor.execute("SELECT count FROM books WHERE id = ?", (book_id,))
    row = cursor.fetchone()
    if not row or row[0] <= 0:
        await callback.answer("Книги нет в наличии!", show_alert=True)
        conn.close()
        await show_books(callback)
        return

    cursor.execute("UPDATE books SET count = count - 1 WHERE id = ?", (book_id,))
    cursor.execute("INSERT INTO issued_books (book_id, user_id, issued_at) VALUES (?, ?, ?)",
                   (book_id, user_id, datetime.now()))
    conn.commit()
    conn.close()
    await callback.answer("Вы успешно взяли книгу!", show_alert=True)
    await show_books(callback)

@dp.callback_query(F.data == "my_books")
async def my_books(callback: CallbackQuery):
    conn = sqlite3.connect('library.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT b.id, b.title, ib.issued_at, ib.id
        FROM issued_books ib
        JOIN books b ON ib.book_id = b.id
        WHERE ib.user_id = ?
    ''', (callback.from_user.id,))
    books = cursor.fetchall()
    conn.close()

    if not books:
        await callback.message.answer("У вас на руках нет книг.")
        return

    text = "📖 Ваши книги:\n\n"
    keyboard = []
    for book_id, title, issued_at, issued_id in books:
        text += f"{title} (взято: {issued_at[:10]})\n"
        keyboard.append([InlineKeyboardButton(text=f"🔁 Вернуть {title}", callback_data=f"return_{issued_id}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("return_"))
async def return_book(callback: CallbackQuery):
    issued_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    conn = sqlite3.connect('library.db')
    cursor = conn.cursor()
    # Проверяем, существует ли запись и принадлежит ли пользователю
    cursor.execute("SELECT book_id, user_id FROM issued_books WHERE id = ?", (issued_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Книга уже возвращена!", show_alert=True)
        conn.close()
        await my_books(callback)
        return
    
    book_id, owner_id = row
    if owner_id != user_id:
        await callback.answer("Это не ваша книга!", show_alert=True)
        conn.close()
        await my_books(callback)
        return
    
    # Удаляем конкретную запись
    cursor.execute("DELETE FROM issued_books WHERE id = ?", (issued_id,))
    # Увеличиваем количество экземпляров
    cursor.execute("UPDATE books SET count = count + 1 WHERE id = ?", (book_id,))
    conn.commit()
    conn.close()
    
    await callback.answer("Книга возвращена в библиотеку!", show_alert=True)
    await my_books(callback)

@dp.callback_query(F.data == "search_book")
async def search_start(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")]
    ])
    await callback.message.answer(
        "🔍 Введите название книги или автора для поиска.\n"
        "Например: *Пушкин* или *Война и мир*",
        reply_markup=keyboard
    )
    await state.set_state(SearchState.waiting_for_query)
    await callback.answer()

@dp.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Поиск отменён.", reply_markup=main_menu())
    await callback.answer()

@dp.message(StateFilter(SearchState.waiting_for_query))
async def process_search_query(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("Пожалуйста, введите что-нибудь для поиска.")
        return

    conn = sqlite3.connect('library.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, author, count 
        FROM books 
        WHERE LOWER(title) LIKE LOWER(?) OR LOWER(author) LIKE LOWER(?)
        ORDER BY title
    """, (f"%{query}%", f"%{query}%"))
    books = cursor.fetchall()
    conn.close()

    if not books:
        await message.answer(f"По запросу «{query}» ничего не найдено.")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")]
        ])
        await message.answer("Попробуйте другой запрос или нажмите Отмена.", reply_markup=keyboard)
        return

    text = f"📚 Результаты поиска по запросу «{query}»:\n\n"
    keyboard = []
    for b_id, title, author, count in books:
        status = f"✅ Доступно: {count}" if count > 0 else "❌ Нет в наличии"
        text += f"▪️ {title} — {author} [{status}]\n"
        if count > 0:
            keyboard.append([InlineKeyboardButton(text=f"📥 Взять {title}", callback_data=f"take_{b_id}")])

    keyboard.append([
        InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_main"),
        InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_again")
    ])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.clear()

@dp.callback_query(F.data == "search_again")
async def search_again(callback: CallbackQuery, state: FSMContext):
    await search_start(callback, state)

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.answer("Выберите действие:", reply_markup=main_menu())

@dp.message(Command("add"))
async def add_book(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split("|")
        if len(parts) < 2:
            raise ValueError("Недостаточно параметров")
        title = parts[0].split(" ", 1)[1].strip()
        author = parts[1].strip()
        count = int(parts[2].strip()) if len(parts) > 2 else 1
        conn = sqlite3.connect('library.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO books (title, author, count) VALUES (?, ?, ?)", 
                       (title, author, count))
        conn.commit()
        conn.close()
        await message.answer(f"Книга добавлена! (Количество: {count})")
    except Exception as e:
        await message.answer("Ошибка! Пиши: /add Название | Автор | количество (опционально)\nПример: /add Капитанская дочка | Пушкин | 3")

# 5. WEBHOOK
async def on_startup(app: web.Application):
    """Устанавливает вебхук при старте сервера"""
    print(f">>> Setting webhook to {WEBHOOK_URL}", file=sys.stderr)
    await bot.set_webhook(WEBHOOK_URL)

def main():
    print(">>> Starting bot application", file=sys.stderr)
    init_db()  # обязательно!
    
    app = web.Application()
    webhook_handler = SimpleRequestHandler(dp, bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    
    print(f">>> Starting web server on {WEBAPP_HOST}:{WEBAPP_PORT}", file=sys.stderr)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)

if __name__ == "__main__":
    main()
