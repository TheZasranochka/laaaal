import os
import time
import uuid
import requests
import numpy as np
from typing import Dict, List, Tuple
from pypdf import PdfReader
from docx import Document
import pdfplumber
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Загружаем .env и здесь — Config читает окружение в момент импорта модуля,
# поэтому переменные должны быть доступны независимо от порядка импортов.
load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


class Config:
    RAG_DOCS_PATH = os.getenv("RAG_DOCS_PATH", "./rag_docs")
    PROMPT_PATH = os.getenv("PROMPT_PATH", "./prompt.txt")
    GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
    # Базовая модель доступна на бесплатном тарифе (GIGACHAT_API_PERS) и расходует
    # меньше токенов, чем Pro/Max. Меняется через env GIGACHAT_MODEL.
    MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")
    CHUNK_SIZE = 1200
    CHUNK_OVERLAP = 200
    TOP_K_RESULTS = 7
    CONTEXT_CHUNKS = 3            # сколько чанков класть в промпт
    MAX_CONTEXT_CHARS = 3500     # суммарный лимит контекста RAG (экономия токенов)
    TEMPERATURE = 0.2
    MAX_TOKENS = 1200            # было 3000 — снижено, чтобы не упираться в лимит контекста
    HISTORY_TURNS = 3            # сколько прошлых пар сообщений помнить
    ENABLE_MINZDRAV = _env_bool("ENABLE_MINZDRAV", False)  # внешний скрейпинг по умолчанию выключен


class GigaChatRESTClient:
    def __init__(self, auth_key: str, model: str = "GigaChat"):
        self.auth_key = auth_key
        self.model = model
        self.access_token = None
        self.token_expires_at = None
        self.base_url = "https://gigachat.devices.sberbank.ru/api/v1"
        self.oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"

    def _get_access_token(self) -> str | None:
        if not self.auth_key:
            print("[GigaChat] GIGACHAT_AUTH_KEY не задан.")
            return None
        rquid = str(uuid.uuid4())
        auth_header = self.auth_key if self.auth_key.startswith('Basic ') else f'Basic {self.auth_key}'
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'RqUID': rquid,
            'Authorization': auth_header
        }
        try:
            # В продакшене лучше загрузить сертификат Сбера и убрать verify=False
            response = requests.post(self.oauth_url, headers=headers,
                                     data='scope=GIGACHAT_API_PERS', verify=False, timeout=15)
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get('access_token')
                # expires_at у Сбера приходит в миллисекундах
                exp = data.get('expires_at')
                self.token_expires_at = (exp / 1000) if exp else (time.time() + 1700)
                return self.access_token
            print(f"[GigaChat] OAuth {response.status_code}: {response.text[:300]}")
        except Exception as e:
            print(f"[GigaChat] Ошибка токена: {e}")
        return None

    def _ensure_token(self) -> bool:
        if self.access_token is None or time.time() >= (self.token_expires_at or 0):
            return self._get_access_token() is not None
        return True

    def chat(self, messages: List[dict], temperature: float = 0.2,
             max_tokens: int = 1200, _retry: bool = True) -> dict:
        """Возвращает {'ok': bool, 'text': str, 'error': str|None}."""
        if not self._ensure_token():
            return {"ok": False, "text": "Не удалось авторизоваться в GigaChat. Проверьте GIGACHAT_AUTH_KEY.",
                    "error": "auth"}

        headers = {'Content-Type': 'application/json',
                   'Authorization': f'Bearer {self.access_token}'}
        payload = {
            'model': self.model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'stream': False,
        }
        try:
            resp = requests.post(f"{self.base_url}/chat/completions", headers=headers,
                                 json=payload, verify=False, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "text": data['choices'][0]['message']['content'], "error": None}

            body = resp.text[:500]
            print(f"[GigaChat] chat {resp.status_code}: {body}")

            if resp.status_code == 401 and _retry:
                self.access_token = None
                return self.chat(messages, temperature, max_tokens, _retry=False)

            # Закончились токены / лимит тарифа
            if resp.status_code in (402, 429) or 'token' in body.lower() or 'quota' in body.lower() or 'лимит' in body.lower():
                return {"ok": False,
                        "text": "На стороне GigaChat закончились доступные токены или превышен лимит запросов. "
                                "Пополните баланс токенов в личном кабинете GigaChat и попробуйте снова.",
                        "error": "quota"}
            # Слишком длинный контекст
            if resp.status_code == 400 and ('context' in body.lower() or 'length' in body.lower() or 'token' in body.lower()):
                return {"ok": False,
                        "text": "Запрос получился слишком длинным для модели. Попробуйте переформулировать короче.",
                        "error": "context"}

            return {"ok": False, "text": f"Сервис GigaChat временно недоступен (код {resp.status_code}).",
                    "error": f"http_{resp.status_code}"}
        except Exception as e:
            print(f"[GigaChat] Ошибка запроса: {e}")
            return {"ok": False, "text": "Ошибка обращения к GigaChat. Попробуйте позже.", "error": "exception"}


class RAGProcessor:
    def __init__(self, config):
        self.config = config
        self.documents, self.metadatas = [], []
        self.vectorizer, self.tfidf_matrix = None, None

    def extract_text_from_pdf(self, path: str) -> str:
        text = ""
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
            if text.strip():
                return text
        except Exception:
            pass
        try:
            for page in PdfReader(path).pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
        except Exception:
            pass
        return text

    def extract_text(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext == '.pdf':
            return self.extract_text_from_pdf(path)
        if ext == '.docx':
            doc = Document(path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if ext in ['.txt', '.md']:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        return ""

    def _pick_files(self, folder: str) -> List[str]:
        """Берём по одному файлу на документ: если есть и .docx, и .pdf с одним именем —
        предпочитаем .docx (чище извлекается текст). Так в индексе нет дублей."""
        by_name: Dict[str, str] = {}
        priority = {'.docx': 0, '.txt': 1, '.md': 1, '.pdf': 2}
        for root, _, files in os.walk(folder):
            for f in files:
                if f.startswith('.'):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext not in priority:
                    continue
                key = os.path.join(root, os.path.splitext(f)[0]).lower()
                cur = by_name.get(key)
                if cur is None or priority[ext] < priority[os.path.splitext(cur)[1].lower()]:
                    by_name[key] = os.path.join(root, f)
        return sorted(by_name.values())

    def load_documents(self, folder: str) -> bool:
        if not os.path.exists(folder):
            return False
        print(f"Индексация документов из {folder}...")
        files = self._pick_files(folder)

        step = max(1, self.config.CHUNK_SIZE - self.config.CHUNK_OVERLAP)
        for fp in files:
            text = " ".join(self.extract_text(fp).split())  # нормализуем пробелы
            if len(text) > 100:
                for i in range(0, len(text), step):
                    chunk = text[i:i + self.config.CHUNK_SIZE]
                    if len(chunk) > 100:
                        self.documents.append(chunk)
                        self.metadatas.append({"source": os.path.basename(fp)})

        if self.documents:
            self.vectorizer = TfidfVectorizer(max_features=10000, min_df=1, max_df=0.95, ngram_range=(1, 2))
            self.tfidf_matrix = self.vectorizer.fit_transform(self.documents)
            print(f"Загружено {len(self.documents)} чанков из {len(files)} документов.")
            return True
        return False

    def search(self, query: str, top_k: int = None) -> Tuple[List[str], List[dict]]:
        if not self.documents:
            return [], []
        top_k = top_k or self.config.TOP_K_RESULTS
        try:
            q_vec = self.vectorizer.transform([query])
            sims = cosine_similarity(q_vec, self.tfidf_matrix).flatten()
            idxs = np.argsort(sims)[-top_k:][::-1]
            docs, metas = [], []
            for i in idxs:
                if sims[i] > 0.05:
                    docs.append(self.documents[i])
                    metas.append(self.metadatas[i])
            return docs, metas
        except Exception:
            return [], []


class MinzdravSearcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
        self.base = "https://minzdrav.gov.ru"

    def search(self, q: str) -> dict:
        res = {"found": False, "content": "", "sources": []}
        try:
            r = self.session.get(f"{self.base}/search", params={'q': q, 'type': 'articles'}, timeout=8)
            soup = BeautifulSoup(r.text, 'lxml')
            for a in soup.find_all('a', href=True):
                title = a.get_text(strip=True)
                if len(title) > 10 and not title.startswith('Бессмертный'):
                    href = a['href'] if a['href'].startswith('http') else self.base + a['href']
                    res['found'] = True
                    res['content'] += f"\n**{title}**\n"
                    res['sources'].append({"title": title, "url": href})
                    if len(res['sources']) >= 3:
                        break
        except Exception:
            pass
        return res


# Ключевые слова, по которым вопрос считается медицинским / про покрытие —
# в таких случаях ассистент сначала уточняет диагноз.
_MEDICAL_HINTS = (
    "лечен", "болезн", "болит", "боль", "симптом", "диагноз", "операц", "госпитал",
    "анализ", "обследов", "врач", "приём", "прием", "стоматолог", "зуб", "покрыва",
    "покрытие", "входит", "положен", "страхов", "направлен", "льгот", "услуг", "клиник",
    "лекарств", "терап", "хирург", "узи", "мрт", "кт ", "рентген", "беремен", "роды",
    "реабилит", "физиотерап", "невролог", "кардиолог", "онколог", "диспансер", "скрининг",
    "вакцин", "прививк", "covid", "ковид", "травм", "перелом",
)
_DIAGNOSIS_SKIP = ("не знаю", "нет диагноза", "без диагноза", "не поставлен",
                   "не указан", "пропустить", "не хочу", "затрудняюсь")
_RESET_HINTS = ("сменить диагноз", "другой диагноз", "новый диагноз", "сбросить диагноз")
_GREETINGS = ("привет", "здравств", "добрый день", "добрый вечер", "доброе утро", "hello", "hi ")


class MedicalAssistant:
    def __init__(self):
        self.config = Config()
        self.rag = RAGProcessor(self.config)
        self.giga = GigaChatRESTClient(self.config.GIGACHAT_AUTH_KEY, self.config.MODEL)
        self.minzdrav = MinzdravSearcher()
        self.sessions: Dict[str, dict] = {}
        self.prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        if os.path.exists(self.config.PROMPT_PATH):
            with open(self.config.PROMPT_PATH, 'r', encoding='utf-8') as f:
                return f.read()
        return "Ты - эксперт по ДМС. Отвечай чётко, ссылайся на источники. Если данных нет - честно скажи."

    def get_session(self, sid: str) -> dict:
        if sid not in self.sessions:
            self.sessions[sid] = {"history": [], "diagnosis": None,
                                  "awaiting_diagnosis": False, "pending_question": None}
        return self.sessions[sid]

    def reset_session(self, sid: str):
        self.sessions[sid] = {"history": [], "diagnosis": None,
                              "awaiting_diagnosis": False, "pending_question": None}

    # ---------- эвристики диагноза ----------
    @staticmethod
    def _needs_diagnosis(text: str) -> bool:
        t = text.lower()
        if any(g in t for g in _GREETINGS) and len(t) < 30:
            return False
        return any(h in t for h in _MEDICAL_HINTS)

    @staticmethod
    def _is_reset(text: str) -> bool:
        t = text.lower()
        return any(h in t for h in _RESET_HINTS)

    @staticmethod
    def _is_skip_diagnosis(text: str) -> bool:
        t = text.lower()
        return any(h in t for h in _DIAGNOSIS_SKIP)

    # ---------- сборка запроса к модели ----------
    def _build_messages(self, sess: dict, question: str, ctx: str, mz_ctx: str) -> List[dict]:
        messages = [{"role": "system", "content": self.prompt}]

        # короткая память диалога
        for turn in sess["history"][-self.config.HISTORY_TURNS:]:
            messages.append({"role": "user", "content": turn["q"][:600]})
            messages.append({"role": "assistant", "content": turn["a"][:800]})

        diag = sess.get("diagnosis")
        parts = []
        if diag and diag != "не указан":
            parts.append(f"=== ДИАГНОЗ / ЖАЛОБА КЛИЕНТА ===\n{diag}")
        parts.append(f"=== ВОПРОС ===\n{question}")
        parts.append(f"=== ДОКУМЕНТЫ ДМС ===\n{ctx if ctx else 'Нет данных в документах.'}")
        if mz_ctx:
            parts.append(f"=== МИНЗДРАВ ===\n{mz_ctx}")
        parts.append("=== ЗАДАЧА ===\nДай точный ответ на основе источников и с учётом диагноза клиента (если указан).")
        messages.append({"role": "user", "content": "\n\n".join(parts)})
        return messages

    def _gate(self, sess: dict, text: str) -> dict:
        """Ответ-уточнение (просим диагноз). Это валидный ход диалога, его можно сохранять."""
        sess["history"].append({"q": sess.get("pending_question") or "", "a": text})
        return {"answer": text, "sources": [], "ok": True,
                "kind": "gate", "diagnosis": sess.get("diagnosis")}

    def ask(self, sid: str, question: str) -> dict:
        sess = self.get_session(sid)
        q = (question or "").strip()
        if not q:
            return {"answer": "Пустой вопрос.", "sources": [], "ok": False, "kind": "error", "diagnosis": None}

        # запрос на смену диагноза
        if self._is_reset(q):
            sess["diagnosis"] = None
            sess["awaiting_diagnosis"] = True
            sess["pending_question"] = None
            return self._gate(sess, "Хорошо. Укажите, пожалуйста, новый диагноз или жалобу, "
                                    "и я продолжу консультацию с учётом него.")

        # пользователь отвечает на запрос диагноза
        if sess.get("awaiting_diagnosis"):
            sess["awaiting_diagnosis"] = False
            sess["diagnosis"] = "не указан" if self._is_skip_diagnosis(q) else q
            question = sess.get("pending_question") or \
                "Что покрывает моя программа ДМС с учётом указанного диагноза?"
            sess["pending_question"] = None
            q = question  # отвечаем на исходный вопрос
        # требуем диагноз, если он ещё не известен и вопрос — медицинский
        elif not sess.get("diagnosis") and self._needs_diagnosis(q):
            sess["awaiting_diagnosis"] = True
            sess["pending_question"] = q
            return self._gate(
                sess,
                "Чтобы корректно проверить покрытие по вашей программе ДМС, уточните, пожалуйста, "
                "ваш диагноз или основную жалобу (например: «гипертония», «боль в спине», "
                "«плановая операция на колене»).\n\n"
                "Если диагноза пока нет — напишите «не знаю», и я дам общую информацию."
            )

        # ---- обычный ответ через RAG + GigaChat ----
        rag_docs, rag_meta = self.rag.search(q)
        ctx, total = "", 0
        for d in rag_docs[:self.config.CONTEXT_CHUNKS]:
            if total + len(d) > self.config.MAX_CONTEXT_CHARS:
                d = d[: max(0, self.config.MAX_CONTEXT_CHARS - total)]
            if d:
                ctx += (d + "\n\n")
                total += len(d)
            if total >= self.config.MAX_CONTEXT_CHARS:
                break

        mz_ctx = ""
        mz_sources = []
        if self.config.ENABLE_MINZDRAV:
            mz = self.minzdrav.search(q)
            if mz["found"]:
                mz_ctx = mz["content"]
                mz_sources = mz["sources"]

        messages = self._build_messages(sess, q, ctx.strip(), mz_ctx)
        result = self.giga.chat(messages, self.config.TEMPERATURE, self.config.MAX_TOKENS)
        ans = result["text"]

        # в историю кладём только удачные ответы
        if result["ok"]:
            sess["history"].append({"q": q, "a": ans})
            sess["history"] = sess["history"][-(self.config.HISTORY_TURNS * 2):]

        sources = [m['source'] for m in rag_meta[:2]] + [s['title'] for s in mz_sources[:2]]
        return {"answer": ans, "sources": list(dict.fromkeys(sources)),
                "ok": result["ok"], "kind": "answer" if result["ok"] else "error",
                "diagnosis": sess.get("diagnosis")}
