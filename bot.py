# -*- coding: utf-8 -*-
"""
Telegram-бот «Кто ты на шахматной доске?» — MSM EduLeague

Запуск:
    1. python3 -m venv venv && source venv/bin/activate
    2. python3 -m pip install -r requirements.txt
    3. Заполнить .env (см. .env.example) или задать переменные окружения вручную
    4. python3 bot.py
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from quiz_data import (
    ANSWER_PROMPT,
    OPTION_ICONS,
    QUESTION_IMAGES,
    QUESTIONS,
    START_BUTTON_TEXT,
    WELCOME_IMAGE,
    WELCOME_TEXT,
    calculate_result,
    format_result_text,
)

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TOURNAMENT_LINK = os.environ.get("TOURNAMENT_LINK", "https://sapienschess.ru")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

EMPTY_SCORES = {"A": 0, "B": 0, "C": 0, "D": 0}
IMAGES_DIR = Path(__file__).parent / "images"

# Картинки читаем один раз в память при старте.
_QUESTION_IMAGE_BYTES: dict[int, bytes] = {}
for _idx, _filename in enumerate(QUESTION_IMAGES):
    _path = IMAGES_DIR / _filename
    if _path.exists():
        _QUESTION_IMAGE_BYTES[_idx] = _path.read_bytes()
    else:
        logger.warning("Не найдена картинка для вопроса %s: %s", _idx + 1, _path)

_welcome_path = IMAGES_DIR / WELCOME_IMAGE
_WELCOME_IMAGE_BYTES = _welcome_path.read_bytes() if _welcome_path.exists() else None
if _WELCOME_IMAGE_BYTES is None:
    logger.warning("Не найдена картинка приветствия: %s", _welcome_path)

T = TypeVar("T")


def get_question_photo(q_index: int) -> bytes:
    data = _QUESTION_IMAGE_BYTES.get(q_index)
    if data is None:
        raise FileNotFoundError(
            f"Картинка для вопроса {q_index + 1} отсутствует в папке images/"
        )
    return data


def get_welcome_photo() -> bytes:
    if _WELCOME_IMAGE_BYTES is None:
        raise FileNotFoundError(f"Картинка приветствия отсутствует: {_welcome_path}")
    return _WELCOME_IMAGE_BYTES


async def telegram_retry(
    action: Callable[[], Awaitable[T]],
    action_name: str,
    retries: int = 3,
) -> T:
    """Повторяет сетевые Telegram-запросы при временных сбоях прокси/сети."""
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            return await action()
        except RetryAfter as exc:
            last_error = exc
            delay = float(exc.retry_after) + 0.5
            logger.warning(
                "%s: Telegram просит подождать %.1f сек. Попытка %s/%s",
                action_name,
                delay,
                attempt,
                retries,
            )
            await asyncio.sleep(delay)
        except (NetworkError, TimedOut) as exc:
            last_error = exc
            if attempt >= retries:
                break
            delay = min(1.5 * attempt, 5.0)
            logger.warning(
                "%s: временная сетевая ошибка: %s. Повтор через %.1f сек. Попытка %s/%s",
                action_name,
                exc,
                delay,
                attempt,
                retries,
            )
            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


async def safe_answer_callback(query) -> None:
    """answerCallbackQuery нужен только чтобы убрать «часики» на кнопке.
    Если из-за прокси этот запрос не прошёл, логику теста всё равно продолжаем.
    """
    try:
        await query.answer()
    except (NetworkError, TimedOut) as exc:
        logger.warning("Не удалось ответить на callback_query, продолжаю обработку: %s", exc)


async def safe_delete(message: Message) -> None:
    try:
        await telegram_retry(lambda: message.delete(), "Удаление старого сообщения", retries=2)
    except BadRequest as exc:
        logger.debug("Сообщение уже недоступно для удаления: %s", exc)
    except Exception as exc:
        logger.warning("Не удалось удалить старое сообщение, продолжаю: %s", exc)


def build_question_keyboard(q_index: int) -> InlineKeyboardMarkup:
    """
    Telegram не даёт управлять размером/шрифтом inline-кнопок.
    Поэтому полный текст вариантов показываем в подписи к вопросу,
    а сами кнопки делаем максимально короткими и удобными для нажатия.
    """
    rows = []
    for i, (_option_text, score_type) in enumerate(QUESTIONS[q_index]["options"]):
        icon = OPTION_ICONS[i]
        rows.append(
            [
                InlineKeyboardButton(
                    icon,
                    callback_data=f"ans|{q_index}|{score_type}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Хочу на турнир!", callback_data="want_tournament")],
            [InlineKeyboardButton("🔄 Пройти заново", callback_data="restart")],
        ]
    )


def question_caption(q_index: int) -> str:
    question = QUESTIONS[q_index]
    options_text = "\n".join(
        f"{OPTION_ICONS[i]} {option_text}"
        for i, (option_text, _score_type) in enumerate(question["options"])
    )
    return (
        f"Вопрос {q_index + 1} из {len(QUESTIONS)}\n\n"
        f"{question['text']}\n\n"
        f"{options_text}\n\n"
        f"{ANSWER_PROMPT}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    context.user_data["scores"] = dict(EMPTY_SCORES)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(START_BUTTON_TEXT, callback_data="start_test")]]
    )
    await telegram_retry(
        lambda: update.message.reply_photo(
            photo=get_welcome_photo(),
            filename=WELCOME_IMAGE,
            caption=WELCOME_TEXT,
            reply_markup=keyboard,
        ),
        "Отправка приветствия",
    )


async def send_first_question(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переход на фото вопроса 1."""
    chat_id = query.message.chat_id
    await safe_delete(query.message)
    await telegram_retry(
        lambda: context.bot.send_photo(
            chat_id=chat_id,
            photo=get_question_photo(0),
            filename=QUESTION_IMAGES[0],
            caption=question_caption(0),
            reply_markup=build_question_keyboard(0),
        ),
        "Отправка первого вопроса",
    )


async def show_next_question(query, context: ContextTypes.DEFAULT_TYPE, q_index: int) -> None:
    """Переход фото-вопрос -> фото-вопрос: обновляем медиа в том же сообщении."""
    await telegram_retry(
        lambda: query.edit_message_media(
            media=InputMediaPhoto(
                media=get_question_photo(q_index),
                filename=QUESTION_IMAGES[q_index],
                caption=question_caption(q_index),
            ),
            reply_markup=build_question_keyboard(q_index),
        ),
        f"Показ вопроса {q_index + 1}",
    )


async def show_result(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переход фото-вопрос -> текст-результат."""
    scores = context.user_data.get("scores", dict(EMPTY_SCORES))
    result = calculate_result(scores)
    text = format_result_text(result)

    chat_id = query.message.chat_id
    await safe_delete(query.message)
    await telegram_retry(
        lambda: context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=build_result_keyboard(),
        ),
        "Отправка результата",
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    await safe_answer_callback(query)
    data = query.data or ""

    if data == "start_test":
        context.user_data["scores"] = dict(EMPTY_SCORES)
        await send_first_question(query, context)
        return

    if data == "restart":
        context.user_data["scores"] = dict(EMPTY_SCORES)
        await send_first_question(query, context)
        return

    if data == "want_tournament":
        text = f"Проверь свой стиль в реальной игре! 👉 {TOURNAMENT_LINK}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Пройти заново", callback_data="restart")]]
        )
        chat_id = query.message.chat_id
        await safe_delete(query.message)
        tournament_image_path = IMAGES_DIR / "chess_competition.jpg"
        if tournament_image_path.exists():
            await telegram_retry(
                lambda: context.bot.send_photo(
                    chat_id=chat_id,
                    photo=tournament_image_path.read_bytes(),
                    caption=text,
                    reply_markup=keyboard,
                ),
                "Показ ссылки на турнир с картинкой",
            )
        else:
            await telegram_retry(
                lambda: context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                ),
                "Показ ссылки на турнир",
            )
        return

    if data.startswith("ans|"):
        try:
            _, q_index_str, score_type = data.split("|")
            q_index = int(q_index_str)
        except ValueError:
            logger.warning("Некорректный callback_data: %s", data)
            return

        if score_type not in EMPTY_SCORES:
            logger.warning("Неизвестный тип результата в callback_data: %s", data)
            return

        scores = context.user_data.setdefault("scores", dict(EMPTY_SCORES))
        scores[score_type] = scores.get(score_type, 0) + 1

        next_index = q_index + 1
        if next_index < len(QUESTIONS):
            await show_next_question(query, context, next_index)
        else:
            await show_result(query, context)
        return

    logger.warning("Неизвестный callback_data: %s", data)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Чтобы в логах не было 'No error handlers are registered'."""
    logger.exception("Ошибка при обработке update=%s", update, exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Создайте файл .env на основе .env.example "
            "или экспортируйте переменную окружения BOT_TOKEN."
        )

    missing = [i + 1 for i in range(len(QUESTIONS)) if i not in _QUESTION_IMAGE_BYTES]
    if missing:
        logger.warning(
            "Не хватает картинок для вопросов: %s — бот всё равно запустится, "
            "но эти вопросы упадут с ошибкой при показе.",
            missing,
        )

    # Таймауты увеличены с запасом на случай нестабильного соединения.
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
        connection_pool_size=8,
        httpx_kwargs={"trust_env": False},
    )

    get_updates_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
        connection_pool_size=8,
        httpx_kwargs={"trust_env": False},
    )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    logger.info("Бот запущен, ожидаю сообщения...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
