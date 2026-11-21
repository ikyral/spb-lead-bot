# Развёртывание на сервере с нуля

Файлы в этой папке: копии того, что реально стоит на рабочем сервере (Ubuntu, systemd). Секретов, IP-адресов и прокси в них нет. Значения `<...>` подставляете сами.

| Файл в репозитории | Куда на сервере |
|---|---|
| `spb-lead-bot.service` | `/etc/systemd/system/spb-lead-bot.service` |
| `spb-lead-bot.service.d/override.conf` | `/etc/systemd/system/spb-lead-bot.service.d/override.conf` |
| `spb-lead-bot-update.service` | `/etc/systemd/system/spb-lead-bot-update.service` |
| `spb-lead-bot-update.timer` | `/etc/systemd/system/spb-lead-bot-update.timer` |
| `update.sh` (то же, что `spb-lead-bot-update.sh`) | `/opt/spb-lead-bot-update.sh` (вне репозитория, владелец `leadbot`, права 750) |
| `spb-lead-bot-backup.service` | `/etc/systemd/system/spb-lead-bot-backup.service` |
| `spb-lead-bot-backup.timer` | `/etc/systemd/system/spb-lead-bot-backup.timer` |
| `backup-db.sh` | `/opt/spb-lead-bot-backup.sh` (вне репозитория, владелец `leadbot`, права 750) |
| `journald-spb-lead-bot.conf` | `/etc/systemd/journald.conf.d/size.conf` |
| `sshd-10-hardening.conf` | `/etc/ssh/sshd_config.d/10-hardening.conf` |

Скрипт обновления запускается из `/opt/spb-lead-bot-update.sh`, а не из папки репозитория. Файл `deploy/spb-lead-bot-update.sh` остаётся на месте: на него ссылаются тесты и основной README. Не переименовывайте и не удаляйте его.

Ресурсы рабочего сервера: около 1 ГБ ОЗУ и 1 ГБ swap. Поэтому в `override.conf` стоят `MemoryHigh=700M` и `MemoryMax=850M` (Chrome под Xvfb самый тяжёлый процесс).

## 1. Пользователь и код

Под root:

    adduser --system --group --home /opt/spb-lead-bot leadbot
    apt update && apt install -y git sqlite3 xvfb python3-venv
    sudo -u leadbot git clone <REPO_URL> /opt/spb-lead-bot

Пользователю `leadbot` нужно право перезапускать сервис без пароля (его использует скрипт обновления). Файл `/etc/sudoers.d/leadbot` (создавать через `visudo -f /etc/sudoers.d/leadbot`):

    leadbot ALL=(root) NOPASSWD: /usr/bin/systemctl restart spb-lead-bot

## 2. Окружение Python

    cd /opt/spb-lead-bot
    sudo -u leadbot python3 -m venv venv
    sudo -u leadbot venv/bin/pip install -r requirements.lock

`requirements.lock` держит точные версии, которые проверяет CI. Скрипт обновления при изменении `requirements.txt` ставит пакеты из `requirements.txt`.

## 3. Google Chrome и Xvfb

Основной источник `2gis_cdp` управляет настоящим Chrome. Экрана на сервере нет, поэтому Chrome идёт под виртуальным экраном Xvfb (его запускает `xvfb-run` из unit-файла).

    wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt install -y /tmp/chrome.deb
    google-chrome --version

## 4. Настройки `.env`

    cd /opt/spb-lead-bot
    sudo -u leadbot cp .env.example .env
    chmod 600 .env
    chown leadbot:leadbot .env

Обязательные переменные (только имена, значения вписываете в `.env` сами):

- `BOT_TOKEN`
- `ADMIN_TG_ID`

Для работы `2gis_cdp` на сервере нужны ещё:

- `COLLECT_PROVIDERS` (например `2gis_cdp`)
- `SCRAPER_CDP_CHROME_PATH` (путь к Chrome, обычно `/usr/bin/google-chrome`)
- `TWOGIS_PROXY` (прокси с российским IP; без него страна IP проверяется, и при нерусском IP сбор останавливается)

Остальное (расписание `COLLECT_CRON`, лимиты `SCRAPER_CDP_*`, `LEADS_*`) описано в комментариях `.env.example` и в основном README. Файл `.env` не коммитьте и не копируйте в чаты.

## 5. Сервис

Под root:

    cp deploy/spb-lead-bot.service /etc/systemd/system/spb-lead-bot.service
    mkdir -p /etc/systemd/system/spb-lead-bot.service.d
    cp deploy/spb-lead-bot.service.d/override.conf /etc/systemd/system/spb-lead-bot.service.d/override.conf
    systemctl daemon-reload
    systemctl enable --now spb-lead-bot

Таблицы и новые колонки БД создаются при старте сами. Файл базы `leads.db` лежит в `/opt/spb-lead-bot`.

## 6. Автообновление из main

    install -o leadbot -g leadbot -m 750 deploy/update.sh /opt/spb-lead-bot-update.sh
    cp deploy/spb-lead-bot-update.service /etc/systemd/system/
    cp deploy/spb-lead-bot-update.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now spb-lead-bot-update.timer

Раз в 5 минут скрипт делает `git fetch`, обновляет код через `git merge --ff-only`, при изменении `requirements.txt` ставит пакеты, перезапускает сервис и через 15 секунд проверяет, что он живой. Если нет, откатывает код на прежний коммит. Пока идёт сбор (`collector_state`), обновление откладывается.

**Всё, что попало в `main`, через несколько минут оказывается на сервере.** Перед рискованной веткой (схема БД, миграции) сделайте копию базы (раздел 9).

## 7. Журнал (journald)

Ограничивает размер журнала, чтобы он не съел маленький диск:

    mkdir -p /etc/systemd/journald.conf.d
    cp deploy/journald-spb-lead-bot.conf /etc/systemd/journald.conf.d/size.conf
    systemctl restart systemd-journald

## 8. SSH-hardening

Отключает вход по паролю и вход root по паролю.

**Сначала убедитесь, что вход по ключу работает.** Откройте второе окно терминала и войдите по ключу. Первое окно не закрывайте, пока не проверили.

    cp deploy/sshd-10-hardening.conf /etc/ssh/sshd_config.d/10-hardening.conf
    sshd -t
    systemctl reload ssh

Затем во втором окне откройте новое подключение по ключу. Если не получилось, исправьте конфиг из первого окна (удалите файл и сделайте `systemctl reload ssh`).

## 9. Резервные копии `leads.db`

Копия делается средствами SQLite, не простым `cp` (база может быть в момент записи):

    sudo -u leadbot mkdir -p /opt/spb-lead-bot/backups
    sudo -u leadbot sqlite3 /opt/spb-lead-bot/leads.db ".backup /opt/spb-lead-bot/backups/leads_before_<имя>.db"

Эти ручные копии (`backups/leads_before_*.db`) делаете сами перед рискованными изменениями, они **не ротируются** и не удаляются автоматически. Ежедневные копии: раздел 10. Откат: остановите сервис, подставьте копию на место `leads.db`, запустите сервис.

## 10. Ежедневный бэкап

Таймер раз в сутки делает согласованную копию `leads.db` (`sqlite3 .backup`, не `cp`), проверяет её (`PRAGMA integrity_check` должен вернуть `ok`, таблица `leads` читается) и хранит 7 последних копий в `/opt/spb-lead-bot/backups/daily/` (файлы `leads-ГГГГММДД.db`, права 600; при повторном запуске в тот же день имя `leads-ГГГГММДД-ЧЧММСС.db`, существующие файлы не перезаписываются). Ротация удаляет только файлы вида `leads-ГГГГММДД[-ЧЧММСС].db` внутри этой папки и только после успешной копии. При любой ошибке (нет базы, битая база, мало места: нужно свободного места минимум 2 размера базы) старые копии остаются нетронутыми, а владельцу приходит сообщение в Telegram и сервис получает статус failed.

**Время 04:10 по времени сервера (CET).** Плановый сбор стартует в 03/09/15/21 МСК (это 01/07/13/19 по CET) и длится 15-30 минут, поэтому 04:10 в него не попадает (внеочередной сбор при малом пуле редок). `Persistent=true`: если сервер был выключен в 04:10, копия сделается при следующем запуске.

Установка под root (скрипт кладётся вне репозитория, как скрипт обновления, чтобы автообновление не могло незаметно изменить то, что запускает systemd):

    install -o leadbot -g leadbot -m 750 deploy/backup-db.sh /opt/spb-lead-bot-backup.sh
    cp deploy/spb-lead-bot-backup.service /etc/systemd/system/
    cp deploy/spb-lead-bot-backup.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now spb-lead-bot-backup.timer

Запустить один раз вручную и проверить:

    systemctl start spb-lead-bot-backup.service
    journalctl -u spb-lead-bot-backup -n 20 --no-pager
    ls -l /opt/spb-lead-bot/backups/daily
    systemctl list-timers spb-lead-bot-backup.timer

В журнале должна быть строка `backup created: leads-ГГГГММДД.db`, а `systemctl start` завершиться без ошибки.

Безопасная проверка оповещения (боевая база и папка копий не затрагиваются, в Telegram должно прийти сообщение «⚠️ Бэкап leads.db не удался»):

    sudo -u leadbot env DB=/nonexistent /opt/spb-lead-bot-backup.sh; echo "exit=$?"

Здесь токен и ID администратора в окружении не заданы, поэтому оповещение не уйдёт. Чтобы проверить его целиком, временно подставьте в юнит `Environment=DB=/nonexistent` через `systemctl edit spb-lead-bot-backup.service`, выполните `systemctl start spb-lead-bot-backup.service` (будет статус failed и сообщение в Telegram), затем удалите override (`systemctl revert spb-lead-bot-backup.service`).

Токен для оповещения берётся из `/opt/spb-lead-bot/.env` (`BOT_TOKEN`, `ADMIN_TG_ID`) через `EnvironmentFile=`. Формат systemd проще формата `.env`: строки вида `export X=...`, пробелы вокруг `=`, значения с экранированием могут прочитаться иначе. Кавычки вокруг значения systemd снимает сам. Если в `.env` для этих двух переменных есть `export` или пробелы вокруг `=`, оповещение не уйдёт (в журнале будет `alert skipped`), а копия всё равно сделается. Токен передаётся в curl через stdin, в `ps` и журнал не попадает.

Восстановление из копии (под root):

    systemctl stop spb-lead-bot
    sudo -u leadbot cp /opt/spb-lead-bot/backups/daily/leads-<дата>.db /opt/spb-lead-bot/leads.db
    rm -f /opt/spb-lead-bot/leads.db-wal /opt/spb-lead-bot/leads.db-shm
    systemctl start spb-lead-bot

Старые `-wal` и `-shm` обязательно удалите: иначе SQLite применит их к подставленной копии. Перед восстановлением сохраните текущий `leads.db` (например, переименуйте), если он ещё читается.

## 11. Как проверить

    systemctl status spb-lead-bot
    journalctl -u spb-lead-bot -n 50 --no-pager
    systemctl show spb-lead-bot -p MemoryHigh -p MemoryMax
    systemctl list-timers spb-lead-bot-update.timer
    journalctl -u spb-lead-bot-update -n 30 --no-pager

В Telegram: `/collect_status` показывает источники и состояние сбора. В журнале при первом сборе должна быть строка `External IP: ... (RU)`.
