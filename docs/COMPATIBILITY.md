# Совместимость и основания реализации

Проверено 6 ноября 2026 года. Версия плагина 1.1.0.

## Cardinal

Целевой проект: [sidor0912/FunPayCardinal](https://github.com/sidor0912/FunPayCardinal), ревизия `c09dd13be25072bb3d5cdbba33d1e0d70e477710`.

- `Cardinal.load_plugin`: один Python-файл, обязательные `NAME`, `VERSION`, `DESCRIPTION`, `CREDITS`, `UUID`, `SETTINGS_PAGE`, `BIND_TO_DELETE`.
- События `BIND_TO_PRE_INIT`, `BIND_TO_NEW_ORDER`, `BIND_TO_NEW_MESSAGE`, `BIND_TO_ORDER_STATUS_CHANGED`, `BIND_TO_PRE_STOP`.
- Telegram: `msg_handler`, `cbq_handler`, `add_telegram_commands`, `CBT.PLUGIN_SETTINGS`.
- Панель использует `InlineKeyboardMarkup`, `edit_message_text`, `CBT.EDIT_PLUGIN` и отдельное состояние ввода `ggp_input`; старые кнопки привязаны к конкретной сессии администратора и сообщению.
- Базовый обработчик заказа определяет `lot_id` по описанию. Плагин дополнительно получает полный заказ, сверяет продавца, покупателя, категорию, название, сервер/сторону и однозначность лота.
- Для выбранных лотов стандартную автовыдачу нужно отключить: она вызывается до сторонних обработчиков нового заказа.
- Старый режим получения сообщений не поддерживается: он не предоставляет необходимую проверку автора сообщения.

Для сравнения структуры изучены [SteamAutoPoints](https://github.com/Seno47/funpay-cardinal-plugins-archive/tree/main/plugins/steam-auto-points) и [Auto Robux](https://github.com/Seno47/funpay-cardinal-plugins-archive/tree/main/plugins/auto-robux), ревизия архива `73e385c8d4f7884cd91251cae73c9e9d09978602`. Их исходный код в плагин не переносился.

## GreenGamePay

Основные источники: [документация](https://greengamepay.readme.io/reference/информация), [OpenAPI работающего сервиса](https://пополнистим.рф/openapi.json).

| Операция | Метод и путь |
| --- | --- |
| Авторизация | POST `/api/token`, JSON username/password, access_token/expired в ответе |
| Steam | POST `/api/check`, `/api/create_order`, `/api/pay_order` |
| Пересчёт | POST `/api/get_amount` для RUB; `/api/rates` для KZT/UAH |
| Каталог/баланс | POST `/api/get_all_services`, `/api/check_balance`, `/api/get_ton_balance` |
| Stars | POST `/api/checkStarsRecipient`, `/api/get_stars_price`, `/api/buyStars` |
| Premium | POST `/api/get_premium_price`, `/api/buyPremium` |
| Steam Gifts | POST `/api/steam_gift_calculate`, `/api/steam_gift_create_order`, `/api/steam_gift_pay_order` |

Steam `custom_id` генерируется UUID v4 до создания и сохраняется в журнале. Для подарков Steam сервер возвращает собственный `custom_id`. Stars/Premium не принимают `custom_id` в опубликованной схеме.

Проверка оплаты Steam/gift основана на документированном `msg="Successfully paid the order"` и совпадающем `custom_id`. Для Telegram проверяются `msg="success"`, `tx_id`, получатель и объём. Неизвестный ответ приводит к ручной проверке, а не к ложному сообщению об успехе.

Ответы схемы часто описаны только примерами, без строгих JSON-моделей. В частности, у `/api/get_premium_price` нет примера ответа. Ожидаемое поле `ton` соответствует котировке Stars, но для Premium это **не подтверждено контрактом**. Без него покупка блокируется. Коды подарочных карт не выдаются: контракт не определяет поле/метод получения кодов.

## Что подтверждают проверки

- Дедупликацию повторных событий и сериализацию обработки.
- Сохранение фаз перед запросами создания/оплаты и восстановление после сбоя.
- Отсутствие повторов мутаций после таймаута, HTTP-ошибки или неожиданного ответа.
- Проверку получателя, покупателя, чата, продавца, статуса оплаты, лимитов и доступа к Telegram-командам.
- Безопасную остановку перед оплатой, тестовый режим, повтор сообщения без повторной покупки.
- Импорт одним файлом через настоящий загрузчик Cardinal (отдельная проверка).
- Кнопки, четыре сценария настройки лотов, ввод пароля, истечение/отмену мастера, страницы списков и защиту от устаревших callback-запросов. Внешний вид в работающем Telegram-боте пользователя не проверялся: тесты проверяют реальные структуры inline-клавиатур с подменой Telegram API.

Проверки не подтверждают доступность конкретных товаров, актуальные цены, доступ к аккаунту, реальные списания и фактическую доставку. Аккаунт GreenGamePay для этой разработки не предоставлялся.
