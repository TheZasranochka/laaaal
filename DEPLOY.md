# Деплой AI ДМС на Linux VPS

**Стек:** Flask + Gunicorn + systemd + nginx. БД — SQLite.

```
Интернет → nginx (80/443) → gunicorn (127.0.0.1:8000, 1 воркер + потоки) → Flask (app:app)
```

> **Почему 1 воркер.** Приложение хранит историю чата в памяти процесса
> (`assistant.sessions`) и грузит RAG-индекс один раз на процесс. Несколько
> воркеров разнесут историю по процессам. Конкурентность даём потоками
> (`gthread`, см. `gunicorn.conf.py`). Чтобы масштабироваться шире — вынести
> сессии в БД/Redis (см. раздел 9).

---

## 0. Перед деплоем (на машине разработки)

⚠️ **Ротируйте `GIGACHAT_AUTH_KEY`.** Он лежал в `.env` открытым текстом, считайте его скомпрометированным: выпустите новый ключ в личном кабинете GigaChat, старый отзовите.

- `.env` и `venv/` на сервер **не** копируются и в git **не** попадают (см. `.gitignore`). На сервере создаётся свежий `.env` из `.env.example`.
- Локальный `venv/` собран под Windows и на Linux нерабочий — на сервере ставим зависимости заново.

---

## 1. Требования сервера

- Ubuntu 22.04/24.04 или Debian 12, доступ по SSH с `sudo`.
- Открытые порты 80 и 443.
- (Опционально) домен, A-запись которого указывает на IP сервера, — нужен для HTTPS.

---

## 2. Доставка кода на сервер

### Вариант А — git (рекомендуется)

На Windows, в папке проекта:
```powershell
git init
git add .
git commit -m "Initial deploy"
# создайте ПРИВАТНЫЙ репозиторий на GitHub/GitLab и:
git remote add origin <URL_РЕПОЗИТОРИЯ>
git push -u origin master
```
`.gitignore` уже исключает `.env`, `venv/` и `*.db`.

На сервере:
```bash
sudo apt update && sudo apt install -y git
sudo git clone <URL_РЕПОЗИТОРИЯ> /opt/ai-dms
```

### Вариант Б — без git (архив + scp)

На Windows:
```powershell
$items = Get-ChildItem -Path . -Exclude venv,.env,*.db
Compress-Archive -Path $items -DestinationPath ai-dms.zip -Force
scp ai-dms.zip USER@SERVER:/tmp/
```
На сервере:
```bash
sudo apt update && sudo apt install -y unzip
sudo mkdir -p /opt/ai-dms
sudo unzip /tmp/ai-dms.zip -d /opt/ai-dms
```

---

## 3. Установка на сервере

```bash
# Системные пакеты
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
# Если pip начнёт собирать lxml/scipy из исходников и упадёт — добавьте:
#   sudo apt install -y build-essential libxml2-dev libxslt1-dev python3-dev

# Системный пользователь без логина
sudo adduser --system --group --no-create-home --home /opt/ai-dms ai-dms
sudo chown -R ai-dms:ai-dms /opt/ai-dms
sudo chmod 755 /opt/ai-dms

# Виртуальное окружение и зависимости (от имени ai-dms)
cd /opt/ai-dms
sudo -u ai-dms python3 -m venv venv
sudo -u ai-dms venv/bin/pip install --upgrade pip
sudo -u ai-dms venv/bin/pip install -r requirements.txt
```

Создайте `.env`:
```bash
sudo -u ai-dms cp .env.example .env
# Сгенерируйте секрет Flask:
python3 -c "import secrets; print(secrets.token_hex(32))"
# Впишите GIGACHAT_AUTH_KEY и FLASK_SECRET_KEY:
sudo -u ai-dms nano /opt/ai-dms/.env
sudo chmod 600 /opt/ai-dms/.env
```

(Опционально) проверка запуска вручную перед systemd:
```bash
sudo -u ai-dms venv/bin/gunicorn -c gunicorn.conf.py app:app
# Ctrl+C для остановки. Должно появиться "Загрузка AI-ассистента... / AI готов."
```

---

## 4. systemd

```bash
sudo cp /opt/ai-dms/deploy/ai-dms.service /etc/systemd/system/ai-dms.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-dms
sudo systemctl status ai-dms          # должно быть active (running)
journalctl -u ai-dms -f               # живые логи
```

---

## 5. nginx

```bash
# nginx (www-data) должен читать статику:
sudo chmod -R a+rX /opt/ai-dms/static

sudo cp /opt/ai-dms/deploy/nginx.conf /etc/nginx/sites-available/ai-dms
# в файле замените server_name на ваш домен (или оставьте _)
sudo nano /etc/nginx/sites-available/ai-dms

sudo ln -s /etc/nginx/sites-available/ai-dms /etc/nginx/sites-enabled/ai-dms
sudo rm -f /etc/nginx/sites-enabled/default   # убрать дефолтную заглушку (опц.)
sudo nginx -t
sudo systemctl reload nginx
```

Проверка: открыть `http://SERVER_IP/` — должна отдаться главная страница.

---

## 6. HTTPS (опционально, нужен домен)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example.com
```
Certbot сам пропишет 443 и редирект с 80 и настроит автопродление.

---

## 7. Проверка и логи

```bash
systemctl status ai-dms          # состояние сервиса
journalctl -u ai-dms -f          # логи приложения/gunicorn
sudo tail -f /var/log/nginx/error.log
curl -I http://127.0.0.1:8000/   # ответ gunicorn напрямую
```

---

## 8. Обновление версии

```bash
cd /opt/ai-dms
sudo -u ai-dms git pull                                  # или заново распакуйте архив
sudo -u ai-dms venv/bin/pip install -r requirements.txt  # если менялись зависимости
sudo systemctl restart ai-dms
```

---

## 9. Заметки и дальнейшие шаги

- **Масштабирование > 1 воркера.** Сейчас история чата и RAG-индекс — в памяти
  процесса. Чтобы поднять несколько воркеров, вынесите `assistant.sessions` в
  Redis/БД и шарьте индекс (или примите, что история живёт только в текущем
  процессе).
- **PostgreSQL вместо SQLite.** SQLite ок для небольшой нагрузки. Для прода
  надёжнее Postgres: `apt install postgresql`, добавьте `psycopg[binary]` в
  `requirements.txt`, укажите `DATABASE_URL` в `.env`, перезапустите сервис.
- **Сертификат Сбера для GigaChat.** В `rag_assistant.py` запросы идут с
  `verify=False` — это шумит предупреждениями и небезопасно. В проде поставьте
  корневой сертификат Минцифры/Сбера и включите проверку TLS.
- **Брандмауэр.** `sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable`.
- **Бэкап.** Регулярно сохраняйте `/opt/ai-dms/dms_assistant.db` и `/opt/ai-dms/.env`.
