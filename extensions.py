"""Расширения Flask вынесены сюда, чтобы не было кругового импорта
app <-> models (models импортирует db, app импортирует models)."""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
