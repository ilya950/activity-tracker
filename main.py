"""
Точка входа для хостинга (Bothost, Bot-Hosting и любых других).

Запуск локально:   python main.py
Запуск на сервере: python main.py   (эта же команда указывается как стартовая)

Ничего лишнего здесь нет: файл просто вызывает запуск бота из telegram_bot/bot.py.
Он нужен потому, что хостинги ищут главный файл в корне проекта, а наш бот
лежит в подпапке telegram_bot.
"""

import os
import sys

# Чтобы импорт пакета telegram_bot работал из любой папки
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram_bot.bot import main

if __name__ == "__main__":
    main()
