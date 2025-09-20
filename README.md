<div id="top"></div>

## 使用技術一覧

<p style="display: inline">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB.svg?logo=python&style=for-the-badge">
  <img src="https://img.shields.io/badge/discord.py-2.3-5865F2.svg?logo=discord&style=for-the-badge">
  <img src="https://img.shields.io/badge/FastAPI-0.104-009688.svg?logo=fastapi&style=for-the-badge">
  <img src="https://img.shields.io/badge/PostgreSQL-13-336791.svg?logo=postgresql&style=for-the-badge">
  <img src="https://img.shields.io/badge/Docker-Ready-1488C6.svg?logo=docker&style=for-the-badge">
</p>

## 目次

1. [プロジェクトについて](#プロジェクトについて)
2. [環境](#環境)
3. [ディレクトリ構成](#ディレクトリ構成)
4. [開発環境構築](#開発環境構築)
   [注意事項](#注意事項)
   [参考](#参考)

<br />
<div align="right">
    <a href="https://github.com/1310070/valorantBot/blob/main/Dockerfile"><strong>Dockerfileの詳細 »</strong></a>
</div>
<br />

## プロジェクト名

valorant bot by いのすけ

## プロジェクトについて
valorant bot by いのすけは Discord サーバー向けのボットです。Discord のスラッシュコマンドを利用し、サーバー内で VALORANT の募集 DM を送信したり、tracker.gg のプロフィール URL を生成したり、ゲームを起動しなくても当日のストア情報を取得できるようにしました。FastAPI エンドポイントを併設し、Chrome 拡張から受け取った Riot 認証 Cookie を Discord ユーザーごとに暗号化保存します。

## 主な機能
- `/call` — 選択したメンバーへ募集 DM を送り、参加可否とメッセージを自動収集します。
- `/profile` — Riot ID から tracker.gg のプロフィール URL を生成し、リンクボタン付きで返します。
- `/store` — 保存された Cookie を用いて VALORANT ストアの武器スキン4種を Embed 表示し、失敗時はワンクリック診断ボタンを提供します。
- FastAPI `/nonce`・`/riot-cookies` — Chrome 拡張から送信される Cookie を検証し、ユーザー ID ごとに暗号化して PostgreSQL に保存します。
- `scripts/diag_reauth.py` — CLI からストア取得の再認証フローを総当たりで確認する診断ツールです。

## 環境

| 種別 | バージョン |
| ---- | ---------- |
| Python | 3.11 |
| discord.py | 2.3 |
| FastAPI | 0.104 |
| PostgreSQL | 13 |

## ディレクトリ構成

```
valorantBot/
├── Dockerfile
├── README.md
├── requirements.txt
├── riot-cookie-extension/      # Cookie 収集用 Chrome 拡張
│   ├── manifest.json
│   ├── popup.html
│   └── popup.js
└── valorantBot2/
    ├── __init__.py
    ├── bot.py                  # Discord ボットのエントリーポイント
    ├── rec.py                  # FastAPI アプリケーション
    ├── cogs/
    │   └── ui.py               # スラッシュコマンド定義
    ├── services/
    │   ├── cookiesDB.py        # Cookie 永続化処理
    │   ├── get_store.py        # ストア取得ロジック
    │   ├── net_diag.py         # ネットワーク診断ユーティリティ
    │   ├── profile_service.py  # tracker.gg URL 生成
    │   └── reauth_diag.py      # 再認証診断ロジック
    ├── scripts/
    │   └── diag_reauth.py      # CLI 診断スクリプト
    └── views/
        └── buttons.py          # Discord UI コンポーネント
```

<p align="right">(<a href="#top">トップへ</a>)</p>

## 開発環境構築

### 1. リポジトリのクローン

```bash
git clone https://github.com/1310070/valorantBot.git
cd valorantBot
```

### 2. 依存関係のインストール

```bash
python -m venv .venv
source .venv/bin/activate  # Windows の場合は .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 環境変数の設定

`.env` ファイルを作成し、以下の変数を設定します（例）。

```env
DISCORD_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DATABASE_URL=postgresql://user:password@host:5432/valorant_bot
COOKIE_ENC_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
STARTUP_CHANNEL_ID=
PORT=8190
VALORANT_COOKIES_DIR=
HTTP_PROXY=
HTTPS_PROXY=
NO_PROXY=
```

- `DISCORD_TOKEN` は Discord ボットのトークンです。
- `DATABASE_URL` または `DB_DSN` で PostgreSQL への接続文字列を指定します。`postgresql://` 形式を推奨します。
- `COOKIE_ENC_KEY` が未設定の場合、起動ごとにランダム生成されるため永続保存したい場合は固定値を設定してください（`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`）。
- `STARTUP_CHANNEL_ID` を設定すると起動時メッセージを送るチャンネルを固定できます。
- `VALORANT_COOKIES_DIR` を設定するとファイルベースの Cookie パス候補が追加されます。
- 必要に応じて HTTP(S) プロキシ関連の環境変数を指定してください。

サーバー上にデプロイする際にプロキシ設定をしないと、cloudflareによって、API呼び出しがブロックされるので設定してください。私はgoogle cloudのVPCでファイアウォールルールを設定して、回避しました。ローカル環境でやる分には必要なかったです。

### 4. ボットと API サーバーの起動

```bash
python -m valorantBot2.bot
```

ボットを起動するとバックグラウンドで FastAPI サーバー（既定ポート 8190）も起動し、`/nonce`・`/riot-cookies` エンドポイントを提供します。

FastAPI サーバーのみをローカルで確認したい場合は次のコマンドを使用します。

```bash
python -m valorantBot2.rec
```

### 5. Docker を利用した起動

```bash
docker build -t valorant-bot .
docker run --rm \
  -e DISCORD_TOKEN=xxxxxxxx \
  -e DATABASE_URL=postgresql://user:password@host:5432/valorant_bot \
  -e COOKIE_ENC_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  -p 8190:8190 \
  valorant-bot
```

### 6. Chrome 拡張で Cookie を登録

1. `riot-cookie-extension/` フォルダを Chrome の「デベロッパーモード」で読み込みます。
2. 拡張の UI から Discord ユーザー ID を入力し、指示に従って `https://auth.riotgames.com` の Cookie を送信します。
3. `ok: true` と表示されれば保存成功です。`/store` コマンドでストア情報を取得できるようになります。

### コマンド一覧

| 用途 | コマンド |
| ---- | -------- |
| Discord ボット + API 起動 | `python -m valorantBot2.bot` |

<p align="right">(<a href="#top">トップへ</a>)</p>
　注意事項

/storeについて
！！！！！！！アカウントBANのリスクがあります！！！！！！！！！！！！！！！
公式が出しているend pointではなく、非公式な団体が出しているend point を使用しています。また公式がOKを出しているわけでもありません。また、cookie情報は極めて大切なログイン情報源です。データベースで管理しているため、管理者以外は基本覗くことはできませんが、最悪の場合は乗っ取られる可能性があります。これらの注意事項を承認したうえで使用してください。また、いのすけ(1310070)は何事も責任を負いません。よろしくお願いします。

<p align="right">(<a href="#top">トップへ</a>)</p>
　参考

https://valapidocs.techchrism.me/
https://valapidocs.techchrism.me/endpoint/entitlement
https://valapidocs.techchrism.me/endpoint/cookie-reauth
https://valapidocs.techchrism.me/endpoint/auth-request
https://valapidocs.techchrism.me/endpoint/storefront
https://valapidocs.techchrism.me/endpoint/prices


