"""Claudeで記事を自動生成し、WordPressにpublish状態で投稿するスクリプト。

必要な環境変数:
  ANTHROPIC_API_KEY  Anthropic APIキー
  WP_URL             WordPressサイトのURL (例: https://example.com)
  WP_USERNAME        WordPressユーザー名
  WP_APP_PASSWORD    WordPressのアプリケーションパスワード
  ANTHROPIC_MODEL    (任意) 使用するモデルID。省略時は claude-sonnet-5
"""

import json
import os
import re
import sys

import requests

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

ARTICLE_PROMPT = """あなたはブログ記事のライターです。読者にとって役立つ、独立したブログ記事を1本作成してください。
テーマは自由に決めてかまいません（雑学、ライフハック、健康、テクノロジーなど、幅広いジャンルから毎回異なるものを選んでください）。

出力は必ず次のJSON形式のみで返してください。JSON以外の文章やコードブロックの記号は含めないでください。

{
  "title": "記事のタイトル",
  "content": "<p>...</p> のようなHTML形式の本文"
}

本文は見出し(h2/h3)や段落(p)、必要であればリスト(ul/li)を使い、1000文字程度の読み応えのある内容にしてください。
"""


def generate_article() -> tuple[str, str]:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    response = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": ARTICLE_PROMPT}],
        },
        timeout=120,
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"].strip()

    # コードブロックで囲まれて返ってきた場合に備えて中身だけ取り出す
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"JSON形式のレスポンスを取得できませんでした: {text}")

    article = json.loads(match.group(0))
    return article["title"], article["content"]


def post_to_wordpress(title: str, content: str) -> None:
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/posts",
        auth=(username, app_password),
        json={"title": title, "content": content, "status": "publish"},
        timeout=60,
    )
    response.raise_for_status()
    post = response.json()
    print(f"投稿完了: {post.get('link', post.get('id'))}")


def main() -> None:
    title, content = generate_article()
    print(f"生成された記事タイトル: {title}")
    post_to_wordpress(title, content)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)
