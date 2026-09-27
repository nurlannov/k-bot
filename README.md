# K Telegram Bot

Telegram-бот для просмотра и бронирования мест на K.

## Что делает

- Принимает ссылку на сеанс K
- Показывает даты сеанса, сектора, ряды, свободные места
- Бронирует выбранные места в корзину
- Следит за таймером корзины и восстанавливает бронь, если слетела
- Стоп по команде `/stop`

## Установка

1. Python 3.10+
2. `pip install -r requirements.txt`
3. Скопировать `config.example.py` → `config.py`, вписать:
   - `TELEGRAM_BOT_TOKEN` — от [@BotFather](https://t.me/BotFather)
   - `USER_UUID` — из localStorage `kassir.kg`
   - `ALLOWED_USER_IDS` — свой telegram id от [@userinfobot](https://t.me/userinfobot)

## Запуск
