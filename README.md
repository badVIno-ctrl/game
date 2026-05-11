# FPS ARENA

Онлайн PvP-арена + игра с ботами. Backend: FastAPI + WebSocket.

## Запуск локально

```powershell
# 1. Установить зависимости (Python 3.10+)
pip install -r requirements.txt

# 2. Запустить сервер
python main.py
```

Сервер поднимется на `http://0.0.0.0:8000`.

- На своей машине заходи: `http://localhost:8000`
- С другого устройства в ТОЙ ЖЕ Wi-Fi сети: `http://<IP_твоего_ПК>:8000`
  (узнать IP: `ipconfig` → IPv4-адрес, например `192.168.1.42`)
- Если Windows блокирует — разреши Python в Брандмауэре (всплывёт окно при первом запуске).



## Структура проекта

- `main.py` — FastAPI сервер + WebSocket лобби/игровые комнаты
- `index.html` — главное меню (выбор режима, лобби PvP)
- `game.html` — сама игра
- `requirements.txt`, `Procfile` — для деплоя
