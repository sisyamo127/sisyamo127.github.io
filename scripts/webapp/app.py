"""おもちゃブログ記事生成の管理用ローカルWebアプリ。

起動方法:
  python scripts/webapp/app.py

起動後、ブラウザで http://localhost:5000 を開く。
"""

import json
import os
import sys
import threading
import uuid
from datetime import datetime

from flask import Flask, redirect, render_template, request, url_for

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import generate_article as ga  # noqa: E402

app = Flask(__name__)

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
_jobs = {}
_jobs_lock = threading.Lock()


def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_history_entry(entry: dict) -> None:
    history = load_history()
    history.insert(0, entry)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def run_job(job_id: str, topic: str, category: str, include_amazon: bool, include_image: bool) -> None:
    logs = []

    def log(message: str) -> None:
        logs.append(message)
        with _jobs_lock:
            _jobs[job_id]["logs"] = list(logs)

    try:
        result = ga.run_pipeline(
            topic=topic or None,
            category=category or None,
            include_amazon=include_amazon,
            include_featured_image=include_image,
            log=log,
        )
        entry = {
            "id": job_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": result["title"],
            "category": result["category"],
            "char_count": result["char_count"],
            "wp_link": result["wp_link"],
            "local_path": result["local_path"],
            "error": result["error"],
        }
        save_history_entry(entry)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["result"] = entry
    except Exception as exc:
        log(f"エラーが発生しました: {exc}")
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)


@app.route("/")
def index():
    categories = []
    try:
        categories = [c["name"] for c in ga.fetch_categories() if c["name"] != "Uncategorized"]
    except Exception:
        pass
    history = load_history()
    return render_template("index.html", categories=sorted(categories), history=history)


@app.route("/generate", methods=["POST"])
def generate():
    topic = request.form.get("topic", "").strip()
    category = request.form.get("category", "").strip()
    include_amazon = request.form.get("include_amazon") == "on"
    include_image = request.form.get("include_image") == "on"

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "logs": []}

    thread = threading.Thread(
        target=run_job, args=(job_id, topic, category, include_amazon, include_image), daemon=True
    )
    thread.start()

    return redirect(url_for("job_status", job_id=job_id))


@app.route("/jobs/<job_id>")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return redirect(url_for("index"))
    return render_template("job.html", job_id=job_id, job=job)


@app.route("/jobs/<job_id>/data")
def job_status_data(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id, {})
    return {
        "status": job.get("status", "unknown"),
        "logs": job.get("logs", []),
        "result": job.get("result"),
        "error": job.get("error"),
    }


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
