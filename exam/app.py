"""
exam/app.py — Flask web app for MCQ practice.

Serves a single-page UI on http://127.0.0.1:5001/.
State is persisted in exam.sqlite (attempts + wrong book).
Question bank is loaded from exam/questions.json at startup.
Knowledge points are auto-tagged at first load by searching the KB.

Endpoints
---------
GET  /                          → single-page HTML
GET  /api/health                → {status, n_questions, n_complete}
GET  /api/question?mode=...     → next question
      mode ∈ {random, wrong, by_doc, by_domain}
      extra: doc=<filename>, n=1
POST /api/answer                → {question_id, user_answer} → {correct, correct_answer, explanation, kb_link}
GET  /api/wrong                 → list of wrong-book question ids
POST /api/mastered              → {question_id} → mark as mastered
GET  /api/stats                 → {total_attempts, correct, accuracy, wrong_count}
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bm25  # noqa: E402
import storage  # noqa: E402

EXAM_DIR = ROOT / "exam"
DB_PATH = EXAM_DIR / "exam.sqlite"
QUESTIONS_PATH = EXAM_DIR / "questions.json"

app = Flask(__name__, template_folder=str(EXAM_DIR / "templates"))


# --- DB ----------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS attempts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id TEXT NOT NULL,
            user_answer TEXT NOT NULL,
            correct     INTEGER NOT NULL,
            ts          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_qid ON attempts(question_id);

        CREATE TABLE IF NOT EXISTS wrong_book (
            question_id TEXT PRIMARY KEY,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_ts     TEXT NOT NULL,
            mastered    INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


# --- Questions ---------------------------------------------------------------

def load_questions() -> list[dict]:
    qs = json.loads(QUESTIONS_PATH.read_text())
    # Lazy-compute knowledge point for any question that doesn't have one.
    # We cache into the file on first run, so subsequent loads are fast.
    needs_kp = [q for q in qs if not q.get("knowledge_point")]
    if needs_kp:
        for q in needs_kp:
            q["knowledge_point"] = annotate_knowledge_point(q)
        # Persist back to disk
        QUESTIONS_PATH.write_text(json.dumps(qs, ensure_ascii=False, indent=2))
    return qs


def annotate_knowledge_point(q: dict) -> str:
    """Use BM25 to find the best-matching domain PDF for this question.
    Returns the doc filename (or "" if no hit)."""
    try:
        text = q["question"]
        # Take first 200 chars to focus on the actual question
        hits = bm25.search(text, top_k=1)
        if hits:
            return hits[0].get("filename", "")
    except Exception:
        pass
    return ""


# Doc → knowledge point label (just maps source PDF to a domain name)
DOC_TO_KP = {
    "综合测试一.pdf": "综合练习",
    "综合测试二.pdf": "综合练习",
    "综合测试三.pdf": "综合练习",
    "综合测试四.pdf": "综合练习",
    "域1：安全与风险管理.pdf": "域1 安全与风险管理",
    "域2：资产安全.pdf": "域2 资产安全",
    "域3：安全架构与工程.pdf": "域3 安全架构与工程",
    "域4：通信与网络安全.pdf": "域4 通信与网络安全",
    "域5：身份与访问管理.pdf": "域5 身份与访问管理",
    "域6：安全评估与测试.pdf": "域6 安全评估与测试",
    "域7：安全运营.pdf": "域7 安全运营",
    "域8：软件开发安全.pdf": "域8 软件开发安全",
}


def quick_knowledge_point(q: dict) -> str:
    """Fast KP label without BM25. The source doc IS the knowledge point
    for 综合测试 (they mix all domains). For 域 docs, it tells you the
    domain. We keep this fast; the BM25 search is reserved for the
    kb_link on answer submit."""
    return DOC_TO_KP.get(q.get("source_doc", ""), q.get("source_doc", ""))


def get_questions_with_kp(questions: list[dict]) -> list[dict]:
    """Add knowledge_point if missing. Cached in-memory."""
    out = []
    for q in questions:
        if "knowledge_point" not in q:
            q["knowledge_point"] = annotate_knowledge_point(q)
        out.append(q)
    return out


# --- Routes ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    qs = load_questions()
    n_complete = sum(1 for q in qs if q.get("correct") and len(q.get("options", [])) >= 2)
    return jsonify({
        "status": "ok",
        "n_questions": len(qs),
        "n_complete": n_complete,
        "n_with_kp": sum(1 for q in qs if q.get("knowledge_point")),
    })


@app.route("/api/question", methods=["GET"])
def get_question():
    qs = load_questions()
    # filter to complete questions (with answer)
    pool = [q for q in qs if q.get("correct") and len(q.get("options", [])) >= 2]
    mode = request.args.get("mode", "random")

    db = get_db()
    if mode == "wrong":
        rows = db.execute(
            "SELECT question_id FROM wrong_book WHERE mastered = 0"
        ).fetchall()
        ids = {r["question_id"] for r in rows}
        pool = [q for q in pool if _qid(q) in ids]

    doc = request.args.get("doc")
    if doc:
        pool = [q for q in pool if q.get("source_doc") == doc]

    if not pool:
        return jsonify({"error": "no questions match"}), 404

    import random
    q = random.choice(pool)
    return jsonify(_present_question(q))


@app.route("/api/answer", methods=["POST"])
def submit_answer():
    data = request.get_json(force=True)
    qid = data.get("question_id")
    user_answer = "".join(sorted(data.get("user_answer", "").upper()))
    if not qid or not user_answer:
        return jsonify({"error": "question_id and user_answer required"}), 400

    qs = load_questions()
    q = next((q for q in qs if _qid(q) == qid), None)
    if q is None:
        return jsonify({"error": "question not found"}), 404

    correct_answer = "".join(sorted(q["correct"]))
    is_correct = user_answer == correct_answer

    db = get_db()
    db.execute(
        "INSERT INTO attempts (question_id, user_answer, correct, ts) VALUES (?, ?, ?, ?)",
        (qid, user_answer, 1 if is_correct else 0, datetime.utcnow().isoformat()),
    )
    if not is_correct:
        # Upsert wrong_book
        row = db.execute(
            "SELECT attempts FROM wrong_book WHERE question_id = ?", (qid,)
        ).fetchone()
        if row:
            db.execute(
                "UPDATE wrong_book SET attempts = attempts + 1, last_ts = ? "
                "WHERE question_id = ?",
                (datetime.utcnow().isoformat(), qid),
            )
        else:
            db.execute(
                "INSERT INTO wrong_book (question_id, attempts, last_ts, mastered) "
                "VALUES (?, 1, ?, 0)",
                (qid, datetime.utcnow().isoformat()),
            )
    else:
        # If a correct answer comes in for a question in wrong_book, leave it
        # (don't auto-master). User explicitly marks mastered.
        pass
    db.commit()

    # KB search link for the explanation
    kb_link = ""
    try:
        hits = bm25.search(q["question"][:200], top_k=1)
        if hits:
            kb_link = f"http://127.0.0.1:5001/kb/{hits[0]['filename']}#page={hits[0].get('page_num', 1)}"
    except Exception:
        pass

    return jsonify({
        "correct": is_correct,
        "correct_answer": q["correct"],
        "explanation": q.get("explanation", ""),
        "knowledge_point": quick_knowledge_point(q),
        "kb_link": kb_link,
        "source_doc": q.get("source_doc", ""),
    })


@app.route("/api/wrong", methods=["GET"])
def list_wrong():
    db = get_db()
    rows = db.execute(
        "SELECT question_id, attempts, last_ts, mastered FROM wrong_book "
        "ORDER BY last_ts DESC"
    ).fetchall()
    return jsonify([
        dict(r) for r in rows
    ])


@app.route("/api/mastered", methods=["POST"])
def mark_mastered():
    data = request.get_json(force=True)
    qid = data.get("question_id")
    if not qid:
        return jsonify({"error": "question_id required"}), 400
    db = get_db()
    db.execute(
        "UPDATE wrong_book SET mastered = 1 WHERE question_id = ?", (qid,)
    )
    db.commit()
    return jsonify({"status": "ok"})


@app.route("/api/stats", methods=["GET"])
def stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    correct = db.execute("SELECT COUNT(*) FROM attempts WHERE correct = 1").fetchone()[0]
    wrong_count = db.execute(
        "SELECT COUNT(*) FROM wrong_book WHERE mastered = 0"
    ).fetchone()[0]
    return jsonify({
        "total_attempts": total,
        "correct": correct,
        "accuracy": (correct / total) if total > 0 else 0.0,
        "wrong_count": wrong_count,
    })


@app.route("/api/question/<qid>")
def get_question_by_id(qid):
    qs = load_questions()
    q = next((q for q in qs if _qid(q) == qid), None)
    if q is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_present_question(q))


# --- helpers -----------------------------------------------------------------

def _qid(q: dict) -> str:
    return f"{q['source_doc']}#{q.get('qnum', 0)}"


def _present_question(q: dict) -> dict:
    """Format a question for the UI (without revealing the answer)."""
    return {
        "id": _qid(q),
        "qnum": q.get("qnum"),
        "source_doc": q.get("source_doc", ""),
        "qtype": q.get("qtype", "single"),
        "question": q.get("question", ""),
        "options": [
            {"letter": o["letter"], "text": o["text"]}
            for o in q.get("options", [])
        ],
        "knowledge_point": quick_knowledge_point(q),
    }


# --- main --------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    storage.init_db()
    print(f"Exam app: {len(load_questions())} questions loaded")
    print(f"  DB: {DB_PATH}")
    print(f"  KB search: enabled (BM25)")
    app.run(host="127.0.0.1", port=5001, debug=False)
