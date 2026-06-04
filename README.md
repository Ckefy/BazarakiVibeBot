# Bazaraki Telegram Bot

Телеграм-бот, который следит за поиском на [bazaraki.com](https://bazaraki.com) и
присылает ссылки на **новые** объявления.

- Пришли боту ссылку на поиск (с любыми фильтрами — район, цена, число спален) →
  он подпишет тебя на **30 дней**.
- Раз в ~15 минут бот проверяет каждую активную подписку и шлёт ссылки на новое.
- Кнопки под подтверждением: **🔄 Продлить +30 дней** и **❌ Отменить**.
- Работает в облаке через **GitHub Actions** — даже когда твой компьютер выключен.
  Сервер не нужен, всё бесплатно.

## Как это устроено

GitHub Actions по расписанию (`cron`) запускает `bot.py`. За один запуск бот:

1. забирает новые сообщения и нажатия кнопок (`getUpdates`);
2. опрашивает активные подписки и шлёт ссылки на новые объявления;
3. обрабатывает истечение 30-дневного срока;
4. сохраняет состояние в `state.json`, который workflow коммитит обратно в репозиторий.

Bazaraki закрыт Cloudflare-челленджем, поэтому страница забирается через
`cloudscraper` (он решает JS-проверку).

---

## Установка (≈10 минут)

### 1. Создай бота и получи токен
1. Открой в Telegram [@BotFather](https://t.me/BotFather).
2. Отправь `/newbot`, придумай имя и username (должен заканчиваться на `bot`).
3. BotFather пришлёт **токен** вида `123456789:AAE...`. Сохрани его.

### 2. Залей проект на GitHub
Создай **приватный** репозиторий (в `state.json` хранятся id чатов) и запушь туда эти файлы:

```bash
cd bazaraki-bot
git init
git add .
git commit -m "Initial bazaraki bot"
git branch -M main
git remote add origin git@github.com:<твой-логин>/bazaraki-bot.git
git push -u origin main
```

### 3. Добавь токен в секреты репозитория
GitHub → твой репозиторий → **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `TELEGRAM_BOT_TOKEN`
- Secret: токен от BotFather

(Опционально: `SCRAPER_API_KEY` — см. раздел про Cloudflare ниже.)

### 4. Включи Actions
GitHub → вкладка **Actions** → если просит — нажми «I understand my workflows, enable them».
Workflow `bazaraki-bot` запускается каждые 15 минут. Можно запустить вручную:
**Actions → bazaraki-bot → Run workflow**.

> ⚠️ Дай боту права на запись: **Settings → Actions → General → Workflow permissions →
> Read and write permissions** (нужно, чтобы коммитить `state.json` обратно).

### 5. Пользуйся
1. Найди своего бота в Telegram по username, нажми **Start**.
2. Пришли ссылку на поиск, например:
   ```
   https://bazaraki.com/real-estate-to-rent/apartments-flats/number-of-bedrooms---3/?ordering=newest&price_max=2000
   ```
3. Бот подтвердит подписку. Дальше будет сам присылать новые объявления.
4. `/list` — посмотреть подписки, кнопки — продлить/отменить.

---

## Важные нюансы

- **Задержка.** GitHub Actions запускает cron не точно по минутам и при высокой
  нагрузке может задерживать запуски на несколько минут. Поэтому реакция на кнопки
  и новые объявления — не мгновенная (обычно в пределах 15–25 минут). Хочешь чаще —
  поменяй `*/15` на `*/10` или `*/5` в `.github/workflows/bot.yml`.

- **Cloudflare и IP дата-центра.** `cloudscraper` пробивает челлендж с «домашних»
  IP, но IP-адреса GitHub Actions — дата-центровые, и Cloudflare их блокирует (`403`).
  Поэтому на Actions нужно ходить через scraping-сервис: задай секрет `SCRAPER_API_KEY`,
  и бот начнёт ходить через сервис вместо cloudscraper. Сервис выбирается переменной
  `SCRAPER_PROVIDER`:

  | `SCRAPER_PROVIDER` | Сервис | Доп. переменные |
  |--------------------|--------|-----------------|
  | `scrapingant` (по умолчанию) | [ScrapingAnt](https://scrapingant.com) | `SCRAPER_PROXY_TYPE` (по умолч. `residential`) |
  | `scraperapi` | [ScraperAPI](https://scraperapi.com) | — |
  | `scrapingbee` | [ScrapingBee](https://scrapingbee.com) | — |
  | `custom` | любой | `SCRAPER_URL_TEMPLATE` с `{key}` и `{url}` |

  Перед тем как полагаться на Actions, проверь ключ локально:
  ```bash
  export SCRAPER_API_KEY="..." SCRAPER_PROVIDER="scrapingant"
  python check_service.py
  ```
  Должно напечатать `OK — parsed N listings`.

  ⚠️ Обход Cloudflare тратит много кредитов, бесплатных тарифов хватает на нечастый
  опрос. Если кредиты кончаются — увеличь интервал в `bot.yml` (например, `0 * * * *`
  — раз в час). Расход смотри в дашборде сервиса.

---

## Запуск локально (для теста или вместо GitHub Actions)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123456789:AAE..."
python bot.py            # один проход
```

Чтобы крутить постоянно на своём всегда-включённом устройстве (мини-ПК, Raspberry Pi),
запускай по cron, например каждые 10 минут:

```cron
*/10 * * * * cd /path/to/bazaraki-bot && /path/to/.venv/bin/python bot.py >> bot.log 2>&1
```

---

## Файлы

| Файл | Назначение |
|------|------------|
| `bot.py` | основная логика: команды, подписки, опрос, отправка |
| `scraper.py` | загрузка страницы Bazaraki (cloudscraper / scraping-API) и парсинг ссылок |
| `storage.py` | чтение/запись `state.json` |
| `state.json` | состояние: подписки, «что уже видели», offset Telegram |
| `.github/workflows/bot.yml` | cron-запуск в GitHub Actions + коммит состояния |
