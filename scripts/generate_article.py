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
  UNSPLASH_ACCESS_KEY   (任意) アイキャッチ画像の自動取得に使用
  OPENAI_API_KEY        (任意) 画像生成機能(generate_image_with_openai)に使用
  RAKUTEN_APP_ID        (任意) 楽天市場商品検索APIのアプリケーションID(商品画像・リンクに使用)
  RAKUTEN_ACCESS_KEY    (任意) 楽天市場商品検索APIのアクセスキー(pk_で始まる)
  RAKUTEN_AFFILIATE_ID  (任意) 楽天アフィリエイトID(商品リンクをアフィリエイトにする)

必要なライブラリ: requirements.txt を参照 (pip install -r scripts/requirements.txt)

実行方法:
  python scripts/generate_article.py
"""

import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime

import requests
from dotenv import load_dotenv

from eyecatch import render_eyecatch

load_dotenv()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

# Claude Sonnet 5の料金(1トークンあたり、Anthropic公式レート)
SONNET_5_INPUT_PRICE_PER_MTOK = 2.00
SONNET_5_OUTPUT_PRICE_PER_MTOK = 10.00


class _UsageTracker(threading.local):
    """スレッドごとのAPI利用量を保持する(Webアプリで複数ジョブが並行しても混ざらないように)。"""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0


_usage = _UsageTracker()


def reset_usage() -> None:
    """使用量カウンターをリセットする。1回の記事生成ジョブの開始時に呼ぶ。"""
    _usage.input_tokens = 0
    _usage.output_tokens = 0


def get_usage_summary() -> dict:
    """これまでの(直近reset_usage()以降の)Claude API利用量と概算費用を返す。"""
    cost_usd = (
        _usage.input_tokens / 1_000_000 * SONNET_5_INPUT_PRICE_PER_MTOK
        + _usage.output_tokens / 1_000_000 * SONNET_5_OUTPUT_PRICE_PER_MTOK
    )
    return {
        "input_tokens": _usage.input_tokens,
        "output_tokens": _usage.output_tokens,
        "cost_usd": round(cost_usd, 4),
    }


def sum_usage(*usages: dict | None) -> dict:
    """複数のusage(dict、Noneも可)のトークン数を合算し、概算費用を再計算して返す。

    記事生成の各段階(チャット・タイトル案・構成案・本文生成)は別々に
    get_usage_summary()するため、パイプライン全体の合計を出すのに使う。
    """
    input_tokens = sum((u or {}).get("input_tokens", 0) for u in usages)
    output_tokens = sum((u or {}).get("output_tokens", 0) for u in usages)
    cost_usd = (
        input_tokens / 1_000_000 * SONNET_5_INPUT_PRICE_PER_MTOK
        + output_tokens / 1_000_000 * SONNET_5_OUTPUT_PRICE_PER_MTOK
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 4),
    }

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


MAX_PAUSE_TURN_CONTINUATIONS = 5


def _call_claude(
    messages: list,
    tools: list | None = None,
    max_tokens: int = 12000,
    system: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[str, dict]:
    """Claudeを呼び出し、(テキスト全文, パース済みJSON)を返す。

    toolsを渡すとサーバーサイドツール(Web検索など)を有効にできる。その場合、
    途中でtool_use/tool_resultのブロックが挟まるため、最後のtextブロックを
    最終回答として採用する。Web検索が長引くとAPIが途中で区切って
    stop_reason="pause_turn"を返すので、その場合は同じターンを続けさせる。
    effort("low"等)を下げると、検索回数や考える量が減って速く・安くなる。
    """
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": list(messages),
    }
    if tools:
        payload["tools"] = tools
    if system:
        payload["system"] = system
    if effort:
        payload["output_config"] = {"effort": effort}

    content_blocks = []
    for _ in range(MAX_PAUSE_TURN_CONTINUATIONS + 1):
        response = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            # Web検索などのツールを使う呼び出しは数分かかることがあるため、待ち時間を長くとる
            timeout=600 if tools else 180,
        )
        if not response.ok:
            # 「400 Bad Request」だけでは原因が分からないため、APIが返したエラー内容を表示する
            try:
                message = response.json().get("error", {}).get("message", response.text)
            except ValueError:
                message = response.text
            if "credit balance" in message:
                message = (
                    "Anthropic APIのクレジット残高が不足しています。"
                    "console.anthropic.com の Plans & Billing でクレジットを追加してください。"
                )
            raise RuntimeError(f"Claude APIエラー (HTTP {response.status_code}): {message[:300]}")
        data = response.json()

        usage = data.get("usage", {})
        _usage.input_tokens += usage.get("input_tokens", 0)
        _usage.output_tokens += usage.get("output_tokens", 0)

        content_blocks = data["content"]
        if data.get("stop_reason") != "pause_turn":
            break
        # 途中で区切られたターンは、そこまでの内容をassistantとして送り返すと続きから再開される
        payload["messages"] = payload["messages"] + [{"role": "assistant", "content": content_blocks}]

    text_blocks = [block for block in content_blocks if block.get("type") == "text"]
    if not text_blocks:
        block_types = [b.get("type") for b in content_blocks]
        raise ValueError(
            f"テキスト形式のレスポンスが見つかりませんでした(stop_reason={data.get('stop_reason')}, "
            f"ブロック: {block_types[:10]})。max_tokensが不足している可能性があります"
        )
    # ツール使用時は複数のtextブロックが挟まるため、最後(最終回答)を採用する
    text = text_blocks[-1]["text"].strip()

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
                "[[MID_CONVERSATION]]・[[PRODUCT:数字]]・[[PLACE:数字]]・[[RELATED:数字]]のプレースホルダーは、増やしたり消したりせずそのまま維持してください。"
                "加筆する際も、紹介済みの商品以外に新しい具体的なブランド名・商品名は追加しないでください。"
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


CHAT_SYSTEM_PROMPT = """あなたはおもちゃブログの編集者です。ユーザーと対話しながら、
これから書く記事の方向性を固めるのが仕事です。

最終的に固めたい情報:
- topic: 記事のテーマ・切り口(具体的であるほど良い。店名・エリア・対象年齢・価格帯など
  ユーザーから聞き出せた具体的な事実があれば必ず含める)
- category: カテゴリー(候補: {categories})
- include_amazon: 記事内にAmazon商品紹介を含めるか
- include_image: アイキャッチ画像を自動設定するか

ルール:
- ユーザーの最初のメッセージだけで方向性が十分明確なら、無理に質問を重ねず、すぐready状態にしてよい
- 情報が不足している場合は、一度に1つだけ質問すること
- 質問は3〜4個程度の選択肢(choices)を用意すること。ユーザーは選択肢以外にも自由記述で
  答えられるので、choicesは代表的なものだけでよい
- 2〜3往復程度で十分な情報が集まったら、それ以上質問せずreadyにすること
- 出力は必ず次のJSON形式のみで返すこと。JSON以外の文章やコードブロック記号は含めないこと

質問する場合:
{{"type": "question", "question": "質問文", "choices": ["選択肢1", "選択肢2", "選択肢3"]}}

十分な情報が集まった場合:
{{"type": "ready", "topic": "記事テーマの説明(具体的な事実を含める)", "category": "カテゴリー名",
  "include_amazon": true, "include_image": true,
  "summary": "ユーザーへの確認メッセージ(この内容で記事を作りますね、等)"}}
"""


def chat_step(history: list, categories: list, log=DEFAULT_LOG) -> dict:
    """記事の方向性を固めるためのチャット1ターン分を処理する。

    historyは[{"role": "user"|"assistant", "content": "..."}]の会話履歴
    (最後がユーザーの発言であること)。質問(type=question)か、方向性が
    固まった状態(type=ready)のいずれかを表すdictを返す。
    """
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")
    system = CHAT_SYSTEM_PROMPT.format(categories=category_names or "(カテゴリー未設定)")
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    _, result = _call_claude(messages, system=system, max_tokens=2000)
    log(f"チャット応答: {result.get('type')}")
    return result


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


def research_topic(title: str, target_keyword: str, log=DEFAULT_LOG) -> str:
    """構成案を作る前に、記事に関連する実在の情報をWeb検索で調べておく(事実誤認の予防)。

    店舗名・場所、ブランドの産地/発祥地、具体的な商品名、価格帯など、記事作成の裏付けに
    なる事実を箇条書きで返す。特に見つからなければ空文字列を返す。
    """
    prompt = f"""あなたはリサーチャーです。これから「{title}」というタイトルの記事を書く予定です。
狙うキーワード:「{target_keyword}」

Web検索を使って、この記事に関連する実在の情報(店舗名・場所、ブランドの産地/発祥地、
具体的な商品名、価格帯、発売時期など)を調べてください。不確かな情報は含めないこと。
分からなかった項目は無理に埋めず省略してください。
{SEARCH_LIMIT_NOTE}
記事に駅・空港・商業施設・店舗が出てくる場合は、その公式サイトのフロアマップ(構内図・館内マップ)の
ページや店舗案内ページのURLも、実際に見つかったものだけ記載してください(推測でURLを作らないこと)。

必ず次のJSON形式のみで返してください。他の文章は含めないこと。

{{"facts": "調べて分かった事実の箇条書き(- 形式)。特になければ空文字列"}}
"""
    _, data = _call_claude([{"role": "user", "content": prompt}], tools=WEB_SEARCH_TOOL, max_tokens=8000, effort="medium")
    facts = data.get("facts", "").strip()
    if facts:
        log("事前調査で関連情報を確認しました。")
    else:
        log("事前調査: 特筆すべき情報は見つかりませんでした。")
    return facts


# 構成案(新規作成・リライト共通)で、紹介する商品と場所を計画させる指示とJSONの形。
# 商品は本文を書く前に楽天で実物を確定させ、本文ではその商品だけを具体名で紹介する。
PRODUCT_AND_PLACE_PLAN_INSTRUCTIONS = """## 記事内で紹介する商品(最大6個)
本文で具体的な商品を紹介する箇所をすべて洗い出し、product_mentionsに挙げてください(最大6個、同じ商品の重複なし)。
ここに挙げた商品は、本文を書く前に楽天市場で実在の商品に置き換え、その商品を本文で紹介します。
本文で具体的な商品名を出すのはここに挙げた商品だけになるので、紹介したい商品は漏れなく挙げてください。
ただし駅・空港・店舗の限定品は通販では買えないことが多いため、ここには通販で買える商品を挙げ、
限定品や店舗の情報は(事前調査で確認できたものだけ)outlineの概要に書いてください。
- heading: 紹介する箇所の見出し(outlineのheadingと同じ文字列にすること)
- name: 紹介する商品の種類(例:「木製の型はめパズル」「お風呂で遊べる水鉄砲」)
- search_keyword: 楽天市場で検索するための具体的なキーワード(日本語、2〜4語。ブランド名・商品名が決まっていればそれを含める)

## 記事内で紹介する場所(0〜3個)
駅・空港・商業施設・店舗など、読者が実際に行く場所を紹介する記事なら、その場所をplacesに挙げてください
(該当しなければ空配列)。紹介箇所に地図と写真を載せます。
- heading: 紹介する箇所の見出し(outlineのheadingと同じ文字列)
- name: 表示名(例:「JR札幌駅 西改札」「羽田空港 第1ターミナル」)
- kind: "station" / "airport" / "store" / "other" のいずれか
- map_query: Googleマップで検索してその場所が出る文字列(例:「JR札幌駅」「羽田空港 第1ターミナル」「キデイランド 原宿店」)
- photo_query_en: 写真検索用の英語の施設名(例:「Sapporo Station」「Haneda Airport Terminal 1」)。店舗単体など写真が不要なら空文字列
- floor_map_url: 事前調査で実際に確認できた公式フロアマップ・構内図ページのURL。なければ空文字列(推測で作らないこと)
- official_url: 事前調査で実際に確認できた公式サイト・店舗案内ページのURL。なければ空文字列(推測で作らないこと)"""

PRODUCT_AND_PLACE_JSON_FIELDS = """  "product_mentions": [
    {"heading": "見出し", "name": "商品の種類", "search_keyword": "楽天検索キーワード"}
  ],
  "places": [
    {"heading": "見出し", "name": "場所の表示名", "kind": "station", "map_query": "Googleマップ検索文字列",
     "photo_query_en": "English facility name", "floor_map_url": "", "official_url": ""}
  ]"""


def generate_outline(
    title: str,
    target_keyword: str,
    categories: list,
    category: str | None = None,
    log=DEFAULT_LOG,
) -> dict:
    """承認されたタイトルをもとに、本文を書く前の構成案(アウトライン)を作る。

    構成案を作る前にWeb検索で関連する実在の情報を調べ、それを踏まえて作成することで
    事実誤認(例: 実際は東京発祥のブランドを北海道発と書いてしまう、など)を予防する。
    """
    try:
        facts = research_topic(title, target_keyword, log=log)
    except Exception as exc:
        log(f"事前調査に失敗しました(調査なしで構成案を作成します): {exc}")
        facts = ""
    facts_section = f"\n## 事前調査でわかった事実(参考にすること)\n{facts}\n" if facts else ""

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
{facts_section}
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
事前調査でわかった事実があれば、それを優先して使ってください。ブランドの産地・店舗の場所など
検証可能な事実については、事前調査にない内容を憶測で作らないこと。

{PRODUCT_AND_PLACE_PLAN_INSTRUCTIONS}

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
{PRODUCT_AND_PLACE_JSON_FIELDS}
}}
"""
    _, data = _call_claude([{"role": "user", "content": prompt}])
    return data


def revise_outline(outline: dict, feedback: str, categories: list, log=DEFAULT_LOG) -> dict:
    """既存の構成案(outline)を、ユーザーからの自由記述の修正指示(feedback)に沿って
    部分的に修正し、同じJSON形式で返す。指示されていない部分は変更しない。
    """
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")
    prompt = f"""以下は「おもちゃミュージアム」というブログの、承認前の記事構成案(アウトライン)です。
ユーザーから修正の指示がありました。指示された箇所だけを修正し、それ以外はそのまま維持してください。

## 現在の構成案
{json.dumps(outline, ensure_ascii=False, indent=2)}

## ユーザーからの修正指示
{feedback}

## 制約
- categoryは次のいずれかから選ぶこと(新しいカテゴリー名を作らないこと): {category_names}
- outlineの見出し数は5〜7個を維持すること
- product_mentions(最大6個)とplacesのheadingは、修正後のoutlineのheadingと一致させること
- placesのfloor_map_url・official_urlは、元の構成案にあるもの以外を推測で追加しないこと

修正後の構成案全体を、元と同じJSON形式(title, seo_title, meta_description, keywords, category,
reader_persona, reader_question, yu_answer, mid_question, mid_answer, closing_comment,
amazon_search_keyword, outline, product_mentions, places)で、必ずJSONのみ返してください。
"""
    _, data = _call_claude([{"role": "user", "content": prompt}])
    log("構成案を修正しました。")
    return data


def build_content_prompt_from_outline(outline: dict, reference_text: str | None = None) -> str:
    outline_lines = "\n".join(
        f"- {o['heading']}: {o['summary']}" for o in outline.get("outline", [])
    )
    keywords = "、".join(outline.get("keywords", []))

    product_mentions = outline.get("product_mentions", [])
    if product_mentions:
        product_blocks = []
        for i, p in enumerate(product_mentions):
            item = p.get("item")
            if item:
                price = f"{item['price']:,}円(税込)" if item.get("price") else "不明"
                product_blocks.append(
                    f"[[PRODUCT:{i}]] 見出し「{p['heading']}」で紹介する実在の商品\n"
                    f"  商品カードでの表示名: {item['name']}\n"
                    f"  販売ページの商品名: {item.get('full_name', item['name'])}\n"
                    f"  価格: {price} / ショップ: {item.get('shop', '')} / レビュー: {item.get('review_count', 0)}件\n"
                    f"  商品説明(抜粋): {item.get('caption') or 'なし'}"
                )
            else:
                product_blocks.append(
                    f"[[PRODUCT:{i}]] 見出し「{p['heading']}」で紹介する商品の種類: {p['name']}"
                    "(特定の商品は見つからなかったので、具体的な商品名は出さず種類として紹介する)"
                )
        product_instruction = f"""
## 紹介する商品と商品カードのプレースホルダー(重要)
以下の商品を本文で紹介し、それぞれ本文で最初に触れた直後にプレースホルダー(例: [[PRODUCT:0]])を1回だけ挿入してください。
プレースホルダーは後で画像付きの商品カードに置き換えるので、他の文章とは改行で区切ること。
- 商品は「商品カードでの表示名」で呼ぶこと(本文とカードで名前をそろえるため。販売ページの商品名は検索用の
  キーワードが並んでいるので、そのまま書かない)
- 特徴・仕様は販売ページの商品名と商品説明に書かれている範囲で書くこと(書かれていない機能・素材・対象年齢などを作らない)
- 価格は「◯円前後」程度にとどめること(変動するため)
- ここに挙げた商品以外に、具体的なブランド名・商品名を出さないこと(一般的な種類の話はしてよい)。
  例外として、構成案の概要に書かれている店舗名・限定品名は使ってよい(事前調査で確認済みのため)
- これらは楽天市場(通販)で見つけた商品です。駅・空港・店舗の売り場で売っているとは書かないこと
  (店頭での取り扱いは確認できていないため)。「通販でも手に入る」「事前にネットで用意しておける」
  「似たタイプとして」など、通販の商品であることが分かる紹介のしかたにすること

{chr(10).join(product_blocks)}
"""
    else:
        product_instruction = ""

    places = outline.get("places", [])
    if places:
        place_lines = "\n".join(
            f'- [[PLACE:{i}]] 見出し「{p["heading"]}」で紹介する場所: {p["name"]}'
            for i, p in enumerate(places)
        )
        place_instruction = f"""
## 場所の地図・写真のプレースホルダー
以下の場所について本文で触れた直後(その場所の行き方・場所の説明のあたり)に、プレースホルダーを1回だけ挿入してください。
後で地図と写真に置き換えるので、他の文章とは改行で区切ること。
{place_lines}
"""
    else:
        place_instruction = ""

    related_posts = outline.get("related_posts", [])
    if related_posts:
        related_lines = "\n".join(
            f'- [[RELATED:{i}]] 見出し「{r["heading"]}」で紹介する関連記事: 「{r["title"]}」(共通点: {r.get("reason", "")})'
            for i, r in enumerate(related_posts)
        )
        related_instruction = f"""
## 関連記事(サイト内の別記事)の紹介
以下はこのサイトの既存記事で、内容が共通・類似しています。指定の見出しの中で内容が重なる箇所に、
「◯◯については、こちらの記事で詳しく紹介しています」のように一文で触れ、その直後にプレースホルダーを1回だけ挿入してください。
後で記事へのリンクカードに置き換えるので、他の文章とは改行で区切ること。関連記事の中身を推測で要約・創作しないこと。
{related_lines}
"""
    else:
        related_instruction = ""

    reference_section = ""
    if reference_text:
        reference_section = f"""
## 元記事(リライト元。トピック・店舗名・地名などの事実はできるだけ尊重し、古そうな情報は無難な表現に直すこと)
{reference_text[:6000]}
"""

    return f"""あなたは「おもちゃミュージアム」というブログの専属ライターです。
以下の承認済み構成案に沿って、記事本文を執筆してください。構成案の見出し・流れは変更しないこと。

タイトル:「{outline.get('title', '')}」
メタディスクリプション: {outline.get('meta_description', '')}
SEOキーワード: {keywords}
{reference_section}
## 構成案(この通りの見出し・順序で書くこと)
{outline_lines}
{product_instruction}{place_instruction}{related_instruction}

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


def generate_article_from_outline(
    outline: dict,
    max_expand_attempts: int = 2,
    log=DEFAULT_LOG,
    reference_text: str | None = None,
    exclude_post_id: int | None = None,
) -> dict:
    """承認済みのアウトラインに沿って本文を生成し、タイトル等の固定フィールドと結合する。

    本文を書く前に、紹介予定の商品を楽天で実在の商品に確定させ、その商品情報をもとに書かせる。
    """
    outline = dict(outline)
    mentions = outline.get("product_mentions") or []
    if mentions and not any("item" in m for m in mentions):
        log(f"紹介する商品({len(mentions[:MAX_PRODUCT_MENTIONS])}件)を楽天市場で探しています...")
        outline["product_mentions"] = resolve_product_mentions(mentions, log=log)

    if "related_posts" not in outline:
        log("内容が共通・類似しているサイト内の記事を探しています...")
        try:
            outline["related_posts"] = select_related_posts(outline, exclude_post_id=exclude_post_id, log=log)
        except Exception as exc:
            log(f"関連記事の選定に失敗しました(関連記事の紹介なしで書きます): {exc}")
            outline["related_posts"] = []

    messages = [{"role": "user", "content": build_content_prompt_from_outline(outline, reference_text)}]
    result = _generate_with_expansion(messages, max_expand_attempts, log)

    article = dict(outline)
    article["content"] = result["content"]
    return article


# ---------------------------------------------------------------------------
# 既存記事のリライト
# ---------------------------------------------------------------------------

def build_rewrite_prompt(existing_title: str, existing_text: str, categories: list, category: str | None = None) -> str:
    """リライトの構成案(新規作成の構成案と同じ形式)を作らせるプロンプト。本文は別ステップで書く。"""
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")
    if category:
        category_instruction = f'"category"には必ず次の値をそのまま使うこと:「{category}」'
    else:
        category_instruction = (
            "このブログの既存カテゴリーの中から、記事に最も合うものを1つだけ選んでください"
            f"(新しいカテゴリー名を作らないこと): {category_names}"
        )
    excerpt = existing_text[:6000]

    return f"""あなたは「おもちゃミュージアム」というブログの編集者です。
以下は現在サイトに掲載されている記事です。この記事を最新のハウススタイルでリライトするための構成案を作ってください。
本文はこの後、別の工程で構成案に沿って書きます。

## 元記事
タイトル:「{existing_title}」
本文(参考、HTMLタグは除去済み):
{excerpt}

## リライトの方針
- 元記事のトピック・具体的な事実(店舗名、商品名、地名等)はできるだけ尊重すること
- 情報が古くなっていそうな部分(価格、流行、時期の記述等)は一般的で無難な表現にする前提で構成すること
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

## アウトライン
5〜7個の見出し(h2)と、それぞれ何を書くかの概要(1〜2文)。全体で本文5000文字以上になるボリューム感にすること。

{PRODUCT_AND_PLACE_PLAN_INSTRUCTIONS}
(元記事に出てくる公式サイト等のURLがあれば、floor_map_url・official_urlに使ってよい)

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
  "amazon_search_keyword": "記事全体を総括するおすすめ商品の検索キーワード",
  "outline": [
    {{"heading": "見出し1", "summary": "このセクションで書く内容の概要"}}
  ],
{PRODUCT_AND_PLACE_JSON_FIELDS}
}}
"""


def generate_rewrite(
    existing_title: str,
    existing_text: str,
    categories: list,
    category: str | None = None,
    max_expand_attempts: int = 2,
    log=DEFAULT_LOG,
    exclude_post_id: int | None = None,
) -> dict:
    """既存記事を参考に、ハウススタイルへリライトした記事を生成する。

    新規作成と同じく「構成案(紹介する商品・場所を含む)→ 楽天で実商品を確定 → 本文執筆」の順で進める。
    """
    _, plan = _call_claude(
        [{"role": "user", "content": build_rewrite_prompt(existing_title, existing_text, categories, category)}]
    )
    log(f"リライトの構成案を作成しました(見出し{len(plan.get('outline', []))}個)")
    return generate_article_from_outline(
        plan, max_expand_attempts=max_expand_attempts, log=log, reference_text=existing_text,
        exclude_post_id=exclude_post_id,
    )


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


# ---------------------------------------------------------------------------
# 楽天市場商品検索API(2026年の仕様変更後: openapi.rakuten.co.jp、applicationId+accessKeyが必須)
# ---------------------------------------------------------------------------
# 楽天の商品画像はショップの著作物のため、WordPressにはアップロードせず
# 楽天の画像URLをそのまま表示する(商品ページへのリンクとセットで使う)。

RAKUTEN_ITEM_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
RAKUTEN_MIN_INTERVAL_SEC = 1.1  # アプリ登録時のQPSが1のため
_rakuten_lock = threading.Lock()
_rakuten_last_call = 0.0


def rakuten_configured() -> bool:
    return bool(os.environ.get("RAKUTEN_APP_ID") and os.environ.get("RAKUTEN_ACCESS_KEY"))


def clean_rakuten_item_name(name: str, max_chars: int = 45) -> str:
    """楽天の商品名から【クーポン】★送料無料★のような宣伝文句を除き、表示用に短くする。"""
    name = re.sub(r"【[^】]*】|\[[^\]]*\]|［[^］]*］|★[^★]*★|◆[^◆]*◆|＼[^／]*／", " ", name or "")
    name = re.sub(
        r"お買い物マラソン|スーパーSALE|ポイント\s*\d+倍[！!]?|P\d+倍[！!]?|\d+%\s*OFF|\d+%off|"
        r"クーポン\S*|送料無料[！!]?|楽天\d位(受賞)?|ギフト無料|あす楽",
        " ",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_chars:
        name = name[:max_chars].rstrip() + "…"
    return name


RAKUTEN_TOY_GENRE_ID = 566382  # 楽天市場「おもちゃ」ジャンル
RAKUTEN_MIN_REVIEWS = 3


def _rakuten_request(keyword: str, genre_id: int | None) -> list:
    global _rakuten_last_call
    params = {
        "applicationId": os.environ["RAKUTEN_APP_ID"],
        "accessKey": os.environ["RAKUTEN_ACCESS_KEY"],
        "affiliateId": os.environ.get("RAKUTEN_AFFILIATE_ID", ""),
        "keyword": keyword,
        "hits": 10,
        "imageFlag": 1,
        "availability": 1,
        "formatVersion": 2,
    }
    if genre_id:
        params["genreId"] = genre_id
    with _rakuten_lock:
        wait = RAKUTEN_MIN_INTERVAL_SEC - (time.time() - _rakuten_last_call)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(
            RAKUTEN_ITEM_SEARCH_URL,
            params=params,
            headers={"Referer": os.environ.get("WP_URL", "https://www.omotya-museum.com").rstrip("/") + "/"},
            timeout=30,
        )
        _rakuten_last_call = time.time()

    if not response.ok:
        raise RuntimeError(f"楽天APIエラー (HTTP {response.status_code}): {response.text[:300]}")
    return response.json().get("Items", [])


def search_rakuten_items(keyword: str, hits: int = 3) -> list:
    """楽天市場で商品を検索し、画像付きの商品をhits件返す。

    まず「おもちゃ」ジャンルで探し、なければ全ジャンルで探す。並び順は楽天の関連度順を
    基本とし(レビュー件数で並べ替えると収納グッズなど関係の薄い人気商品が上位に来るため)、
    その中でレビューが一定数ある商品を優先する。
    """
    raw_items = _rakuten_request(keyword, RAKUTEN_TOY_GENRE_ID) or _rakuten_request(keyword, None)

    items = []
    for it in raw_items:
        images = it.get("mediumImageUrls") or []
        if not images:
            continue
        image = images[0] if isinstance(images[0], str) else images[0].get("imageUrl", "")
        items.append({
            "name": clean_rakuten_item_name(it.get("itemName", "")),
            "price": it.get("itemPrice"),
            "url": it.get("affiliateUrl") or it.get("itemUrl"),
            "image": re.sub(r"\?_ex=\d+x\d+", "?_ex=300x300", image),
            "shop": it.get("shopName", ""),
            "review_average": it.get("reviewAverage") or 0,
            "review_count": it.get("reviewCount") or 0,
            "full_name": it.get("itemName", ""),
            "caption": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", it.get("itemCaption") or "")).strip()[:300],
        })
    # 関連度の高い上位5件の中でだけレビューありを優先する(下位の人気商品が割り込まないように)
    head, tail = items[:5], items[5:]
    head = [i for i in head if i["review_count"] >= RAKUTEN_MIN_REVIEWS] + [
        i for i in head if i["review_count"] < RAKUTEN_MIN_REVIEWS
    ]
    return (head + tail)[:hits]


def _amazon_search_url(keyword: str) -> str:
    tag = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
    url = f"https://www.amazon.co.jp/s?k={urllib.parse.quote(keyword)}"
    if tag:
        url += f"&tag={urllib.parse.quote(tag)}"
    return url


def build_rakuten_card_html(item: dict, amazon_keyword: str | None = None) -> str:
    """楽天の商品1件ぶんのカード(画像・価格・楽天ボタン+Amazon検索ボタン)。"""
    name = html.escape(item["name"])
    price = f"{item['price']:,}円(税込)" if item.get("price") else ""
    review = ""
    if item.get("review_count"):
        review = f"★{item['review_average']:.1f}({item['review_count']:,}件)"
    amazon_button = ""
    if amazon_keyword:
        amazon_button = (
            f'<a rel="nofollow noopener sponsored" href="{html.escape(_amazon_search_url(amazon_keyword))}" target="_blank" '
            'style="display:inline-block;background:#ff9900;color:#fff;font-weight:700;padding:6px 16px;'
            'border-radius:6px;text-decoration:none;font-size:0.85rem;margin:4px 0;">▶ Amazonで探す</a>'
        )
    return f"""<div style="border:1px solid #ddd;border-radius:10px;padding:12px;margin:16px 0;display:flex;gap:14px;align-items:center;background:#fafafa;flex-wrap:wrap;">
<a rel="nofollow noopener sponsored" href="{html.escape(item['url'])}" target="_blank" style="flex-shrink:0;"><img src="{html.escape(item['image'])}" alt="{name}" style="width:120px;height:120px;object-fit:contain;border-radius:6px;background:#fff;"></a>
<div style="flex:1;min-width:180px;">
<p style="font-weight:bold;margin:0 0 4px;font-size:0.92rem;">{name}</p>
<p style="margin:0 0 8px;font-size:0.82rem;color:#555;">{price} {review}</p>
<a rel="nofollow noopener sponsored" href="{html.escape(item['url'])}" target="_blank" style="display:inline-block;background:#bf0000;color:#fff;font-weight:700;padding:6px 16px;border-radius:6px;text-decoration:none;font-size:0.85rem;margin:4px 6px 4px 0;">▶ 楽天市場で見る</a>
{amazon_button}
<p style="margin:6px 0 0;font-size:0.72rem;color:#999;">※価格は記事作成時点のものです</p>
</div>
</div>"""


def build_inline_product_card_html(name: str, search_keyword: str, log=DEFAULT_LOG) -> str:
    """本文中に挿入する、1商品ぶんの小さな紹介カード(画像+購入リンク)を組み立てる。

    Amazon Creators APIで実商品が取れればその画像・リンクを使う。取れない場合は
    楽天市場の商品(画像・価格・楽天リンク+Amazon検索リンク)、それも無理なら
    Unsplashの画像(あれば)+Amazon検索リンクにフォールバックする。
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
        log(f"商品「{name}」のAmazon検索に失敗しました(楽天にフォールバックします): {exc}")

    if rakuten_configured():
        try:
            items = search_rakuten_items(search_keyword, hits=1)
            if items:
                log(f"商品「{name}」: 楽天市場の「{items[0]['name']}」を紹介します")
                return build_rakuten_card_html(items[0], amazon_keyword=search_keyword)
            log(f"商品「{name}」: 楽天市場で該当商品が見つかりませんでした")
        except Exception as exc:
            log(f"商品「{name}」の楽天検索に失敗しました: {exc}")

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


MAX_RELATED_POSTS = 3


def fetch_published_posts_brief() -> list:
    """関連記事選び用に、公開済みの記事一覧(タイトル・URL・抜粋)を取得する。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    posts, page = [], 1
    while True:
        response = requests.get(
            f"{wp_url}/wp-json/wp/v2/posts",
            params={"per_page": 100, "page": page, "status": "publish", "_fields": "id,title,link,excerpt"},
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        for p in response.json():
            excerpt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", (p.get("excerpt") or {}).get("rendered", ""))).strip()
            posts.append({
                "id": p["id"],
                "title": html.unescape(p["title"]["rendered"]),
                "link": p["link"],
                "excerpt": html.unescape(excerpt)[:120],
            })
        if page >= int(response.headers.get("X-WP-TotalPages", "1") or 1):
            break
        page += 1
    return posts


def select_related_posts(outline: dict, exclude_post_id: int | None = None, log=DEFAULT_LOG) -> list:
    """これから書く記事と内容が共通・類似している既存記事を最大3件選び、紹介する見出しを決める(内部リンク用)。"""
    posts = [p for p in fetch_published_posts_brief() if p["id"] != exclude_post_id]
    if not posts:
        return []
    headings = [o["heading"] for o in outline.get("outline", [])]
    listing = "\n".join(f"{i}: {p['title']} / {p['excerpt']}" for i, p in enumerate(posts))
    _, data = _call_claude(
        [{"role": "user", "content": (
            "これから次の記事を書きます。サイト内の既存記事の中から、内容が共通・類似していて、読者が"
            f"あわせて読むと役立つ記事を最大{MAX_RELATED_POSTS}件選んでください。"
            "テーマが本当に重なるものだけを選び、無理に選ばないこと(なければ空配列)。\n\n"
            f"## これから書く記事\nタイトル: {outline.get('title', '')}\n見出し: {' / '.join(headings)}\n\n"
            f"## 既存記事(番号: タイトル / 抜粋)\n{listing}\n\n"
            "それぞれ、紹介するのに最も合う見出し(上の見出しと同じ文字列)と、共通点を一言で添えてください。\n"
            '{"related": [{"index": 番号, "heading": "見出し", "reason": "共通点"}]} のJSONだけを返してください。'
        )}],
        max_tokens=800,
        model=LIGHT_MODEL,
    )
    related, seen = [], set()
    for r in data.get("related") or []:
        try:
            post = posts[int(r.get("index"))]
        except (TypeError, ValueError, IndexError):
            continue
        if post["id"] in seen:
            continue
        seen.add(post["id"])
        heading = r.get("heading") if r.get("heading") in headings else (headings[-1] if headings else "")
        related.append({**post, "heading": heading, "reason": (r.get("reason") or "").strip()})
    for r in related[:MAX_RELATED_POSTS]:
        log(f"関連記事として紹介: {r['title']}(共通点: {r['reason']})")
    if not related:
        log("内容が重なる既存記事は見つかりませんでした(関連記事の紹介なし)")
    return related[:MAX_RELATED_POSTS]


def build_related_post_card_html(post: dict) -> str:
    """本文中に入れる「あわせて読みたい」の内部リンクカード。"""
    return (
        '<div style="border:1px solid #e5e7eb;border-left:4px solid #ff6600;border-radius:6px;'
        'padding:10px 14px;margin:16px 0;background:#fffaf5;">'
        '<span style="display:inline-block;font-size:0.75rem;font-weight:bold;color:#ff6600;margin-bottom:4px;">あわせて読みたい</span><br>'
        f'<a href="{html.escape(post["link"])}" style="font-weight:bold;text-decoration:none;">{html.escape(post["title"])}</a>'
        "</div>"
    )


MAX_PRODUCT_MENTIONS = 6


def resolve_product_mentions(mentions: list, log=DEFAULT_LOG) -> list:
    """構成案で予定した商品(種類・検索キーワード)を、楽天で実在の商品に置き換える。

    本文を書く前に実商品を確定させ、その商品情報をもとに本文を書かせることで、
    本文の説明と商品カードの中身がずれないようにする。同じ商品が重複しないようにし、
    見つからなかった枠はitem=Noneのまま残す(本文では一般的な種類として扱う)。
    """
    resolved = []
    used_urls = set()
    for mention in (mentions or [])[:MAX_PRODUCT_MENTIONS]:
        mention = dict(mention)
        mention["item"] = None
        if rakuten_configured() and mention.get("search_keyword"):
            try:
                for item in search_rakuten_items(mention["search_keyword"], hits=3):
                    if item["url"] not in used_urls:
                        mention["item"] = item
                        used_urls.add(item["url"])
                        break
            except Exception as exc:
                log(f"商品「{mention.get('name', '')}」の楽天検索に失敗しました: {exc}")
        resolved.append(mention)

    _add_display_names([m["item"] for m in resolved if m["item"]])
    for mention in resolved:
        if mention["item"]:
            log(f"紹介する商品を確定: {mention.get('name', '')} → {mention['item']['name']}")
        else:
            log(f"商品「{mention.get('name', '')}」は楽天で見つからなかったため、種類の紹介にとどめます")
    return resolved


def _add_display_names(items: list) -> None:
    """楽天の商品名(検索用キーワードの羅列)を、読者向けの短い名前に置き換える(1回の軽量モデル呼び出し)。"""
    if not items:
        return
    listing = "\n".join(f"{i}: {it.get('full_name') or it['name']} / ショップ: {it.get('shop', '')}" for i, it in enumerate(items))
    try:
        _, data = _call_claude(
            [{"role": "user", "content": (
                "以下は楽天市場の商品名です。検索用のキーワードが並んでいて読みにくいので、ブログの商品カードに載せる"
                "短く自然な日本語の商品名(全角25文字以内)にしてください。ブランド名があれば先頭に入れ、"
                "セール・クーポン・ポイント・送料・出産祝いなどの宣伝文句や用途の羅列は除くこと。"
                "商品名にない特徴を付け足さないこと。\n"
                f"{listing}\n"
                '{"names": ["0番の名前", "1番の名前", ...]} のJSONだけを返してください。'
            )}],
            max_tokens=800,
            model=LIGHT_MODEL,
        )
        names = data.get("names") or []
    except Exception:
        return
    for item, name in zip(items, names):
        name = (name or "").strip()
        if name:
            item["name"] = name[:30]


# ---------------------------------------------------------------------------
# 場所(駅・空港・店舗など)の紹介ブロック: Googleマップ埋め込み + 写真(Wikimedia Commons) + 公式案内へのリンク
# ---------------------------------------------------------------------------
# 公式サイトのフロアマップや店舗写真は各社の著作物のため転載せず、公式ページへのリンクにとどめる。
# 写真はWikimedia Commonsの自由ライセンス画像のみ使い、撮影者・ライセンスを表記する。

WIKIMEDIA_UA = {"User-Agent": "omotya-museum-article-tool/1.0 (https://www.omotya-museum.com)"}


def _url_is_alive(url: str) -> bool:
    """URLが実在するか(AIが作った架空のURLでないか)を確かめる。

    存在しないドメイン・404などは不可。自動アクセスだけを拒否するサイト(403/429等)は実在するとみなす。
    """
    if not url.startswith(("http://", "https://")):
        return False
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_USER_AGENT}, timeout=15, allow_redirects=True)
        return r.status_code < 400 or r.status_code in (401, 403, 405, 429)
    except Exception:
        return False


def fetch_commons_photo(query: str, place_name: str, log=DEFAULT_LOG) -> dict | None:
    """Wikimedia Commonsから場所の写真を探し、軽量モデルに最も適した1枚を選ばせる。"""
    r = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 8,
            "prop": "imageinfo", "iiprop": "url|extmetadata|mime", "iiurlwidth": 800,
        },
        headers=WIKIMEDIA_UA,
        timeout=30,
    )
    r.raise_for_status()
    pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))

    candidates = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        license_name = (meta.get("LicenseShortName") or {}).get("value", "")
        if not license_name or "fair use" in license_name.lower() or not info.get("thumburl"):
            continue
        strip = lambda v: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", v or "")).strip()  # noqa: E731
        candidates.append({
            "title": p["title"],
            "description": strip((meta.get("ImageDescription") or {}).get("value"))[:150],
            "date": strip((meta.get("DateTimeOriginal") or {}).get("value"))[:20],
            "url": info["thumburl"],
            "page_url": info.get("descriptionurl", ""),
            "artist": strip((meta.get("Artist") or {}).get("value"))[:60] or "不明",
            "license": license_name,
            "license_url": (meta.get("LicenseUrl") or {}).get("value", ""),
        })
    if not candidates:
        return None

    listing = "\n".join(
        f"{i}: {c['title']} / {c['description']} / 撮影日: {c['date'] or '不明'}" for i, c in enumerate(candidates)
    )
    try:
        _, data = _call_claude(
            [{"role": "user", "content": (
                f"ブログ記事で「{place_name}」を紹介する箇所に載せる写真を選びます。"
                "現在の施設の様子(外観・構内・ターミナルなど)が分かる写真を1枚選んでください。"
                "古い時代の写真、食べ物や無関係な物のアップ、別の場所の写真は選ばないこと。\n"
                f"{listing}\n"
                '適切なものがなければ-1。{"index": 番号} のJSONだけを返してください。'
            )}],
            max_tokens=100,
            model=LIGHT_MODEL,
        )
        index = int(data.get("index", -1))
    except Exception as exc:
        log(f"写真の選定に失敗しました(先頭の候補を使います): {exc}")
        index = 0
    if index < 0 or index >= len(candidates):
        return None
    return candidates[index]


def build_place_block_html(place: dict, log=DEFAULT_LOG) -> str:
    """場所1つぶんの紹介ブロック(写真・地図・公式ページへのリンク)を組み立てる。"""
    name = place.get("name", "")
    parts = []

    photo = None
    if place.get("photo_query_en"):
        try:
            photo = fetch_commons_photo(place["photo_query_en"], name, log=log)
        except Exception as exc:
            log(f"「{name}」の写真検索に失敗しました: {exc}")
    if photo:
        license_html = (
            f'<a href="{html.escape(photo["license_url"])}" rel="noopener" target="_blank">{html.escape(photo["license"])}</a>'
            if photo["license_url"] else html.escape(photo["license"])
        )
        parts.append(
            f'<figure style="margin:0 0 12px;"><img src="{html.escape(photo["url"])}" alt="{html.escape(name)}" '
            'style="width:100%;height:auto;border-radius:8px;" loading="lazy">'
            f'<figcaption style="font-size:0.72rem;color:#888;margin-top:4px;">写真: {html.escape(photo["artist"])} / '
            f'{license_html} / <a href="{html.escape(photo["page_url"])}" rel="noopener" target="_blank">Wikimedia Commons</a></figcaption></figure>'
        )
        log(f"「{name}」の写真を掲載します(Wikimedia Commons: {photo['title']})")

    map_query = place.get("map_query") or name
    if map_query:
        zoom = 18 if place.get("kind") in ("station", "airport") else 17
        map_src = f"https://maps.google.com/maps?q={urllib.parse.quote(map_query)}&z={zoom}&output=embed"
        parts.append(
            f'<iframe src="{html.escape(map_src)}" width="100%" height="320" style="border:0;border-radius:8px;" '
            f'loading="lazy" referrerpolicy="no-referrer-when-downgrade" title="{html.escape(name)}の地図"></iframe>'
        )

    links = []
    for label, key in (("公式フロアマップ・構内図を見る", "floor_map_url"), ("公式サイトを見る", "official_url")):
        url = (place.get(key) or "").strip()
        if url and url not in [u for _, u in links] and _url_is_alive(url):
            links.append((label, url))
        elif url:
            log(f"「{name}」の{label}のURLにアクセスできなかったため掲載しません: {url}")
    if links:
        parts.append("".join(
            f'<a href="{html.escape(url)}" rel="noopener" target="_blank" style="display:inline-block;background:#2563eb;color:#fff;'
            f'font-weight:700;padding:6px 16px;border-radius:6px;text-decoration:none;font-size:0.85rem;margin:8px 8px 0 0;">▶ {label}</a>'
            for label, url in links
        ))

    if not parts:
        return ""
    return (
        '<div style="border:1px solid #e5e7eb;border-radius:10px;padding:14px;margin:16px 0;background:#fafafa;">'
        f'<p style="font-weight:bold;margin:0 0 10px;">📍 {html.escape(name)}</p>{"".join(parts)}</div>'
    )


def resolve_category_id(category_name: str, categories: list) -> int | None:
    for c in categories:
        if c["name"] == category_name:
            return c["id"]
    return None


# Unsplash APIの利用規約では、写真を使う際に撮影者とUnsplashへのクレジット表記(UTMパラメータ付きリンク)と、
# 写真を保存するときのダウンロード通知(download_locationへのアクセス)が求められる。
UNSPLASH_UTM = "utm_source=omotya_museum_article_tool&utm_medium=referral"


def _unsplash_photo_info(photo: dict, keyword: str) -> dict:
    user = photo.get("user") or {}
    return {
        "url": photo["urls"]["regular"],
        "photographer": user.get("name") or user.get("username") or "Unsplash",
        "photographer_url": f"{(user.get('links') or {}).get('html', 'https://unsplash.com')}?{UNSPLASH_UTM}",
        "download_location": (photo.get("links") or {}).get("download_location"),
        "alt": photo.get("alt_description") or keyword,
    }


def fetch_unsplash_photo(keyword: str, choose_for: str | None = None) -> dict | None:
    """Unsplashから記事テーマに合う写真を1枚探す。キーがなければNoneを返す。

    choose_forに記事タイトルを渡すと、検索結果の上位8枚の説明文を見て、
    記事に最も合う1枚を軽量モデルに選ばせる(先頭の1枚は記事とずれていることがあるため)。
    返り値: {"url", "photographer", "photographer_url", "download_location", "alt"}
    """
    access_key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not access_key:
        return None

    response = requests.get(
        "https://api.unsplash.com/search/photos",
        params={"query": keyword, "per_page": 8 if choose_for else 1, "orientation": "landscape"},
        headers={"Authorization": f"Client-ID {access_key}"},
        timeout=30,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return None
    if not choose_for or len(results) == 1:
        return _unsplash_photo_info(results[0], keyword)

    listing = "\n".join(
        f"{i}: {p.get('alt_description') or ''} / {p.get('description') or ''}"[:200] for i, p in enumerate(results)
    )
    try:
        _, data = _call_claude(
            [{"role": "user", "content": (
                f"ブログ記事「{choose_for}」のアイキャッチに使う写真を選びます。"
                "写真の左側にはタイトル文字を重ねるので、記事のテーマ(おもちゃ・子ども・場所など)が"
                "伝わる写真を1枚選んでください。記事と関係の薄い物(アクセサリー、無関係な外国の文字が目立つ物など)は避けること。\n"
                f"{listing}\n"
                '{"index": 番号} のJSONだけを返してください。'
            )}],
            max_tokens=100,
            model=LIGHT_MODEL,
        )
        index = int(data.get("index", 0))
    except Exception:
        index = 0
    if not 0 <= index < len(results):
        index = 0
    return _unsplash_photo_info(results[index], keyword)


def unsplash_credit_html(photo: dict) -> str:
    return (
        f'Photo by <a href="{html.escape(photo["photographer_url"])}" rel="noopener" target="_blank">'
        f'{html.escape(photo["photographer"])}</a> on '
        f'<a href="https://unsplash.com/?{UNSPLASH_UTM}" rel="noopener" target="_blank">Unsplash</a>'
    )


def fetch_unsplash_image_url(keyword: str) -> str | None:
    """Unsplashから記事テーマに合う画像のURLを1枚取得する(Unsplash上の画像をそのまま表示する用途)。"""
    photo = fetch_unsplash_photo(keyword)
    return photo["url"] if photo else None


LIGHT_MODEL = "claude-haiku-4-5-20251001"


def build_unsplash_query(title: str, keyword: str) -> str:
    """Unsplashは英語の検索の方が精度が高いため、記事タイトルから英語の検索語を作る。"""
    try:
        _, data = _call_claude(
            [{"role": "user", "content": (
                "次のブログ記事のアイキャッチ写真をストックフォトサイトで探します。"
                "記事の内容に合う写真が見つかりやすい、短い英語の検索語(2〜4語)を考えてください。"
                "人物の顔のアップより、おもちゃや遊んでいる様子が写る写真が望ましいです。\n"
                f"記事タイトル: {title}\n関連キーワード: {keyword}\n"
                '必ず {"query": "..."} のJSONだけを返してください。'
            )}],
            max_tokens=200,
            model=LIGHT_MODEL,
        )
        return (data.get("query") or "").strip() or keyword
    except Exception:
        return keyword


def split_title_lines(text: str, max_chars: int, max_lines: int) -> list | None:
    """アイキャッチに載せるタイトルの改行位置を決める(単語の途中で区切らないよう軽量モデルに任せる)。"""
    _, data = _call_claude(
        [{"role": "user", "content": (
            f"次の日本語のタイトルを、画像に載せるため{max_lines}行以内(できるだけ少ない行数)に分けてください。"
            f"1行は全角{max_chars}文字以内を目安にし、単語の途中では絶対に区切らないこと"
            "(「ぬいぐるみ」「おもちゃ」などを分断しない)。文字は一切変えないこと。\n"
            f"タイトル: {text}\n"
            '{"lines": ["1行目", "2行目"]} のJSONだけを返してください。'
        )}],
        max_tokens=300,
        model=LIGHT_MODEL,
    )
    lines = [str(line) for line in (data.get("lines") or []) if line]
    return lines if "".join(lines) == text else None


def set_featured_image_from_unsplash(keyword: str, title: str, log=DEFAULT_LOG) -> int | None:
    """Unsplashの写真をWordPressにアップロードしてアイキャッチ用のメディアIDを返す。

    規約に沿って、ダウンロード通知を送り、メディアのキャプションに撮影者のクレジットを入れる。
    """
    query = build_unsplash_query(title, keyword)
    log(f"アイキャッチ画像をUnsplashで検索しています(検索語: {query})...")
    photo = fetch_unsplash_photo(query, choose_for=title) or (
        fetch_unsplash_photo(keyword, choose_for=title) if query != keyword else None
    )
    if not photo:
        log(f"Unsplashで「{keyword}」の写真が見つかりませんでした(アイキャッチなしで保存します)")
        return None

    if photo.get("download_location"):
        try:
            requests.get(
                photo["download_location"],
                headers={"Authorization": f"Client-ID {os.environ['UNSPLASH_ACCESS_KEY']}"},
                timeout=30,
            )
        except Exception as exc:
            log(f"Unsplashへのダウンロード通知に失敗しました(処理は続けます): {exc}")

    # ファイル名は英数字のみ使えるため、英語の検索語から作る(例: eyecatch-bath-toys-water-play)
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40] or "photo"
    image_response = requests.get(photo["url"], headers={"User-Agent": BROWSER_USER_AGENT}, timeout=60)
    image_response.raise_for_status()
    image_bytes = image_response.content
    try:
        # 写真の左側に白いパネルを重ね、記事タイトルを載せたアイキャッチにする
        image_bytes = render_eyecatch(image_bytes, title, line_splitter=split_title_lines)
    except Exception as exc:
        log(f"アイキャッチへのタイトル合成に失敗しました(写真のみで設定します): {exc}")
    media_id = upload_image_bytes_to_wp(image_bytes, f"eyecatch-{slug}.jpg", "image/jpeg")["id"]
    try:
        requests.post(
            f"{os.environ['WP_URL'].rstrip('/')}/wp-json/wp/v2/media/{media_id}",
            auth=(os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"]),
            headers={"User-Agent": BROWSER_USER_AGENT},
            json={"caption": unsplash_credit_html(photo), "alt_text": photo["alt"]},
            timeout=30,
        ).raise_for_status()
    except Exception as exc:
        log(f"アイキャッチ画像のクレジット表記の設定に失敗しました(メディアライブラリで手動設定してください): {exc}")
    log(f"アイキャッチ画像を設定しました(Photo by {photo['photographer']} on Unsplash)")
    return media_id


def replace_featured_image(post_id: int, log=DEFAULT_LOG) -> dict:
    """既存記事のアイキャッチだけを作り直して差し替える(本文・タイトル・公開状態・URLは変更しない)。

    返り値: {"title", "link", "image_url"}
    """
    wp_url = os.environ["WP_URL"].rstrip("/")
    auth = (os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"])
    post = requests.get(
        f"{wp_url}/wp-json/wp/v2/posts/{post_id}",
        auth=auth,
        params={"context": "edit", "_fields": "id,title,link"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    post.raise_for_status()
    post = post.json()
    title = html.unescape(post["title"].get("raw") or post["title"].get("rendered", ""))
    log(f"「{title}」のアイキャッチを作り直しています...")

    media_id = set_featured_image_from_unsplash(title, title, log=log)
    if not media_id:
        raise RuntimeError("記事に合う写真が見つからなかったため、アイキャッチを差し替えられませんでした")

    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/posts/{post_id}",
        auth=auth,
        params={"_fields": "id,link,featured_media"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        json={"featured_media": media_id},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"アイキャッチの差し替えに失敗しました (HTTP {response.status_code}): {response.text[:300]}")

    media = requests.get(
        f"{wp_url}/wp-json/wp/v2/media/{media_id}",
        params={"_fields": "source_url"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    image_url = media.json().get("source_url") if media.ok else None
    log("アイキャッチを差し替えました(本文・公開状態は変更していません)")
    return {"title": title, "link": response.json().get("link") or post.get("link"), "image_url": image_url}


def upload_image_bytes_to_wp(image_bytes: bytes, filename: str, mime_type: str = "image/jpeg") -> dict:
    """画像バイト列をWordPressメディアライブラリにアップロードし、{"id":.., "url":..}を返す。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    # HTTPヘッダーは英数字しか送れないため、日本語などを含むファイル名は英数字に置き換える
    stem, _, ext = filename.rpartition(".")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem if ext else filename).strip("-")
    filename = f"{stem or 'image-' + datetime.now().strftime('%Y%m%d%H%M%S')}.{ext or 'jpg'}"

    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/media",
        auth=(username, app_password),
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": mime_type,
        },
        data=image_bytes,
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"画像のアップロードに失敗しました (HTTP {response.status_code}): {response.text}"
        )
    media = response.json()
    return {"id": media.get("id"), "url": media.get("source_url")}


def generate_image_with_openai(prompt: str, size: str = "1024x1024") -> bytes:
    """OpenAIの画像生成API(gpt-image-1)で画像を生成し、画像バイト列(PNG)を返す。

    OPENAI_API_KEYが必要。OpenAI側のAPI仕様変更により動作しない場合は、
    .envのOPENAI_API_KEYやモデル名の見直しが必要な場合がある。
    """
    api_key = os.environ["OPENAI_API_KEY"]
    response = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "gpt-image-1", "prompt": prompt, "size": size, "n": 1},
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(
            f"OpenAI画像生成に失敗しました (HTTP {response.status_code}): {response.text}"
        )
    item = response.json()["data"][0]
    if item.get("b64_json"):
        import base64

        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        image_response = requests.get(item["url"], timeout=60)
        image_response.raise_for_status()
        return image_response.content
    raise RuntimeError("OpenAIの応答から画像データを取得できませんでした")


def insert_image_after_heading(content: str, heading_index: int, image_url: str, alt_text: str = "") -> str:
    """content内のheading_index番目(0始まり)のh2見出しの直後に画像を挿入する。
    見出しが見つからない場合は本文末尾に追加する。
    """
    img_tag = f'\n<img src="{image_url}" alt="{alt_text}" style="max-width:100%;height:auto;">\n'
    matches = list(re.finditer(r"<h2[^>]*>.*?</h2>", content, re.DOTALL))
    if heading_index < 0 or heading_index >= len(matches):
        return content + img_tag
    insert_pos = matches[heading_index].end()
    return content[:insert_pos] + img_tag + content[insert_pos:]


def post_to_wordpress_draft(
    title: str, content: str, category_id: int | None, featured_media_id: int | None = None
) -> tuple[str, int]:
    """下書きとして投稿し、(記事URL, post_id)を返す。"""
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
    return post.get("link") or f"post id {post.get('id')}", post.get("id")


def update_wordpress_post(
    post_id: int,
    title: str,
    content: str,
    category_id: int | None,
    featured_media_id: int | None = None,
) -> tuple[str, int]:
    """既存の投稿をリライト結果で上書きする(下書きに戻す)。slugは変更しないためURLは維持される。
    (記事URL, post_id)を返す。
    """
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
    return post.get("link") or f"post id {post.get('id')}", post.get("id")


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


# ---------------------------------------------------------------------------
# CocoonのSEO欄(SEOタイトル・メタディスクリプション・メタキーワード)
# ---------------------------------------------------------------------------
# Cocoonはこれらをpost metaに保存するが、標準ではREST APIに公開していない。
# WordPress側でregister_post_meta(show_in_rest=true)するスニペットを追加して
# 初めて読み書きできる(未登録のmetaキーはREST APIが黙って無視する)。

COCOON_SEO_META_KEYS = {
    "seo_title": "the_page_seo_title",
    "meta_description": "the_page_meta_description",
    "keywords": "the_page_meta_keywords",
}

SEO_TITLE_MAX_CHARS = 32
META_DESCRIPTION_MIN_CHARS = 90


class SeoMetaNotExposedError(RuntimeError):
    """CocoonのSEO用post metaがREST APIに公開されていない(スニペット未導入)。"""


def _keywords_to_str(keywords) -> str:
    if isinstance(keywords, str):
        return keywords.strip()
    return ",".join(k.strip() for k in (keywords or []) if k and k.strip())


def seo_meta_exposed() -> bool:
    """CocoonのSEO欄がREST APIで読み書きできる状態か(スニペット導入済みか)を確認する。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    response = requests.get(
        f"{wp_url}/wp-json/wp/v2/posts",
        auth=(os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"]),
        params={"per_page": 1, "context": "edit", "_fields": "meta"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    posts = response.json()
    if not posts:
        return False
    meta = posts[0].get("meta") or {}
    return isinstance(meta, dict) and COCOON_SEO_META_KEYS["seo_title"] in meta


def fetch_posts_seo_status() -> list:
    """全記事(公開・下書き等)のCocoon SEO欄の入力状況を取得する。

    各要素: id, title, link, status, date, seo_title, meta_description, keywords, missing(未入力の項目名リスト)
    """
    wp_url = os.environ["WP_URL"].rstrip("/")
    auth = (os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"])

    posts = []
    page = 1
    while True:
        response = requests.get(
            f"{wp_url}/wp-json/wp/v2/posts",
            auth=auth,
            params={
                "per_page": 100,
                "page": page,
                "status": "publish,draft,future,pending,private",
                "context": "edit",
                "_fields": "id,title,link,status,date,meta",
            },
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        for p in batch:
            meta = p.get("meta") or {}
            if not isinstance(meta, dict) or COCOON_SEO_META_KEYS["seo_title"] not in meta:
                raise SeoMetaNotExposedError(
                    "CocoonのSEO欄がREST APIに公開されていません。"
                    "WordPressにSEO欄公開用のコードスニペットを追加してください。"
                )
            entry = {
                "id": p["id"],
                "title": p["title"]["raw"] if isinstance(p["title"], dict) and "raw" in p["title"] else p["title"]["rendered"],
                "link": p["link"],
                "status": p["status"],
                "date": p.get("date", ""),
            }
            for field, key in COCOON_SEO_META_KEYS.items():
                entry[field] = (meta.get(key) or "").strip()
            entry["missing"] = [f for f in COCOON_SEO_META_KEYS if not entry[f]]
            posts.append(entry)

        total_pages = int(response.headers.get("X-WP-TotalPages", "1") or 1)
        if page >= total_pages or not batch:
            break
        page += 1

    return posts


def generate_seo_fields(title: str, text: str, log=DEFAULT_LOG) -> dict:
    """既存記事のタイトル・本文から、CocoonのSEO欄に入れる3項目を生成する。"""
    excerpt = text[:4000]
    prompt = f"""あなたはSEOの専門家です。以下のブログ記事(おもちゃ専門ブログ「おもちゃミュージアム」)について、
検索エンジン向けのSEO情報を作成してください。記事の内容に書かれていないことは書かないでください。

## 記事タイトル
{title}

## 記事本文(冒頭抜粋、HTMLタグ除去済み)
{excerpt}

## 作成するもの
- seo_title: 検索結果に表示するタイトル。全角{SEO_TITLE_MAX_CHARS}文字以内厳守。主要キーワードをなるべく前半に入れる。
  記事タイトルが条件を満たしていればほぼそのままでもよい。「絶対」「100%」など誇大な表現は使わない
- meta_description: 検索結果に表示する説明文。必ず{META_DESCRIPTION_MIN_CHARS}〜120文字にする。主要キーワードを含め、
  記事を読むと何が分かるか・誰の役に立つかを具体的に書く
- keywords: 記事に関連する検索キーワードを3〜5個

必ず次のJSON形式のみで返してください。他の文章は含めないこと。
{{"seo_title": "...", "meta_description": "...", "keywords": ["...", "..."]}}
"""
    messages = [{"role": "user", "content": prompt}]
    raw_text, data = _call_claude(messages, max_tokens=4000)

    desc_len = len((data.get("meta_description") or "").strip())
    if desc_len < META_DESCRIPTION_MIN_CHARS:
        log(f"  メタディスクリプションが{desc_len}文字と短いため、書き直しを依頼します...")
        messages += [
            {"role": "assistant", "content": raw_text},
            {"role": "user", "content": (
                f"meta_descriptionが{desc_len}文字しかありません。{META_DESCRIPTION_MIN_CHARS}〜120文字になるよう、"
                "記事の内容に沿って具体的に書き足してください。同じJSON形式で全項目を出力し直してください。"
            )},
        ]
        _, data = _call_claude(messages, max_tokens=4000)

    return {
        "seo_title": (data.get("seo_title") or "").strip(),
        "meta_description": (data.get("meta_description") or "").strip(),
        "keywords": _keywords_to_str(data.get("keywords")),
    }


def update_post_seo_meta(post_id: int, seo_title: str, meta_description: str, keywords) -> None:
    """CocoonのSEO欄だけを更新する(本文・タイトル・公開状態・URLは変更しない)。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    auth = (os.environ["WP_USERNAME"], os.environ["WP_APP_PASSWORD"])

    meta = {
        COCOON_SEO_META_KEYS["seo_title"]: (seo_title or "").strip(),
        COCOON_SEO_META_KEYS["meta_description"]: (meta_description or "").strip(),
        COCOON_SEO_META_KEYS["keywords"]: _keywords_to_str(keywords),
    }
    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/posts/{post_id}",
        auth=auth,
        params={"context": "edit", "_fields": "id,meta"},
        headers={"User-Agent": BROWSER_USER_AGENT},
        json={"meta": meta},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"SEO情報の更新に失敗しました (HTTP {response.status_code}): {response.text}"
        )
    saved = response.json().get("meta") or {}
    # 未登録のmetaキーはエラーにならず無視されるため、反映されたかを確認する
    if not isinstance(saved, dict) or COCOON_SEO_META_KEYS["seo_title"] not in saved:
        raise SeoMetaNotExposedError("CocoonのSEO欄がREST APIに公開されていません")
    for key, value in meta.items():
        if (saved.get(key) or "").strip() != value:
            raise RuntimeError(f"SEO情報が反映されませんでした({key})")


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


# 動的フィルタリング版のWeb検索は1回の処理で複数の検索をまとめて行うため、上限が小さいとすぐ使い切る
WEB_SEARCH_TOOL = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}]
SEARCH_LIMIT_NOTE = (
    "Web検索には回数の上限があります。上限に達したら追加の検索はせず、それまでに検索で確認できた"
    "情報だけでまとめてください(上限に達したことを理由に、確認済みの情報まで捨てないこと)。"
)


def fact_check_article(content: str, log=DEFAULT_LOG) -> list:
    """記事本文中の具体的な事実主張(ブランド名・産地・購入可能場所・価格など)をWeb検索で
    裏取りし、誤りの疑いがある箇所のリストを返す。問題がなければ空リスト。
    """
    prompt = f"""以下はおもちゃブログの記事本文(HTML)です。この中から、ブランド名・産地/所在地・
購入可能な場所・価格・発売時期など、具体的で検証可能な事実の記述を洗い出し、Web検索を使って
実際に正しいか確認してください。

{SEARCH_LIMIT_NOTE}
確認した結果、誤り・古い情報・誇張の疑いがある箇所だけを指摘してください。問題がなければ
issuesを空配列にしてください。裏付けが取れた正しい記述はissuesに含めないでください。

出力は必ず次のJSON形式のみで返してください。JSON以外の文章は含めないでください。

{{
  "issues": [
    {{
      "claim": "記事中の該当する記述(引用)",
      "issue": "何が問題か(例: このブランドは実際には東京発祥で北海道発ではない)",
      "correction": "正しい情報・修正の方向性"
    }}
  ]
}}

記事本文:
{content}
"""
    messages = [{"role": "user", "content": prompt}]
    _, result = _call_claude(messages, tools=WEB_SEARCH_TOOL, max_tokens=8000, effort="low")
    issues = result.get("issues", [])
    if issues:
        log(f"事実確認: {len(issues)}件の指摘がありました。")
    else:
        log("事実確認: 問題は見つかりませんでした。")
    return issues


def fix_article_with_feedback(content: str, issues: list, log=DEFAULT_LOG) -> str:
    """事実確認で指摘された箇所を修正した本文(HTML)を返す。"""
    issues_text = "\n".join(
        f"- 該当箇所: {i.get('claim', '')}\n  問題点: {i.get('issue', '')}\n  修正の方向性: {i.get('correction', '')}"
        for i in issues
    )
    prompt = f"""以下はおもちゃブログの記事本文(HTML)です。事実確認により、以下の指摘がありました。
指摘された箇所のみを、修正の方向性に沿って書き換えてください。それ以外の文章・HTML構造・
装飾(ふきだし・マーカー等)・プレースホルダー([[MID_CONVERSATION]]・[[PRODUCT:数字]]・[[PLACE:数字]]・[[RELATED:数字]])は
変更せずそのまま維持してください。

## 指摘事項
{issues_text}

## 元の本文
{content}

出力は必ず次のJSON形式のみで返してください。JSON以外の文章は含めないでください。

{{
  "content": "修正後の本文(HTML全文)"
}}
"""
    messages = [{"role": "user", "content": prompt}]
    _, result = _call_claude(messages, max_tokens=12000)
    log("事実確認の指摘を反映しました。")
    return result.get("content", content)


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

    # 事実確認・修正は、商品カードや地図を入れる前の本文(プレースホルダー入り)に対して行う。
    # カード・地図・ふきだしのHTMLまでAIに書き直させると崩れることがあるため。
    issues = []
    fact_check_fixed = False
    log("① 内容確認: 事実確認(Web検索)を行っています...")
    try:
        issues = fact_check_article(body, log=log)
    except Exception as exc:
        log(f"事実確認に失敗しました(スキップします): {exc}")
    if issues:
        for i, issue in enumerate(issues, 1):
            log(f"  指摘{i}: 「{issue.get('claim', '')}」→ {issue.get('issue', '')}")
        log("② 修正: 指摘を反映して記事を修正しています...")
        try:
            body = fix_article_with_feedback(body, issues, log=log)
            fact_check_fixed = True
        except Exception as exc:
            log(f"修正の反映に失敗しました(未修正のまま保存します): {exc}")

    if "[[MID_CONVERSATION]]" in body:
        body = body.replace("[[MID_CONVERSATION]]", mid_html, 1).replace("[[MID_CONVERSATION]]", "")
    else:
        # モデルがプレースホルダーを出力しなかった場合は本文中央付近に挿入する
        midpoint = len(body) // 2
        insert_at = body.find("<h2", midpoint) if body.find("<h2", midpoint) != -1 else midpoint
        body = body[:insert_at] + mid_html + body[insert_at:]

    used_item_urls = set()
    if include_amazon:
        for i, mention in enumerate(article.get("product_mentions", [])):
            placeholder = f"[[PRODUCT:{i}]]"
            if placeholder not in body:
                continue
            item = mention.get("item")
            if item:
                # 本文はこの実商品の情報をもとに書かれているので、カードも必ず同じ商品にする
                card_html = build_rakuten_card_html(item, amazon_keyword=mention.get("search_keyword"))
                used_item_urls.add(item["url"])
            else:
                card_html = build_inline_product_card_html(
                    mention.get("name", ""), mention.get("search_keyword", ""), log=log
                )
            body = body.replace(placeholder, card_html, 1).replace(placeholder, "")
    # 残ったプレースホルダー(件数不一致等)は表示に影響しないよう除去する
    body = re.sub(r"\[\[PRODUCT:\d+\]\]", "", body)

    for i, place in enumerate(article.get("places") or []):
        placeholder = f"[[PLACE:{i}]]"
        if placeholder not in body:
            continue
        try:
            block = build_place_block_html(place, log=log)
        except Exception as exc:
            log(f"「{place.get('name', '')}」の地図・写真の作成に失敗しました: {exc}")
            block = ""
        body = body.replace(placeholder, block, 1).replace(placeholder, "")
    body = re.sub(r"\[\[PLACE:\d+\]\]", "", body)

    for i, post in enumerate(article.get("related_posts") or []):
        placeholder = f"[[RELATED:{i}]]"
        if placeholder in body:
            body = body.replace(placeholder, build_related_post_card_html(post), 1).replace(placeholder, "")
    body = re.sub(r"\[\[RELATED:\d+\]\]", "", body)

    product_html = ""
    amazon_keyword = article.get("amazon_search_keyword")
    if include_amazon and amazon_keyword:
        try:
            products = search_amazon_products(amazon_keyword)
            product_html = build_product_card_html(products)
        except Exception as exc:
            log(f"Amazon商品検索に失敗しました(楽天にフォールバックします): {exc}")
        if not product_html and rakuten_configured():
            try:
                # 本文中ですでに紹介した商品は「おすすめ商品」に重複して出さない
                items = [
                    item for item in search_rakuten_items(amazon_keyword, hits=8)
                    if item["url"] not in used_item_urls
                ][:3]
                _add_display_names(items)
                product_html = "\n".join(
                    build_rakuten_card_html(item, amazon_keyword=amazon_keyword) for item in items
                )
                if items:
                    log(f"おすすめ商品: 楽天市場から{len(items)}件を掲載します")
            except Exception as exc:
                log(f"楽天の商品検索に失敗しました(Amazon検索リンクにフォールバックします): {exc}")
        if not product_html:
            product_html = build_amazon_search_button_html(amazon_keyword)

    amazon_section = f"<h2>おすすめ商品</h2>\n{product_html}\n\n" if product_html else ""
    full_content = f"{intro_html}\n\n{body}\n\n{amazon_section}{closing_html}"

    result = {
        "title": article["title"],
        "seo_title": article.get("seo_title", article["title"]),
        "meta_description": article.get("meta_description", ""),
        "keywords": article.get("keywords", []),
        "category": article.get("category", ""),
        "char_count": _content_char_count(full_content),
        "local_path": None,
        "wp_link": None,
        "post_id": None,
        "content": full_content,
        "fact_check_issues": issues,
        "fact_check_fixed": fact_check_fixed,
        "error": None,
    }

    try:
        category_id = resolve_category_id(article.get("category", ""), categories)

        featured_media_id = None
        if include_featured_image and amazon_keyword:
            try:
                featured_media_id = set_featured_image_from_unsplash(
                    amazon_keyword, article["title"], log=log
                )
            except Exception as exc:
                log(f"アイキャッチ画像の設定に失敗しました(画像なしで投稿します): {exc}")

        log("③ 下書き作成: 確認・修正済みの内容をWordPressに下書きとして保存しています...")
        if rewrite_post_id is not None:
            link, post_id = update_wordpress_post(
                rewrite_post_id, article["title"], full_content, category_id, featured_media_id
            )
            log(f"WordPressの記事を下書きとして保存しました: {link}")
        else:
            link, post_id = post_to_wordpress_draft(
                article["title"], full_content, category_id, featured_media_id
            )
            log(f"WordPressに下書き保存しました: {link}")
        result["wp_link"] = link
        result["post_id"] = post_id
    except Exception as exc:
        log(f"WordPressへの保存に失敗しました(ローカル保存のみ完了): {exc}")
        result["error"] = str(exc)

    if result["post_id"]:
        try:
            update_post_seo_meta(
                result["post_id"], result["seo_title"], result["meta_description"], result["keywords"]
            )
            log("CocoonのSEO欄(SEOタイトル・メタディスクリプション・メタキーワード)にも書き込みました")
        except SeoMetaNotExposedError:
            log("CocoonのSEO欄はAPIから書き込めない設定のため、SEO情報は手動でコピペしてください")
        except Exception as exc:
            log(f"CocoonのSEO欄への書き込みに失敗しました(記事自体は保存済み): {exc}")

    filepath = save_article_locally(article, full_content)
    log(f"記事をローカルに保存しました: {filepath}")
    result["local_path"] = filepath
    result["content"] = full_content

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
    article = generate_rewrite(
        existing["title"], existing["text"], categories, category=category, log=log, exclude_post_id=post_id
    )
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
    reset_usage()
    run_pipeline(topic=topic)
    usage = get_usage_summary()
    print(
        f"API利用量: 入力{usage['input_tokens']:,}トークン / "
        f"出力{usage['output_tokens']:,}トークン / 概算費用 ${usage['cost_usd']:.4f}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)
