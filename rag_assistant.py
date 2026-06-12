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

class Config:
    RAG_DOCS_PATH = os.getenv("RAG_DOCS_PATH", "./rag_docs")
    PROMPT_PATH = os.getenv("PROMPT_PATH", "./prompt.txt")
    GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
    CHUNK_SIZE = 1500
    CHUNK_OVERLAP = 300
    TOP_K_RESULTS = 7
    TEMPERATURE = 0.2
    MAX_TOKENS = 3000

class GigaChatRESTClient:
    def __init__(self, auth_key: str):
        self.auth_key = auth_key
        self.access_token = None
        self.token_expires_at = None
        self.base_url = "https://gigachat.devices.sberbank.ru/api/v1"
        self.oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"

    def _get_access_token(self) -> str | None:
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
                self.token_expires_at = data.get('expires_at', time.time() + 1800)
                return self.access_token
        except Exception as e:
            print(f"[GigaChat] Ошибка токена: {e}")
        return None

    def _ensure_token(self) -> bool:
        if self.access_token is None or time.time() >= self.token_expires_at:
            return self._get_access_token() is not None
        return True

    def chat(self, message: str, temperature: float = 0.7, max_tokens: int = 2000) -> str:
        if not self._ensure_token():
            return "Ошибка авторизации в GigaChat. Проверьте ключ."
        
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {self.access_token}'}
        payload = {
            'model': 'GigaChat-Pro',
            'messages': [{'role': 'user', 'content': message}],
            'temperature': temperature, 'max_tokens': max_tokens, 'stream': False
        }
        try:
            resp = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, verify=False, timeout=60)
            if resp.status_code == 200:
                return resp.json()['choices'][0]['message']['content']
            elif resp.status_code == 401:
                self.access_token = None
                return self.chat(message, temperature, max_tokens)
            return f"Ошибка GigaChat: {resp.status_code}"
        except Exception as e:
            return f"Ошибка запроса: {e}"

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
                    if t: text += t + "\n"
            if text.strip(): return text
        except: pass
        try:
            for page in PdfReader(path).pages:
                t = page.extract_text()
                if t: text += t + "\n"
        except: pass
        return text

    def extract_text(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext == '.pdf': return self.extract_text_from_pdf(path)
        if ext == '.docx':
            doc = Document(path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if ext in ['.txt', '.md']:
            with open(path, 'r', encoding='utf-8') as f: return f.read()
        return ""

    def load_documents(self, folder: str) -> bool:
        if not os.path.exists(folder): return False
        print(f"Индексация документов из {folder}...")
        files = [os.path.join(r, f) for r, _, fs in os.walk(folder) for f in fs 
                 if not f.startswith('.') and f.lower().endswith(('.pdf','.docx','.txt','.md'))]
        
        for fp in files:
            text = self.extract_text(fp)
            if len(text.strip()) > 100:
                for i in range(0, len(text), self.config.CHUNK_SIZE - self.config.CHUNK_OVERLAP):
                    chunk = text[i:i + self.config.CHUNK_SIZE]
                    if len(chunk) > 100:
                        self.documents.append(chunk)
                        self.metadatas.append({"source": os.path.basename(fp)})
                        
        if self.documents:
            self.vectorizer = TfidfVectorizer(max_features=10000, min_df=1, max_df=0.95, ngram_range=(1, 2))
            self.tfidf_matrix = self.vectorizer.fit_transform(self.documents)
            print(f"Загружено {len(self.documents)} чанков.")
            return True
        return False

    def search(self, query: str, top_k: int = None) -> Tuple[List[str], List[dict]]:
        if not self.documents: return [], []
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
        except: return [], []

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
                    if len(res['sources']) >= 3: break
        except: pass
        return res

class MedicalAssistant:
    def __init__(self):
        self.config = Config()
        self.rag = RAGProcessor(self.config)
        self.giga = GigaChatRESTClient(self.config.GIGACHAT_AUTH_KEY)
        self.minzdrav = MinzdravSearcher()
        self.sessions = {}
        self.prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        if os.path.exists(self.config.PROMPT_PATH):
            with open(self.config.PROMPT_PATH, 'r', encoding='utf-8') as f: return f.read()
        return "Ты - эксперт по ДМС. Отвечай четко, ссылайся на источники. Если данных нет - честно скажи."

    def get_session(self, sid: str) -> dict:
        if sid not in self.sessions:
            self.sessions[sid] = {"history": []}
        return self.sessions[sid]

    def ask(self, sid: str, question: str) -> dict:
        rag_docs, rag_meta = self.rag.search(question)
        ctx = "\n\n".join(rag_docs[:3]) if rag_docs else "Нет данных в документах."
        
        mz = self.minzdrav.search(question)
        mz_ctx = mz["content"] if mz["found"] else ""

        full_prompt = f"""{self.prompt}
=== ВОПРОС ===
{question}

=== ДОКУМЕНТЫ ДМС ===
{ctx}

=== МИНЗДРАВ ===
{mz_ctx}

=== ЗАДАЧА ===
Дай точный ответ на основе источников."""

        ans = self.giga.chat(full_prompt, self.config.TEMPERATURE, self.config.MAX_TOKENS)
        
        sources = [m['source'] for m in rag_meta[:2]] + [s['title'] for s in mz["sources"][:2]]
        return {"answer": ans, "sources": list(set(sources)), "status": "ok"}