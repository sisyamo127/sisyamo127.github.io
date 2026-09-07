"""おもちゃブログ向けの記事を生成し、WordPressに下書きとして投稿するスクリプト。
サイト(omotya-museum.com)の既存の記事スタイル(「ゆう」の会話導入、【】タイトル、
Cocoonテーマのふきだし/マーカー、Amazon購入ボタン)に合わせて生成する。
公開は行わず、下書き(draft)として保存するので、最終確認は手動で行うこと。

必要な環境変数(.envファイルに記載):
  ANTHROPIC_API_KEY     Anthropic APIキー
  AMAZON_ACCESS_KEY     Amazon Creators APIの認証情報ID(amzn1.application-oa2-client...)
  AMAZON_SECRET_KEY     Amazon Creators APIのクライアントシークレット
  AMAZON_ASSOCIATE_TAG  Amazonアソシエイトタグ
  WP_URL                WordPressサイトのURL(例: https://www.omotya-museum.com)
  WP_USERNAME           WordPressユーザー名
  WP_APP_PASSWORD       WordPressのアプリケーションパスワード
  ANTHROPIC_MODEL       (任意) 使用するモデルID。省略時は claude-sonnet-5

必要なライブラリ: requirements.txt を参照 (pip install -r scripts/requirements.txt)

実行方法:
  python scripts/generate_article.py
"""

import json
import os
import re
import sys
import urllib.parse
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

# サイトに既存の「ゆう」アイコン画像(会話ブロックで使用)
YU_AVATAR_URL = (
    "https://www.omotya-museum.com/wp-content/uploads/2024/09/"
    "cropped-e5d5fac4-7a55-4700-b17e-9be062151c69.webp"
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_categories() -> list:
    """サイトに既存のカテゴリー一覧を取得する。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    response = requests.get(
        f"{wp_url}/wp-json/wp/v2/categories",
        params={"per_page": 100},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def build_article_prompt(
    categories: list, topic: str | None = None, category: str | None = None
) -> str:
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")

    if topic:
        topic_instruction = f"今回のテーマは必ず次の内容にすること:「{topic}」"
    else:
        topic_instruction = (
            "テーマは自由に決めてください(新作トイのレビュー、レトロトイの魅力、知育玩具の選び方、"
            "DIYおもちゃ、コレクター向け情報、プレゼント選びのコツ、旅行先での子ども向けお土産など、"
            "おもちゃに関する範囲内で、毎回異なる話題にしてください)。"
        )

    if category:
        category_instruction = f'"category"には必ず次の値をそのまま使うこと:「{category}」'
    else:
        category_instruction = (
            "このブログの既存カテゴリーの中から、記事に最も合うものを1つだけ選んでください"
            f"(新しいカテゴリー名を作らないこと): {category_names}"
        )

    return f"""あなたは「おもちゃミュージアム」というブログの専属ライターです。
このブログには「ゆう」というハムスターのキャラクターがいて、読者からの悩み相談に答える形で
記事を書き始めるのが定番のスタイルです。おもちゃに関するブログ記事を1本作成してください。
{topic_instruction}

## タイトル
【】で始まる、具体的で読者の悩みに刺さるフックタイトルにする。
例:「【福岡空港お土産】出張パパ必見!子どもが喜ぶおもちゃまとめ」「【年齢別】知育玩具の選び方完全ガイド」

## 会話パート(3箇所)
記事には、悩みを持つ読者と「ゆう」が掛け合いをする会話を**冒頭・中盤・最後の3箇所**に入れる。
- reader_persona: 読者役の短いラベル(例:「読者(プレゼントに悩むママ)」「読者(出張中のパパ)」)。冒頭・中盤で共通して使う
- reader_question: 冒頭での読者の悩み・質問(1〜2文)
- yu_answer: 冒頭での「ゆう」の返答(1〜2文、絵文字を使ってよい、親しみやすいトーン)
- mid_question: 記事の内容を踏まえた、中盤での読者の追加の疑問(1文程度。例:「じゃあ結局どれがいいの?」)
- mid_answer: 中盤での「ゆう」の返答(1〜2文)
- closing_comment: 記事の最後に「ゆう」だけが読者に語りかける、まとめの一言・応援コメント(1〜2文、絵文字を使ってよい)

本文(content)の中で、中盤の会話を入れるのにちょうど良い位置(だいたい本文の半分あたり、話題の区切りが良いところ)に、
プレースホルダーとして `[[MID_CONVERSATION]]` という文字列だけを1箇所挿入すること(この文字列は後で会話ブロックに
置き換えるので、他の文章とは改行で区切ること)。

## 文体・トーン(本文)
- 「です・ます調」で、丁寧で優しい雰囲気にする
- 具体的で実用的な情報(店舗名、商品の特徴など)を盛り込む
- SEOを意識し、検索されやすいキーワードを自然に本文へ盛り込む
- アフィリエイト記事として成立するよう、紹介する商品への興味を高める文章にする

## 文字数・構成(重要)
- 本文(content)は**必ず5000文字以上**にすること。4000文字程度では不足なので、必ず超えるように書くこと
- 目安として、h2見出しを5〜7個程度用意し、それぞれの見出しの下に400〜600文字程度の解説を書くと5000文字を超えやすい
- <h2>から始めること(タイトルや会話パートは含めない。それらは別途組み立てるため)
- 複数の見出し(h2/h3)によるセクション → まとめ、という構成にする

## 装飾(デザイン)
本文のHTML内で、以下のような装飾を適宜使ってください(インラインstyleで指定すること。WordPressの投稿にそのまま貼り付けるため、外部CSSには依存しないこと):
- 重要な語句は <strong> で太字にする
- 特に注目してほしい語句は <span class="marker-under">のように囲む(このサイトの既存記事で使われているマーカースタイル)
- 「ポイント」「まとめ」などは背景色付きのボックスにする。例:
  <div style="background:#fff3cd;border-left:4px solid #ffc107;padding:16px;margin:16px 0;border-radius:4px;"><strong>ポイント</strong><br>ここに内容</div>
- 比較や一覧が適切な場面ではtable要素も使ってよい

## カテゴリー
{category_instruction}

## 出力形式
必ず次のJSON形式のみで返してください。JSON以外の文章やコードブロックの記号は含めないでください。

{{
  "title": "【】から始まる記事タイトル(検索されやすいキーワードを含む)",
  "meta_description": "検索結果に表示される説明文(120文字程度)",
  "keywords": ["SEOキーワード1", "SEOキーワード2", "SEOキーワード3"],
  "category": "上のカテゴリー一覧から選んだ1つ",
  "reader_persona": "読者役の短いラベル",
  "reader_question": "冒頭の読者の悩み・質問",
  "yu_answer": "冒頭のゆうの返答",
  "mid_question": "中盤の読者の追加の疑問",
  "mid_answer": "中盤のゆうの返答",
  "closing_comment": "最後のゆうのまとめ・応援コメント",
  "amazon_search_keyword": "記事に関連する商品をAmazonで探すための検索キーワード(具体的な商品カテゴリ名、日本語)",
  "content": "h2から始まる本文HTML(5000文字以上、途中に[[MID_CONVERSATION]]を1箇所含む)"
}}
"""


MIN_CONTENT_CHARS = 5000


def _call_claude(messages: list) -> tuple[str, dict]:
    """Claudeを呼び出し、(テキスト全文, パース済みJSON)を返す。"""
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
            "max_tokens": 12000,
            "messages": messages,
        },
        timeout=180,
    )
    response.raise_for_status()
    content_blocks = response.json()["content"]
    text_block = next(
        (block for block in content_blocks if block.get("type") == "text"), None
    )
    if text_block is None:
        raise ValueError(f"テキスト形式のレスポンスが見つかりませんでした: {content_blocks}")
    text = text_block["text"].strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"JSON形式のレスポンスを取得できませんでした: {text}")

    return text, json.loads(match.group(0))


def _content_char_count(content: str) -> int:
    """本文HTMLからタグを除いた文字数を数える。"""
    text = re.sub(r"<[^>]+>", "", content)
    return len(text.strip())


DEFAULT_LOG = lambda msg: print(msg, file=sys.stderr)  # noqa: E731


def _generate_with_expansion(messages: list, max_expand_attempts: int = 2, log=DEFAULT_LOG) -> dict:
    """contentが5000文字未満なら追記を依頼し、条件を満たすまで(最大max_expand_attempts回)繰り返す。"""
    raw_text, article = _call_claude(messages)

    for _ in range(max_expand_attempts):
        char_count = _content_char_count(article.get("content", ""))
        if char_count >= MIN_CONTENT_CHARS:
            break

        log(f"本文が{char_count}文字と{MIN_CONTENT_CHARS}文字未満のため、追記を依頼します...")
        messages.append({"role": "assistant", "content": raw_text})
        messages.append({
            "role": "user",
            "content": (
                f"content(本文)が現在{char_count}文字しかありません。"
                f"{MIN_CONTENT_CHARS}文字以上になるよう、既存の内容を薄めず、具体例・詳細な説明・"
                "追加のセクション(h2/h3)を加えて拡張してください。"
                "他のフィールド(title, meta_description等)も含め、同じJSON形式で全文を出力し直してください。"
                "[[MID_CONVERSATION]]のプレースホルダーは1箇所のまま維持してください。"
            ),
        })
        raw_text, article = _call_claude(messages)

    return article


def generate_article(
    categories: list,
    topic: str | None = None,
    category: str | None = None,
    max_expand_attempts: int = 2,
    log=DEFAULT_LOG,
) -> dict:
    messages = [
        {"role": "user", "content": build_article_prompt(categories, topic, category)}
    ]
    return _generate_with_expansion(messages, max_expand_attempts, log)


# ---------------------------------------------------------------------------
# タイトル案生成 → SEOチェック → アウトライン生成 → アウトラインに沿った本文生成
# ---------------------------------------------------------------------------

TITLE_SEO_RULES = [
    "文字数: 全角28〜32文字程度(検索結果で見切れやすい極端な長短を避ける)",
    "キーワード配置: target_keywordがタイトルの前半(できれば最初の15文字程度)に含まれている",
    "重複・カニバリ回避: 既存記事タイトルと内容的に重複していない",
    "具体性: 「おすすめ」「まとめ」など抽象的な言葉だけで終わらず、具体的な切り口(年齢・シーン・数字等)がある",
    "誇大表現の回避: 「絶対」「100%」等の断定的・誇大な表現を使っていない",
]


def fetch_existing_titles(limit: int = 50) -> list:
    """カニバリ確認用に、サイトに既存の公開記事タイトルを取得する。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    response = requests.get(
        f"{wp_url}/wp-json/wp/v2/posts",
        params={"per_page": limit, "_fields": "title"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return [p["title"]["rendered"] for p in response.json()]


def generate_title_candidates(
    categories: list, topic: str | None = None, count: int = 10, log=DEFAULT_LOG
) -> list:
    """SEOを意識したタイトル候補をcount個生成する。"""
    if topic:
        topic_instruction = f"テーマは必ず次の内容にすること:「{topic}」"
    else:
        topic_instruction = (
            "テーマは自由に決めてください(新作トイのレビュー、レトロトイの魅力、知育玩具の選び方、"
            "DIYおもちゃ、コレクター向け情報、プレゼント選びのコツ、旅行先での子ども向けお土産など、"
            "おもちゃに関する範囲内で)。"
        )
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")

    prompt = f"""あなたは「おもちゃミュージアム」というブログのSEOライターです。
{topic_instruction}
このテーマで記事を書くとしたら、という前提で、SEOを意識したタイトル候補を{count}個考えてください。

## タイトルのスタイル
【】で始まる、具体的で読者の悩みに刺さるフックタイトルにする。
例:「【福岡空港お土産】出張パパ必見!子どもが喜ぶおもちゃまとめ」「【年齢別】知育玩具の選び方完全ガイド」

## 参考: このブログの既存カテゴリー
{category_names}

各候補には、そのタイトルでSEO的に狙う検索キーワード(target_keyword)も1つ添えてください。
{count}個は、切り口(年齢別/シーン別/悩み別など)が重ならないようにバリエーションをつけてください。

必ず次のJSON形式のみで返してください。他の文章は含めないこと。
{{"candidates": [{{"title": "...", "target_keyword": "..."}}, ...]}}
"""
    _, data = _call_claude([{"role": "user", "content": prompt}])
    return data.get("candidates", [])


def check_titles_seo(candidates: list, existing_titles: list, log=DEFAULT_LOG) -> list:
    """タイトル候補をSEOルールに沿ってチェックする(タイトル生成とは別のAIコールで行う)。"""
    rules_text = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(TITLE_SEO_RULES))
    candidates_json = json.dumps(candidates, ensure_ascii=False)
    existing_text = "\n".join(f"- {t}" for t in existing_titles) or "(既存記事なし)"

    prompt = f"""あなたはSEOの専門家です。以下のブログ記事タイトル候補を、次のルールに基づいて厳密にチェックしてください。
自分でタイトルを考えるのではなく、あくまで審査役として、与えられた候補だけを評価してください。

## SEOルール
{rules_text}

## 既存記事タイトル(この内容と重複・カニバリしていないか確認すること)
{existing_text}

## チェック対象のタイトル候補
{candidates_json}

各候補について、ルールに沿っているかを判定してください。
- verdict: 問題なければ"ok"、軽微でも問題があれば"warn"
- reasons: 該当した問題点を箇条書きで(日本語、具体的に)。問題がなければ空配列

必ず次のJSON形式のみで返してください。他の文章は含めないこと。候補の順序・件数はそのまま維持すること。
{{"results": [{{"title": "...", "verdict": "ok", "reasons": []}}, ...]}}
"""
    _, data = _call_claude([{"role": "user", "content": prompt}])
    return data.get("results", [])


def generate_outline(
    title: str,
    target_keyword: str,
    categories: list,
    category: str | None = None,
    log=DEFAULT_LOG,
) -> dict:
    """承認されたタイトルをもとに、本文を書く前の構成案(アウトライン)を作る。"""
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")
    if category:
        category_instruction = f'"category"には必ず次の値をそのまま使うこと:「{category}」'
    else:
        category_instruction = (
            "このブログの既存カテゴリーの中から、記事に最も合うものを1つだけ選んでください"
            f"(新しいカテゴリー名を作らないこと): {category_names}"
        )

    prompt = f"""あなたは「おもちゃミュージアム」というブログの編集者です。
次のタイトルで記事を書くことが決まりました。本文を書く前に、構成案(アウトライン)を作成してください。

タイトル:「{title}」
SEOで狙うキーワード:「{target_keyword}」

このブログには「ゆう」というハムスターのキャラクターがいて、読者からの悩み相談に答える形で
記事を書き始めるのが定番のスタイルです。会話パートを冒頭・中盤・最後の3箇所に入れます。
- reader_persona: 読者役の短いラベル
- reader_question / yu_answer: 冒頭の会話
- mid_question / mid_answer: 中盤の会話(記事の内容を踏まえた追加の疑問)
- closing_comment: 最後の「ゆう」単独のまとめ・応援コメント

## カテゴリー
{category_instruction}

## アウトライン
5〜7個の見出し(h2)を考え、それぞれ何を書くかの概要(1〜2文)を添えてください。
全体で本文5000文字以上になるボリューム感を意識すること。

## 記事内で紹介する商品(2〜3個)
アウトラインの中から、具体的な商品を紹介するのにふさわしい見出しを2〜3個選び、
それぞれで紹介する商品(product_mentions)を考えてください。
- heading: 紹介する箇所の見出し(上のoutlineのheadingと同じ文字列にすること)
- name: 紹介する商品の名前・種類(例:「木製の型はめパズル」)
- search_keyword: その商品をAmazonで検索するための具体的なキーワード(日本語)

必ず次のJSON形式のみで返してください。他の文章は含めないこと。

{{
  "title": "{title}",
  "seo_title": "検索エンジン向けのSEOタイトル(全角32文字以内。titleと同じでよいが、32文字を超える場合はここで短縮する)",
  "meta_description": "検索結果に表示される説明文(120文字程度)",
  "keywords": ["SEOキーワード1", "SEOキーワード2", "SEOキーワード3"],
  "category": "選んだカテゴリー",
  "reader_persona": "読者役の短いラベル",
  "reader_question": "冒頭の読者の悩み・質問",
  "yu_answer": "冒頭のゆうの返答",
  "mid_question": "中盤の読者の追加の疑問",
  "mid_answer": "中盤のゆうの返答",
  "closing_comment": "最後のゆうのまとめ・応援コメント",
  "amazon_search_keyword": "記事全体を総括するおすすめ商品をAmazonで探すための検索キーワード(具体的な商品カテゴリ名、日本語)",
  "outline": [
    {{"heading": "見出し1", "summary": "このセクションで書く内容の概要"}}
  ],
  "product_mentions": [
    {{"heading": "見出し1", "name": "商品名・種類", "search_keyword": "Amazon検索キーワード"}}
  ]
}}
"""
    _, data = _call_claude([{"role": "user", "content": prompt}])
    return data


def build_content_prompt_from_outline(outline: dict) -> str:
    outline_lines = "\n".join(
        f"- {o['heading']}: {o['summary']}" for o in outline.get("outline", [])
    )
    keywords = "、".join(outline.get("keywords", []))
    product_mentions = outline.get("product_mentions", [])
    if product_mentions:
        product_lines = "\n".join(
            f'- 見出し「{p["heading"]}」の中で「{p["name"]}」に触れたすぐ後に '
            f'`[[PRODUCT:{i}]]` というプレースホルダーを1つ挿入すること'
            for i, p in enumerate(product_mentions)
        )
        product_instruction = f"""
## 商品紹介プレースホルダー(重要)
以下の商品について本文中で触れ、触れた直後にプレースホルダーを挿入してください(このプレースホルダーは
後で商品カードのHTMLに置き換えるので、他の文章とは改行で区切ること):
{product_lines}
"""
    else:
        product_instruction = ""

    return f"""あなたは「おもちゃミュージアム」というブログの専属ライターです。
以下の承認済み構成案に沿って、記事本文を執筆してください。構成案の見出し・流れは変更しないこと。

タイトル:「{outline.get('title', '')}」
メタディスクリプション: {outline.get('meta_description', '')}
SEOキーワード: {keywords}

## 構成案(この通りの見出し・順序で書くこと)
{outline_lines}
{product_instruction}

## 文体・トーン
- 「です・ます調」で、丁寧で優しい雰囲気にする
- 具体的で実用的な情報(店舗名、商品の特徴など)を盛り込む
- SEOキーワードを自然に本文へ盛り込む
- アフィリエイト記事として成立するよう、紹介する商品への興味を高める文章にする

## 文字数(重要)
- 本文は**必ず5000文字以上**にすること
- 各見出しにつき400〜700文字程度を目安に、構成案の各セクションを詳しく執筆すること

## 装飾(デザイン)
本文のHTML内で、以下のような装飾を適宜使ってください(インラインstyleで指定すること):
- 重要な語句は <strong> で太字にする
- 特に注目してほしい語句は <span class="marker-under">のように囲む
- 「ポイント」「まとめ」などは背景色付きのボックスにする。例:
  <div style="background:#fff3cd;border-left:4px solid #ffc107;padding:16px;margin:16px 0;border-radius:4px;"><strong>ポイント</strong><br>ここに内容</div>
- 比較や一覧が適切な場面ではtable要素も使ってよい

## 会話プレースホルダー
本文中盤の、話題の区切りが良い位置に、プレースホルダーとして `[[MID_CONVERSATION]]` という文字列だけを1箇所挿入すること。

## 出力形式
必ず次のJSON形式のみで返してください。他の文章やコードブロックの記号は含めないこと。
<h2>から始めること(タイトルや会話パートは含めない)。

{{"content": "構成案に沿ったh2から始まる本文HTML(5000文字以上、[[MID_CONVERSATION]]を1箇所含む)"}}
"""


def generate_article_from_outline(outline: dict, max_expand_attempts: int = 2, log=DEFAULT_LOG) -> dict:
    """承認済みのアウトラインに沿って本文を生成し、タイトル等の固定フィールドと結合する。"""
    messages = [{"role": "user", "content": build_content_prompt_from_outline(outline)}]
    result = _generate_with_expansion(messages, max_expand_attempts, log)

    article = dict(outline)
    article["content"] = result["content"]
    return article


# ---------------------------------------------------------------------------
# 既存記事のリライト
# ---------------------------------------------------------------------------

def build_rewrite_prompt(existing_title: str, existing_text: str, categories: list, category: str | None = None) -> str:
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")
    if category:
        category_instruction = f'"category"には必ず次の値をそのまま使うこと:「{category}」'
    else:
        category_instruction = (
            "このブログの既存カテゴリーの中から、記事に最も合うものを1つだけ選んでください"
            f"(新しいカテゴリー名を作らないこと): {category_names}"
        )
    excerpt = existing_text[:6000]

    return f"""あなたは「おもちゃミュージアム」というブログの専属ライターです。
以下は現在サイトに掲載されている記事です。この記事を、最新のハウススタイルに沿ってリライトしてください。

## 元記事
タイトル:「{existing_title}」
本文(参考、HTMLタグは除去済み):
{excerpt}

## リライトの方針
- 元記事のトピック・具体的な事実(店舗名、商品名、地名等)はできるだけ尊重すること
- 情報が古くなっていそうな部分(価格、流行、時期の記述等)は一般的で無難な表現に書き換えるか、最新の情報として自然に書き直すこと
- 単なる言い換えではなく、より詳しく・具体的に加筆し、SEOとしても改善すること
- タイトルは元のままでもよいが、より検索されやすいタイトルがあれば改善してよい(【】から始まる形式を維持)

## このブログの定番スタイル
「ゆう」というハムスターのキャラクターが読者からの悩み相談に答える形で記事を書き始める。
会話パートを冒頭・中盤・最後の3箇所に入れる。
- reader_persona / reader_question / yu_answer: 冒頭の会話
- mid_question / mid_answer: 中盤の会話
- closing_comment: 最後の「ゆう」単独のまとめ・応援コメント

## カテゴリー
{category_instruction}

## 商品紹介(2〜3個)
記事に合う具体的な商品を2〜3個選び、product_mentionsとして挙げてください。
- heading: 紹介する箇所の見出し(outlineのheadingと同じ文字列)
- name: 商品の名前・種類
- search_keyword: Amazonで検索するための具体的なキーワード

## 文字数・構成(重要)
- 本文(content)は**必ず5000文字以上**にすること
- h2見出しを5〜7個用意し、それぞれ400〜600文字程度で執筆すること
- <h2>から始めること(タイトルや会話パートは含めない)
- 本文中盤の良い位置に `[[MID_CONVERSATION]]` を1箇所、商品に触れた直後に `[[PRODUCT:0]]` 等のプレースホルダーを挿入すること

## 装飾(デザイン)
本文のHTML内で、以下のような装飾を適宜使ってください(インラインstyleで指定すること):
- 重要な語句は <strong> で太字にする
- 特に注目してほしい語句は <span class="marker-under">のように囲む
- 「ポイント」「まとめ」などは背景色付きのボックスにする。例:
  <div style="background:#fff3cd;border-left:4px solid #ffc107;padding:16px;margin:16px 0;border-radius:4px;"><strong>ポイント</strong><br>ここに内容</div>
- 比較や一覧が適切な場面ではtable要素も使ってよい

## 出力形式
必ず次のJSON形式のみで返してください。他の文章やコードブロックの記号は含めないこと。

{{
  "title": "【】から始まる記事タイトル",
  "seo_title": "検索エンジン向けのSEOタイトル(全角32文字以内)",
  "meta_description": "検索結果に表示される説明文(120文字程度)",
  "keywords": ["SEOキーワード1", "SEOキーワード2", "SEOキーワード3"],
  "category": "選んだカテゴリー",
  "reader_persona": "読者役の短いラベル",
  "reader_question": "冒頭の読者の悩み・質問",
  "yu_answer": "冒頭のゆうの返答",
  "mid_question": "中盤の読者の追加の疑問",
  "mid_answer": "中盤のゆうの返答",
  "closing_comment": "最後のゆうのまとめ・応援コメント",
  "amazon_search_keyword": "記事全体を総括するおすすめ商品のAmazon検索キーワード",
  "product_mentions": [
    {{"heading": "見出し", "name": "商品名・種類", "search_keyword": "Amazon検索キーワード"}}
  ],
  "content": "h2から始まる本文HTML(5000文字以上、[[MID_CONVERSATION]]と[[PRODUCT:n]]を含む)"
}}
"""


def generate_rewrite(
    existing_title: str,
    existing_text: str,
    categories: list,
    category: str | None = None,
    max_expand_attempts: int = 2,
    log=DEFAULT_LOG,
) -> dict:
    """既存記事を参考に、ハウススタイルへリライトした記事を生成する。"""
    messages = [
        {"role": "user", "content": build_rewrite_prompt(existing_title, existing_text, categories, category)}
    ]
    return _generate_with_expansion(messages, max_expand_attempts, log)


def build_conversation_balloon_html(persona: str, question: str, answer: str) -> str:
    """読者と「ゆう」の会話(Cocoonのふきだしブロック相当)を組み立てる。冒頭・中盤で使用。"""
    return f"""<div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin:16px 0;background:#fafafa;">
<p style="margin:0 0 12px;"><strong>{persona}</strong><br>{question}</p>
<div style="display:flex;align-items:flex-start;gap:12px;">
<img src="{YU_AVATAR_URL}" alt="ゆう" style="width:56px;height:56px;border-radius:50%;object-fit:cover;flex-shrink:0;">
<div style="background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 14px;">
<strong>ゆう</strong><br>{answer}
</div>
</div>
</div>"""


def build_yu_comment_html(comment: str) -> str:
    """記事末尾の「ゆう」単独のまとめコメントを組み立てる。"""
    return f"""<div style="display:flex;align-items:flex-start;gap:12px;margin:24px 0;">
<img src="{YU_AVATAR_URL}" alt="ゆう" style="width:56px;height:56px;border-radius:50%;object-fit:cover;flex-shrink:0;">
<div style="background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 14px;">
<strong>ゆう</strong><br>{comment}
</div>
</div>"""


def search_amazon_products(keyword: str, item_count: int = 3) -> list:
    from amazon_creatorsapi import AmazonCreatorsApi

    # バージョン"3.3"はamazon.co.jp(日本)向けのCreators API認証エンドポイントを指す。
    amazon = AmazonCreatorsApi(
        os.environ["AMAZON_ACCESS_KEY"],
        os.environ["AMAZON_SECRET_KEY"],
        "3.3",
        os.environ["AMAZON_ASSOCIATE_TAG"],
        country="JP",
    )
    result = amazon.search_items(keywords=keyword, item_count=item_count)
    return result.items or []


def build_product_card_html(products: list) -> str:
    """Amazon Creators APIから実際の商品情報が取れた場合のカード表示。"""
    blocks = []
    for product in products:
        try:
            title_text = product.item_info.title.display_value
            url = product.detail_page_url
            image_url = product.images.primary.large.url
        except AttributeError:
            continue

        blocks.append(f"""<div style="border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0;display:flex;gap:16px;flex-wrap:wrap;">
<img src="{image_url}" alt="{title_text}" style="width:140px;height:140px;object-fit:contain;flex-shrink:0;">
<div style="flex:1;min-width:200px;">
<p style="font-weight:bold;margin:0 0 12px;">{title_text}</p>
<a rel="nofollow noopener sponsored" href="{url}" target="_blank" style="display:inline-block;background:#ff6600;color:#fff;font-weight:700;padding:10px 20px;border-radius:8px;text-decoration:none;">▶ Amazonで見る</a>
</div>
</div>""")
    return "\n".join(blocks)


def build_amazon_search_button_html(keyword: str) -> str:
    """Creators APIが使えない場合のフォールバック(検索結果への通常アフィリエイトリンク)。"""
    tag = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
    query = urllib.parse.quote(keyword)
    url = f"https://www.amazon.co.jp/s?k={query}"
    if tag:
        url += f"&tag={urllib.parse.quote(tag)}"

    return f"""<div style="margin:16px 0;">
<a rel="nofollow noopener sponsored" href="{url}" target="_blank" style="display:inline-block;background:#ff6600;color:#fff;font-weight:700;padding:10px 20px;border-radius:8px;text-decoration:none;">▶ Amazonで「{keyword}」を探す</a>
</div>"""


def build_inline_product_card_html(name: str, search_keyword: str, log=DEFAULT_LOG) -> str:
    """本文中に挿入する、1商品ぶんの小さな紹介カード(画像+購入リンク)を組み立てる。

    Amazon Creators APIで実商品が取れればその画像・リンクを使い、
    取れない場合はUnsplashの画像(あれば)+Amazon検索リンクにフォールバックする。
    """
    try:
        products = search_amazon_products(search_keyword, item_count=1)
        for product in products:
            try:
                title_text = product.item_info.title.display_value
                url = product.detail_page_url
                image_url = product.images.primary.large.url
                return f"""<div style="border:1px solid #ddd;border-radius:10px;padding:12px;margin:16px 0;display:flex;gap:12px;align-items:center;background:#fafafa;">
<img src="{image_url}" alt="{title_text}" style="width:90px;height:90px;object-fit:contain;flex-shrink:0;border-radius:6px;background:#fff;">
<div style="flex:1;min-width:160px;">
<p style="font-weight:bold;margin:0 0 8px;font-size:0.92rem;">{title_text}</p>
<a rel="nofollow noopener sponsored" href="{url}" target="_blank" style="display:inline-block;background:#ff6600;color:#fff;font-weight:700;padding:6px 16px;border-radius:6px;text-decoration:none;font-size:0.85rem;">▶ Amazonで見る</a>
</div>
</div>"""
            except AttributeError:
                continue
    except Exception as exc:
        log(f"商品「{name}」のAmazon検索に失敗しました(画像リンクにフォールバックします): {exc}")

    image_url = None
    try:
        image_url = fetch_unsplash_image_url(search_keyword)
    except Exception as exc:
        log(f"商品「{name}」の画像取得に失敗しました: {exc}")

    tag = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
    query = urllib.parse.quote(search_keyword)
    search_url = f"https://www.amazon.co.jp/s?k={query}"
    if tag:
        search_url += f"&tag={urllib.parse.quote(tag)}"

    image_html = (
        f'<img src="{image_url}" alt="{name}" style="width:90px;height:90px;object-fit:cover;flex-shrink:0;border-radius:6px;">'
        if image_url else ""
    )
    return f"""<div style="border:1px solid #ddd;border-radius:10px;padding:12px;margin:16px 0;display:flex;gap:12px;align-items:center;background:#fafafa;">
{image_html}
<div style="flex:1;min-width:160px;">
<p style="font-weight:bold;margin:0 0 8px;font-size:0.92rem;">{name}</p>
<a rel="nofollow noopener sponsored" href="{search_url}" target="_blank" style="display:inline-block;background:#ff6600;color:#fff;font-weight:700;padding:6px 16px;border-radius:6px;text-decoration:none;font-size:0.85rem;">▶ Amazonで「{name}」を探す</a>
</div>
</div>"""


def resolve_category_id(category_name: str, categories: list) -> int | None:
    for c in categories:
        if c["name"] == category_name:
            return c["id"]
    return None


def fetch_unsplash_image_url(keyword: str) -> str | None:
    """Unsplashから記事テーマに合う画像のURLを1枚取得する。キーがなければNoneを返す。"""
    access_key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not access_key:
        return None

    response = requests.get(
        "https://api.unsplash.com/search/photos",
        params={"query": keyword, "per_page": 1, "orientation": "landscape"},
        headers={"Authorization": f"Client-ID {access_key}"},
        timeout=30,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return None
    return results[0]["urls"]["regular"]


def upload_featured_image(image_url: str, filename: str) -> int | None:
    """画像URLをダウンロードしてWordPressメディアライブラリにアップロードし、メディアIDを返す。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    image_response = requests.get(image_url, headers={"User-Agent": BROWSER_USER_AGENT}, timeout=60)
    image_response.raise_for_status()

    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/media",
        auth=(username, app_password),
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Content-Disposition": f'attachment; filename="{filename}.jpg"',
            "Content-Type": "image/jpeg",
        },
        data=image_response.content,
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"アイキャッチ画像のアップロードに失敗しました (HTTP {response.status_code}): {response.text}"
        )
    return response.json().get("id")


def post_to_wordpress_draft(
    title: str, content: str, category_id: int | None, featured_media_id: int | None = None
) -> str:
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    payload = {"title": title, "content": content, "status": "draft"}
    if category_id is not None:
        payload["categories"] = [category_id]
    if featured_media_id is not None:
        payload["featured_media"] = featured_media_id

    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/posts",
        auth=(username, app_password),
        headers={"User-Agent": BROWSER_USER_AGENT},
        json=payload,
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"WordPressへの下書き保存に失敗しました (HTTP {response.status_code}): {response.text}"
        )
    post = response.json()
    return post.get("link") or f"post id {post.get('id')}"


def update_wordpress_post(
    post_id: int,
    title: str,
    content: str,
    category_id: int | None,
    featured_media_id: int | None = None,
) -> str:
    """既存の投稿をリライト結果で上書きする(下書きに戻す)。slugは変更しないためURLは維持される。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    payload = {"title": title, "content": content, "status": "draft"}
    if category_id is not None:
        payload["categories"] = [category_id]
    if featured_media_id is not None:
        payload["featured_media"] = featured_media_id

    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/posts/{post_id}",
        auth=(username, app_password),
        headers={"User-Agent": BROWSER_USER_AGENT},
        json=payload,
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"WordPressの記事更新に失敗しました (HTTP {response.status_code}): {response.text}"
        )
    post = response.json()
    return post.get("link") or f"post id {post.get('id')}"


def fetch_posts_for_rewrite(per_page: int = 50) -> list:
    """リライト対象を選ぶための既存記事一覧(公開・下書き含む)を取得する。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    response = requests.get(
        f"{wp_url}/wp-json/wp/v2/posts",
        auth=(username, app_password),
        params={"per_page": per_page, "status": "publish,draft,future,pending", "_fields": "id,title,link,status"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return [
        {"id": p["id"], "title": p["title"]["rendered"], "link": p["link"], "status": p["status"]}
        for p in response.json()
    ]


def fetch_post_for_rewrite(post_id: int) -> dict:
    """リライト元となる既存記事の本文を取得する(HTMLタグは除去してテキスト化)。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    response = requests.get(
        f"{wp_url}/wp-json/wp/v2/posts/{post_id}",
        auth=(username, app_password),
        params={"_fields": "id,title,content,categories"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    text = re.sub(r"<[^>]+>", " ", data["content"]["rendered"])
    text = re.sub(r"\s+", " ", text).strip()
    return {"id": data["id"], "title": data["title"]["rendered"], "text": text}


ARTICLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "articles")


def save_article_locally(article: dict, full_content: str) -> str:
    os.makedirs(ARTICLES_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w\-]", "", article["title"].replace(" ", "-"))[:30] or "article"
    filepath = os.path.join(ARTICLES_DIR, f"{timestamp}-{slug}.html")

    keywords_line = ", ".join(article.get("keywords", []))
    html = f"""<!-- title: {article['title']} -->
<!-- seo_title(Cocoon SEOボックスの「SEOタイトル」欄に貼り付け): {article.get('seo_title', article['title'])} -->
<!-- meta description(Cocoon SEOボックスの「メタディスクリプション」欄に貼り付け): {article.get('meta_description', '')} -->
<!-- keywords(Cocoon SEOボックスの「メタキーワード」欄に貼り付け): {keywords_line} -->
<!-- category: {article.get('category', '')} -->

{full_content}"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


def finalize_and_publish(
    article: dict,
    categories: list,
    include_amazon: bool = True,
    include_featured_image: bool = True,
    rewrite_post_id: int | None = None,
    log=print,
) -> dict:
    """生成済みのarticle(title/content等を含む辞書)を仕上げ、ローカル保存+WordPress投稿までを行う。

    generate_article()由来・generate_article_from_outline()由来・generate_rewrite()由来の
    どのarticleでも使える共通処理。rewrite_post_idを指定すると、新規投稿ではなく
    その投稿IDを下書きとして上書きする(slugは変更しないためURLは維持される)。
    """
    persona = article.get("reader_persona", "読者")
    intro_html = build_conversation_balloon_html(
        persona, article.get("reader_question", ""), article.get("yu_answer", "")
    )
    mid_html = build_conversation_balloon_html(
        persona, article.get("mid_question", ""), article.get("mid_answer", "")
    )
    closing_html = build_yu_comment_html(article.get("closing_comment", ""))

    body = article["content"]
    if "[[MID_CONVERSATION]]" in body:
        body = body.replace("[[MID_CONVERSATION]]", mid_html)
    else:
        # モデルがプレースホルダーを出力しなかった場合は本文中央付近に挿入する
        midpoint = len(body) // 2
        insert_at = body.find("<h2", midpoint) if body.find("<h2", midpoint) != -1 else midpoint
        body = body[:insert_at] + mid_html + body[insert_at:]

    if include_amazon:
        for i, mention in enumerate(article.get("product_mentions", [])):
            placeholder = f"[[PRODUCT:{i}]]"
            if placeholder in body:
                card_html = build_inline_product_card_html(
                    mention.get("name", ""), mention.get("search_keyword", ""), log=log
                )
                body = body.replace(placeholder, card_html)
    # 残ったプレースホルダー(件数不一致等)は表示に影響しないよう除去する
    body = re.sub(r"\[\[PRODUCT:\d+\]\]", "", body)

    product_html = ""
    amazon_keyword = article.get("amazon_search_keyword")
    if include_amazon and amazon_keyword:
        try:
            products = search_amazon_products(amazon_keyword)
            product_html = build_product_card_html(products)
        except Exception as exc:
            log(f"Amazon商品検索に失敗しました(検索リンクにフォールバックします): {exc}")
        if not product_html:
            product_html = build_amazon_search_button_html(amazon_keyword)

    amazon_section = f"<h2>おすすめ商品</h2>\n{product_html}\n\n" if product_html else ""
    full_content = f"{intro_html}\n\n{body}\n\n{amazon_section}{closing_html}"

    filepath = save_article_locally(article, full_content)
    log(f"記事をローカルに保存しました: {filepath}")

    result = {
        "title": article["title"],
        "seo_title": article.get("seo_title", article["title"]),
        "meta_description": article.get("meta_description", ""),
        "keywords": article.get("keywords", []),
        "category": article.get("category", ""),
        "char_count": _content_char_count(full_content),
        "local_path": filepath,
        "wp_link": None,
        "error": None,
    }

    try:
        category_id = resolve_category_id(article.get("category", ""), categories)

        featured_media_id = None
        if include_featured_image and amazon_keyword:
            try:
                image_url = fetch_unsplash_image_url(amazon_keyword)
                if image_url:
                    featured_media_id = upload_featured_image(image_url, article["title"][:40])
            except Exception as exc:
                log(f"アイキャッチ画像の設定に失敗しました(画像なしで投稿します): {exc}")

        if rewrite_post_id is not None:
            link = update_wordpress_post(
                rewrite_post_id, article["title"], full_content, category_id, featured_media_id
            )
            log(f"WordPressの記事を上書き(下書きに変更)しました: {link}")
        else:
            link = post_to_wordpress_draft(
                article["title"], full_content, category_id, featured_media_id
            )
            log(f"WordPressに下書き保存しました: {link}")
        result["wp_link"] = link
    except Exception as exc:
        log(f"WordPressへの保存に失敗しました(ローカル保存のみ完了): {exc}")
        result["error"] = str(exc)

    return result


def run_pipeline(
    topic: str | None = None,
    category: str | None = None,
    include_amazon: bool = True,
    include_featured_image: bool = True,
    log=print,
) -> dict:
    """テーマ(または自由生成)から記事を1本生成し、仕上げ・投稿までを行う。"""
    categories = fetch_categories()
    article = generate_article(categories, topic=topic, category=category, log=log)
    log(f"生成された記事タイトル: {article['title']}")
    return finalize_and_publish(
        article, categories, include_amazon, include_featured_image, log=log
    )


def run_pipeline_from_outline(
    outline: dict,
    include_amazon: bool = True,
    include_featured_image: bool = True,
    log=print,
) -> dict:
    """承認済みのアウトラインから記事を1本生成し、仕上げ・投稿までを行う。"""
    categories = fetch_categories()
    article = generate_article_from_outline(outline, log=log)
    log(f"生成された記事タイトル: {article['title']}")
    return finalize_and_publish(
        article, categories, include_amazon, include_featured_image, log=log
    )


def run_pipeline_rewrite(
    post_id: int,
    category: str | None = None,
    include_amazon: bool = True,
    include_featured_image: bool = True,
    log=print,
) -> dict:
    """既存記事(post_id)をリライトし、同じ投稿を下書きとして上書きする。"""
    categories = fetch_categories()
    existing = fetch_post_for_rewrite(post_id)
    log(f"「{existing['title']}」をリライトしています...")
    article = generate_rewrite(existing["title"], existing["text"], categories, category=category, log=log)
    log(f"リライト後の記事タイトル: {article['title']}")
    return finalize_and_publish(
        article,
        categories,
        include_amazon,
        include_featured_image,
        rewrite_post_id=post_id,
        log=log,
    )


def main() -> None:
    topic = sys.argv[1] if len(sys.argv) > 1 else None
    run_pipeline(topic=topic)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)
