# traceX — Форум расследований с Графом Связей

Автор: sck_anonn
Стек: FastAPI + SQLAlchemy + SQLite + Jinja2 + TailwindCSS (CDN) + Vis.js Network (CDN)

## Структура проекта

```
tracex/
├── main.py                # весь backend: модели, API, роуты страниц
├── requirements.txt
├── templates/
│   ├── index.html          # список + глобальный граф связей
│   ├── investigation.html  # карточка дела: таймлайн, улики, комментарии, локальный граф
│   └── create.html         # форма создания дела с конструктором графа/таймлайна
├── static/                 # статика (пусто, зарезервировано)
├── Dockerfile
├── docker-compose.yml
├── tracex.service           # systemd unit для запуска без Docker
└── README.md
```

## Вариант 1: Запуск через Docker (рекомендуется)

Требуется установленный Docker + docker-compose на VPS (Ubuntu 22.04/24.04).

```bash
# 1. Скопируйте папку tracex на сервер, например в /opt/tracex
cd /opt/tracex

# 2. Соберите и запустите одной командой
docker compose up -d --build

# Сайт будет доступен на http://ВАШ_IP:8000
```

База данных SQLite хранится в именованном Docker-volume `tracex_data`,
поэтому переживает пересборку контейнера.

Обновление после изменения кода:
```bash
docker compose up -d --build
```

Логи:
```bash
docker compose logs -f
```

## Вариант 2: Запуск напрямую на Ubuntu VPS через systemd

```bash
# 1. Установите зависимости системы
sudo apt update && sudo apt install -y python3 python3-venv python3-pip

# 2. Создайте пользователя сервиса
sudo useradd --system --create-home --shell /usr/sbin/nologin tracex

# 3. Скопируйте проект в /opt/tracex
sudo mkdir -p /opt/tracex
sudo cp -r ./* /opt/tracex/
sudo mkdir -p /opt/tracex/data
sudo chown -R tracex:tracex /opt/tracex

# 4. Создайте виртуальное окружение и установите зависимости
cd /opt/tracex
sudo -u tracex python3 -m venv venv
sudo -u tracex ./venv/bin/pip install --upgrade pip
sudo -u tracex ./venv/bin/pip install -r requirements.txt

# 5. Установите systemd-сервис
sudo cp tracex.service /etc/systemd/system/tracex.service
sudo systemctl daemon-reload
sudo systemctl enable --now tracex

# 6. Проверьте статус
sudo systemctl status tracex
curl http://localhost:8000/healthz
```

Логи сервиса:
```bash
sudo journalctl -u tracex -f
```

### (Опционально) Reverse proxy с Nginx + HTTPS

```nginx
server {
    listen 80;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
Затем `sudo certbot --nginx -d your-domain.example` для бесплатного SSL (Let's Encrypt).

## Локальная разработка

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Откройте http://localhost:8000

## Функционал

- Главная страница с переключателем "Список" / "Граф связей" (стиль Obsidian, тёмная тема).
- Фильтрация по статусу ("Идёт сбор улик", "Завершено", "Требуется фактчекинг") и категории, поиск по тексту.
- Клик по узлу графа открывает карточку соответствующего расследования.
- Страница дела: заголовок, описание, автор, дата, статус, интерактивный таймлайн, блок улик
  (ссылки/документы/изображения/заметки), блок обсуждения (комментарии), локальный граф связей.
- Форма создания расследования: динамический конструктор объектов графа (фигуранты, организации,
  темы, улики, локации), связей между ними ("Объект 1 → Связь → Объект 2"), таймлайна и улик.
- REST API: `/api/graph`, `/api/investigation/{id}/graph`, `/api/investigations`, `/api/investigations` (POST).

## Замечание об этичности использования

Платформа предназначена для открытой, добросовестной журналистики и гражданских расследований
на основе проверяемых фактов. Рекомендуется дополнительно внедрить (не входит в этот базовый пакет,
но легко достраивается поверх текущей архитектуры):
- модерацию публикаций и комментариев перед публикацией;
- систему аутентификации пользователей и ролей (например, через `fastapi-users` или OAuth);
- политику конфиденциальности и раздел "Как присылать материалы анонимно и безопасно";
- ограничение по загрузке файлов и фильтрацию заведомо незаконного контента.
