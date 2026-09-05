"""おもちゃブログ記事生成の管理用ローカルWebアプリ。

起動方法:
  python scripts/webapp/app.py

起動後、ブラウザで http://localhost:5000 を開く。
ページ構成(1ページ1機能):
  /        ホーム(概要・最近の記事)
  /generate 記事生成フォーム
  /history  生成履歴の一覧・検索
  /status   API連携状況の確認
"""

import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta

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


def get_categories() -> list:
    try:
        return sorted(c["name"] for c in ga.fetch_categories() if c["name"] != "Uncategorized")
    except Exception:
        return []


@app.route("/")
def dashboard():
    history = load_history()
    week_ago = datetime.now() - timedelta(days=7)
    this_week_count = sum(
        1 for h in history
        if datetime.strptime(h["created_at"], "%Y-%m-%d %H:%M:%S") >= week_ago
    )
    success_count = sum(1 for h in history if h.get("wp_link"))
    error_count = sum(1 for h in history if not h.get("wp_link"))
    return render_template(
        "dashboard.html",
        active="home",
        history=history,
        this_week_count=this_week_count,
        success_count=success_count,
        error_count=error_count,
    )


@app.route("/generate", methods=["GET"])
def generate_form():
    return render_template("generate.html", active="generate", categories=get_categories())


@app.route("/generate", methods=["POST"])
def generate_submit():
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


@app.route("/history")
def history_page():
    return render_template("history.html", active="history", history=load_history())


@app.route("/status")
def status_page():
    services = []

    services.append({
        "name": "Anthropic (Claude)",
        "status": "ok" if os.environ.get("ANTHROPIC_API_KEY") else "warn",
        "detail": "設定済み" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEYが未設定です",
    })

    amazon_keys = ["AMAZON_ACCESS_KEY", "AMAZON_SECRET_KEY", "AMAZON_ASSOCIATE_TAG"]
    if all(os.environ.get(k) for k in amazon_keys):
        services.append({
            "name": "Amazon Creators API",
            "status": "ok",
            "detail": "設定済み(アソシエイト資格審査等でAmazon側から拒否される場合、記事生成時に検索リンクへ自動フォールバックします)",
        })
    else:
        services.append({
            "name": "Amazon Creators API",
            "status": "warn",
            "detail": "未設定です(なくても検索リンク形式で自動フォールバックします)",
        })

    if os.environ.get("WP_URL") and os.environ.get("WP_USERNAME") and os.environ.get("WP_APP_PASSWORD"):
        try:
            ga.fetch_categories()
            services.append({"name": "WordPress", "status": "ok", "detail": os.environ.get("WP_URL", "")})
        except Exception as exc:
            services.append({"name": "WordPress", "status": "ng", "detail": str(exc)})
    else:
        services.append({"name": "WordPress", "status": "warn", "detail": "WP_URL / WP_USERNAME / WP_APP_PASSWORDが未設定です"})

    services.append({
        "name": "Unsplash(アイキャッチ画像)",
        "status": "ok" if os.environ.get("UNSPLASH_ACCESS_KEY") else "warn",
        "detail": "設定済み" if os.environ.get("UNSPLASH_ACCESS_KEY") else "未設定です(なくてもアイキャッチなしで生成されます)",
    })

    return render_template("status.html", active="status", services=services)


@app.route("/jobs/<job_id>")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return redirect(url_for("dashboard"))
    return render_template("job.html", active="generate", job_id=job_id, job=job)


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
