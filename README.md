# FPS ARENA

Онлайн PvP-арена + игра с ботами. Backend: FastAPI + WebSocket.

## Запуск локально

```powershell
# 1. Установить зависимости (Python 3.10+)
pip install -r requirements.txt

# 2. Запустить сервер
python main.py
```


## Структура проекта

- `main.py` — FastAPI сервер + WebSocket лобби/игровые комнаты
- `index.html` — главное меню (выбор режима, лобби PvP)
- `game.html` — сама игра
- `requirements.txt`, `Procfile` — для деплоя
