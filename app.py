import os
import uuid
from dotenv import load_dotenv

# ВАЖНО: загрузить .env ДО импорта rag_assistant — его класс Config читает
# переменные окружения (GIGACHAT_AUTH_KEY и др.) в момент импорта.
load_dotenv()

from flask import Flask, render_template, request, jsonify, session
from flask import redirect, url_for, flash
from flask_migrate import Migrate
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from extensions import db
from rag_assistant import MedicalAssistant

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-prod")

app.config.from_object('config.Config')
db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# Модели импортируем после init_app. db живёт в extensions.py (см. там про
# круговой импорт). Импорт здесь регистрирует таблицы перед db.create_all().
from models import User, Dialog, MedicalTest  # noqa: E402

# Создаём таблицы при старте — работает и под gunicorn, а не только при `python app.py`.
with app.app_context():
    db.create_all()

# Инициализация AI при старте
assistant = None
def init_ai():
    global assistant
    print("Загрузка AI-ассистента...")
    assistant = MedicalAssistant()
    if not assistant.rag.load_documents(assistant.config.RAG_DOCS_PATH):
        print("RAG база пуста. Ассистент будет отвечать без контекста.")
    print("AI готов.")

init_ai()

@app.route("/")
def index(): return render_template("index.html")

@app.route("/chat")
def chat_page():
    if "chat_id" not in session:
        session["chat_id"] = str(uuid.uuid4())
    return render_template("chat.html")

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@app.route("/api/chat", methods=["POST"])
def api_chat():
    cid = session.get("chat_id")
    if not cid: return jsonify({"error": "Сессия не найдена"}), 400
    
    data = request.get_json()
    msg = data.get("message", "").strip()
    if not msg: return jsonify({"error": "Пустое сообщение"}), 400

    if assistant is None:
        return jsonify({"error": "Сервис загружается. Подождите..."}), 503

    try:
        res = assistant.ask(cid, msg)

        # Сохраняем удачные ходы диалога в БД (ошибки модели не пишем).
        if res.get("ok"):
            try:
                dlg = Dialog(
                    user_id=current_user.id if current_user.is_authenticated else None,
                    chat_id=cid,
                    diagnosis=res.get("diagnosis"),
                    message=msg,
                    response=res["answer"],
                )
                db.session.add(dlg)
                db.session.commit()
            except Exception as db_err:
                db.session.rollback()
                print(f"/api/chat DB save error: {db_err}")

        return jsonify({"response": res["answer"], "sources": res["sources"]})
    except Exception as e:
        print(f"/api/chat error: {e}")
        return jsonify({"error": "Ошибка обработки запроса"}), 500

@app.route("/chat/clear", methods=["POST"])
def clear_chat():
    cid = session.get("chat_id")
    if cid and assistant:
        assistant.reset_session(cid)
    return jsonify({"status": "cleared"})

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        if not email or not password:
            flash("Укажите email и пароль.", "error")
            return redirect(url_for("register"))
        if User.query.filter_by(email=email).first():
            flash("Email уже зарегистрирован.", "error")
            return redirect(url_for("register"))
        user = User(name=email.split('@')[0], email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Регистрация успешна! Теперь вы можете войти.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

# Вход пользователя
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("index"))
        flash("Неверный email или пароль.", "error")
    return render_template("login.html")

# Выход пользователя
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))

@app.route("/api/add_test", methods=["POST"])
@login_required
def add_medical_test():
    data = request.get_json()
    test_name = data.get("test_name")
    test_date = data.get("test_date")  # Ожидаем дату в формате 'YYYY-MM-DD'
    results = data.get("results")

    if not all([test_name, test_date]):
        return jsonify({"error": "Пожалуйста, предоставьте имя теста и дату."}), 400

    try:
        user = current_user  
        if user:
            medical_test = MedicalTest(user_id=user.id, test_name=test_name, test_date=test_date, results=results)
            db.session.add(medical_test)
            db.session.commit()
            return jsonify({"status": "success", "message": "Медицинский тест успешно добавлен."})
        else:
            return jsonify({"error": "Пользователь не найден."}), 404
    except Exception as e:
        print(f"Ошибка добавления теста: {e}")
        return jsonify({"error": "Ошибка при добавлении теста."}), 500


@app.route("/api/get_tests", methods=["GET"])
@login_required  # Защита маршрута
def get_medical_tests():
    user = current_user  # Получаем текущего пользователя
    try:
        tests = MedicalTest.query.filter_by(user_id=user.id).all()
        tests_data = [{"test_name": test.test_name, "test_date": test.test_date, "results": test.results} for test in tests]
        return jsonify({"tests": tests_data})
    except Exception as e:
        print(f"Ошибка извлечения тестов: {e}")
        return jsonify({"error": "Ошибка при извлечении тестов."}), 500

@app.route("/api/get_dialog_history", methods=["GET"])
@login_required  # Защита маршрута
def get_dialog_history():
    user = current_user  # Получаем текущего пользователя
    try:
        dialogs = Dialog.query.filter_by(user_id=user.id).order_by(Dialog.timestamp).all()
        history = [{
            "message": d.message,
            "response": d.response,
            "diagnosis": d.diagnosis,
            "timestamp": d.timestamp.isoformat() if d.timestamp else None,
        } for d in dialogs]
        return jsonify({"history": history})
    except Exception as e:
        print(f"Ошибка извлечения истории диалогов: {e}")
        return jsonify({"error": "Ошибка при извлечении истории."}), 500


if __name__ == "__main__":
    with app.app_context():
        db.create_all()  
    # Локальный dev-сервер. На проде запуск через gunicorn (app:app), см. DEPLOY.md.
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")